"""
pytest fixtures for the YRAL chat service.

The app's `database` module opens an async connection pool to PostgreSQL.
Unit tests don't have a Postgres instance, so we inject a FAKE database
module into sys.modules BEFORE importing app.main. This lets tests run
without a real database.
"""
import sys
import types
from pathlib import Path

import pytest

# Make `app/` and `infra/` importable from tests/.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))


class _FakePool:
    """Fake asyncpg pool that does nothing."""
    pass


class _FakeDB:
    """In-memory fake that mimics the real database module's async API."""
    def __init__(self):
        self.healthy = True
        self._pool = _FakePool()

    async def get_pool(self):
        return self._pool

    async def close_pool(self):
        pass

    async def check_db_health(self):
        return self.healthy


@pytest.fixture
def fake_db(monkeypatch):
    fake = _FakeDB()
    mod = types.ModuleType("database")
    # Expose all public methods from _FakeDB on the mock module.
    for attr in dir(fake):
        if not attr.startswith("_"):
            setattr(mod, attr, getattr(fake, attr))
    monkeypatch.setitem(sys.modules, "database", mod)
    return fake


@pytest.fixture
def client(fake_db, monkeypatch):
    # Ensure no real Sentry DSN leaks into tests
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    # Clear ALL cached app modules so they pick up the fake database
    for mod_name in list(sys.modules):
        if mod_name in ("main", "database") or mod_name.startswith("routes."):
            sys.modules.pop(mod_name, None)
    import main
    from fastapi.testclient import TestClient
    return TestClient(main.app, raise_server_exceptions=False)
