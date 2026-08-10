"""Real Redis/Valkey replay protection integration check.

The runner only includes this module in the Integration profile after it has
validated ENTERPRISE_REDIS_URL.  It intentionally uses two authenticator
instances to prove the atomic key reservation is shared across workers.
"""
from __future__ import annotations

import os
import uuid

import pytest

from enterprise.gateway.auth.service_auth import RedisReplayStore, ReplayStoreUnavailable


@pytest.mark.asyncio
async def test_redis_replay_reservation_is_shared_between_instances():
    url = os.environ.get("ENTERPRISE_REDIS_URL", "").strip()
    if not url:
        pytest.fail("ENTERPRISE_REDIS_URL is required for the Integration profile")
    prefix = f"tyrag:integration-test:{uuid.uuid4().hex}:"
    first = RedisReplayStore(url, prefix=prefix)
    second = RedisReplayStore(url, prefix=prefix)
    key = uuid.uuid4().hex
    try:
        assert await first.reserve(key, 0) is True
        assert await second.reserve(key, 0) is False
    except ReplayStoreUnavailable as exc:
        pytest.fail(f"Redis/Valkey replay integration unavailable: {exc}")
