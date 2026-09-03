"""Shared test fixtures for enterprise layer."""

import os

import pytest
import pytest_asyncio

from enterprise.gateway.db.database import GatewayDatabase
from enterprise.gateway.db.testing import create_gateway
from enterprise.gateway.ragflow_client import RAGFlowStub

# Route registration for the temporary demo router is read at import time, so
# test-mode apps must opt in before `enterprise.gateway.app` is imported.
os.environ.setdefault("ENTERPRISE_TEST_MODE", "1")
os.environ.setdefault(
    "ENTERPRISE_GATEWAY_TEST_DATABASE_URL",
    "postgresql+asyncpg://tyrag_gateway_test:tyrag_gateway_test@127.0.0.1:55432/tyrag_gateway_test",
)


@pytest.fixture
def ragflow_stub():
    """Return a RAGFlowStub that simulates a healthy RAGFlow."""
    return RAGFlowStub(healthy=True)


@pytest.fixture
def ragflow_stub_unhealthy():
    """Return a RAGFlowStub that simulates an unhealthy RAGFlow."""
    return RAGFlowStub(healthy=False)


@pytest_asyncio.fixture
async def gateway_db():
    """Schema-isolated PostgreSQL GatewayDatabase for unit tests."""
    gateway = await create_gateway(":memory:")
    try:
        yield gateway
    finally:
        await gateway.dispose()


@pytest_asyncio.fixture
async def gateway_db_file(tmp_path):
    """Schema-isolated PostgreSQL GatewayDatabase using the legacy fixture key."""
    db_path = tmp_path / "gateway-test.db"
    gateway = await create_gateway(str(db_path))
    try:
        yield gateway
    finally:
        await gateway.dispose()


@pytest_asyncio.fixture
async def db(gateway_db: GatewayDatabase):
    """Open write transaction rolled back after each test."""
    conn = await gateway_db.connect()
    await conn.begin()
    try:
        yield conn
    finally:
        await conn.rollback()
        await conn.close()


@pytest_asyncio.fixture
async def isolated_gateway_db():
    """Inject a per-test schema-isolated PostgreSQL GatewayDatabase into FastAPI."""
    import os
    import tempfile

    import enterprise.gateway.app as app_module

    if app_module._gateway_db is not None:
        await app_module._gateway_db.dispose()
        app_module._gateway_db = None

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    gateway = await create_gateway(db_path)
    app_module.app.dependency_overrides[app_module.get_gateway_db] = lambda: gateway
    try:
        yield gateway, db_path
    finally:
        app_module.app.dependency_overrides.pop(app_module.get_gateway_db, None)
        await gateway.dispose()
        if app_module._runtime_manager is not None:
            app_module._runtime_manager = None
            app_module.config.clear_runtime_settings()
        if app_module._gateway_db is not None:
            await app_module._gateway_db.dispose()
            app_module._gateway_db = None
        try:
            os.unlink(db_path)
        except OSError:
            pass


@pytest_asyncio.fixture
async def isolated_db(isolated_gateway_db):
    """Direct AsyncConnection for seeding data in integration tests."""
    gateway, _ = isolated_gateway_db
    async with gateway.transaction(write=True) as conn:
        yield conn
