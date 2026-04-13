"""Smoke tests for the FastAPI chat service — no real database required."""


def test_root_returns_service_info(client):
    """Root endpoint returns service name and status."""
    r = client.get("/")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "running"
    assert "service" in data


def test_health_ok(client):
    """Health endpoint returns 200 when database is reachable."""
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "OK", "database": "reachable"}


def test_health_db_down(client, fake_db):
    """Health endpoint returns 503 when database is unreachable."""
    fake_db.healthy = False
    r = client.get("/health")
    assert r.status_code == 503


def test_auth_endpoint_requires_token(client):
    """Auth test endpoint returns 401 without a valid JWT."""
    r = client.get("/api/v1/auth/me")
    assert r.status_code == 401


def test_sentry_test_endpoint_removed(client):
    """The /sentry-test endpoint should not exist (security)."""
    r = client.get("/sentry-test")
    assert r.status_code == 404
