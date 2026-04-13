"""Smoke tests for the FastAPI chat service — no real database required."""


def test_root_returns_service_info(client):
    """Root endpoint returns service name and status."""
    r = client.get("/")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "running"
    assert "service" in data


def test_health_endpoint_exists(client):
    """Health endpoint exists and returns a valid JSON response."""
    r = client.get("/health")
    # In test environment (no real DB), it may return 200 or 503
    # depending on whether the fake DB is wired up. Just verify
    # the endpoint exists and returns valid JSON.
    assert r.status_code in (200, 503)
    data = r.json()
    assert "status" in data or "detail" in data


def test_auth_endpoint_requires_token(client):
    """Auth test endpoint returns 401 without a valid JWT."""
    r = client.get("/api/v1/auth/me")
    assert r.status_code == 401


def test_sentry_test_endpoint_removed(client):
    """The /sentry-test endpoint should not exist (security)."""
    r = client.get("/sentry-test")
    assert r.status_code == 404
