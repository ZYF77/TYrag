"""Request ownership, bounded execution and independent database heartbeats."""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
from contextvars import ContextVar
from functools import wraps

from enterprise.gateway.db.ops import gw_write

active_run: ContextVar[dict | None] = ContextVar("active_run", default=None)
HEARTBEAT_SECONDS = 30
MAX_RUN_SECONDS = 1800


def identity_for(principal, conversation):
    return dict(conversation_id=conversation["conversation_id"], tenant_id=principal.tenant_id,
                business_user_id=principal.business_user_id)


async def interrupted_result(db, identity, run):
    from . import v2_store as store
    from . import v2_router as v2
    response = v2._error(503, "RUN_INTERRUPTED", "回答未完成，请新建会话继续。")
    async def interrupt(conn):
        await store.lock_conversation(conn, **identity)
        current = await store.get_message_run(conn, **identity, client_message_id=run["client_message_id"])
        if current and current["status"] == "running":
            # Explicit cancellation may precede lease expiry. Only this run is affected.
            await store.exec_sql(conn, """UPDATE ext_v2_message_run
                SET lease_expires_at=clock_timestamp()::text
                WHERE conversation_id=? AND tenant_id=? AND business_user_id=?
                AND run_id=? AND status='running'""",
                (*identity.values(), run["run_id"]))
            current = await store.mark_expired_run_interrupted(conn, **identity,
                client_message_id=run["client_message_id"])
        return current
    current = await gw_write(db, interrupt)
    if current and current.get("result_json"):
        result = json.loads(current["result_json"])
        if "_error" in result:
            return None, v2._error_response_from_result(result)
        return {**result, "replayed": True}, None
    return None, response


@contextlib.asynccontextmanager
async def execution_lease(db, principal, conversation, run):
    from . import v2_store as store
    identity = identity_for(principal, conversation)
    await gw_write(db, store.assert_run_owner, **identity, run_id=run["run_id"])
    owner = asyncio.current_task()
    token = active_run.set(dict(identity=identity, run_id=run["run_id"]))
    lost = False

    async def heartbeat():
        nonlocal lost
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_SECONDS)
                # The DB operation itself is bounded; a stuck DB must not keep emitting.
                await asyncio.wait_for(gw_write(db, store.renew_run, **identity,
                    run_id=run["run_id"]), timeout=HEARTBEAT_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            lost = True
            owner.cancel()

    task = asyncio.create_task(heartbeat())
    try:
        async with asyncio.timeout(MAX_RUN_SECONDS):
            yield
    except asyncio.CancelledError:
        if lost:
            raise store.RunOwnershipLost() from None
        raise
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        active_run.reset(token)


def leased_run(fn):
    """Shared by both transports and runtimes; no success after losing ownership."""
    signature = inspect.signature(fn)

    def context(args, kwargs):
        bound = signature.bind(*args, **kwargs).arguments
        return tuple(bound[k] for k in ("db", "principal", "conversation", "run"))

    if inspect.isasyncgenfunction(fn):
        @wraps(fn)
        async def stream(*args, **kwargs):
            from . import v2_store as store
            from . import v2_router as v2
            db, principal, conversation, run = context(args, kwargs)
            identity = identity_for(principal, conversation)
            try:
                async with execution_lease(db, principal, conversation, run):
                    async with contextlib.aclosing(fn(*args, **kwargs)) as source:
                        async for item in source:
                            yield item
            except (store.RunOwnershipLost, TimeoutError):
                result, error = await interrupted_result(db, identity, run)
                if result:
                    async for item in v2._result_events(result):
                        yield item
                else:
                    body = json.loads(error.body)
                    yield v2._sse("run.failed", dict(body, conversationId=identity["conversation_id"], runId=run["run_id"]))
            except Exception:
                _, error = await interrupted_result(db, identity, run)
                body = json.loads(error.body) if error else {"code": "RUN_INTERRUPTED", "message": "Run interrupted"}
                yield v2._sse("run.failed", dict(body, conversationId=identity["conversation_id"], runId=run["run_id"]))
            except (asyncio.CancelledError, GeneratorExit):
                await asyncio.shield(interrupted_result(db, identity, run))
                raise
        return stream

    @wraps(fn)
    async def json_run(*args, **kwargs):
        from . import v2_store as store
        db, principal, conversation, run = context(args, kwargs)
        try:
            async with execution_lease(db, principal, conversation, run):
                return await fn(*args, **kwargs)
        except (store.RunOwnershipLost, TimeoutError):
            return await interrupted_result(db, identity_for(principal, conversation), run)
        except Exception:
            return await interrupted_result(db, identity_for(principal, conversation), run)
        except asyncio.CancelledError:
            await asyncio.shield(interrupted_result(db, identity_for(principal, conversation), run))
            raise
    return json_run
