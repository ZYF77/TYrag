"""Shared test fixtures for enterprise layer."""

import os

import pytest
import pytest_asyncio

from enterprise.gateway.ragflow_client import RAGFlowStub

# Route registration for the temporary demo router is read at import time, so
# test-mode apps must opt in before `enterprise.gateway.app` is imported.
os.environ.setdefault("ENTERPRISE_TEST_MODE", "1")


@pytest.fixture
def ragflow_stub():
    """Return a RAGFlowStub that simulates a healthy RAGFlow."""
    return RAGFlowStub(healthy=True)


@pytest.fixture
def ragflow_stub_unhealthy():
    """Return a RAGFlowStub that simulates an unhealthy RAGFlow."""
    return RAGFlowStub(healthy=False)


@pytest_asyncio.fixture
async def isolated_gateway_db(tmp_path):
    """Inject a per-test SQLite file and reset the module-level connection."""
    import enterprise.gateway.app as app_module
    from enterprise.gateway.sync.models import init_db

    if app_module._db is not None:
        await app_module._db.close()
        app_module._db = None

    db_path = tmp_path / "gateway-test.db"
    db = await init_db(str(db_path))
    app_module.app.dependency_overrides[app_module.get_db] = lambda: db
    try:
        yield db, db_path
    finally:
        app_module.app.dependency_overrides.pop(app_module.get_db, None)
        await db.close()
        if app_module._db is not None:
            await app_module._db.close()
            app_module._db = None
