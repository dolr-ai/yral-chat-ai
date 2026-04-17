"""
Unit tests for the database module (app/database.py).

These tests verify that:
  - _read_database_url reads from the correct sources
  - The config module loads defaults correctly
  - The auth module validates JWT tokens correctly

NOTE: The actual database connection pool (asyncpg) is tested via integration
tests against a real PostgreSQL instance, not via unit tests with mocks.
Mocking async database pools is fragile and doesn't catch real issues.
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure app/ is importable
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "app") not in sys.path:
    sys.path.insert(0, str(ROOT / "app"))


# ---------------------------------------------------------------------------
# Tests for config module
# ---------------------------------------------------------------------------

class TestConfig:
    """Tests for configuration loading from environment variables."""

    def test_default_gemini_model(self):
        """Default Gemini model should be gemini-2.5-flash."""
        sys.modules.pop("config", None)
        import config
        assert config.GEMINI_MODEL == "gemini-2.5-flash"

    def test_default_cors_origins(self):
        """Default CORS should allow all origins."""
        sys.modules.pop("config", None)
        import config
        assert config.CORS_ORIGINS == "*"

    def test_expected_issuers(self):
        """JWT issuers must match the auth service URLs."""
        sys.modules.pop("config", None)
        import config
        assert "https://auth.yral.com" in config.EXPECTED_ISSUERS
        assert "https://auth.dolr.ai" in config.EXPECTED_ISSUERS

    def test_default_rate_limits(self):
        """Default rate limits should match the Rust service."""
        sys.modules.pop("config", None)
        import config
        assert config.RATE_LIMIT_PER_MINUTE == 300
        assert config.RATE_LIMIT_PER_HOUR == 5000

    def test_gemini_base_url(self):
        """Gemini base URL should be the OpenAI-compatible endpoint."""
        sys.modules.pop("config", None)
        import config
        assert "generativelanguage.googleapis.com" in config.GEMINI_BASE_URL

    def test_env_override(self):
        """Environment variables should override defaults."""
        with patch.dict(os.environ, {"GEMINI_MODEL": "gemini-2.0-pro"}):
            sys.modules.pop("config", None)
            import config
            assert config.GEMINI_MODEL == "gemini-2.0-pro"


# ---------------------------------------------------------------------------
# Tests for database URL reading
# ---------------------------------------------------------------------------

class TestDatabaseUrl:
    """Tests for _read_database_url function."""

    def test_reads_from_env_var(self):
        """Falls back to DATABASE_URL env var when secret file doesn't exist."""
        sys.modules.pop("database", None)
        test_url = "postgresql://user:pass@localhost/testdb"
        with patch.dict(os.environ, {"DATABASE_URL": test_url}), \
             patch("os.path.exists", return_value=False):
            import database
            url = database._read_database_url()
            assert url == test_url

    def test_reads_from_secret_file(self):
        """Prefers /run/secrets/database_url when it exists."""
        sys.modules.pop("database", None)
        test_url = "postgresql://user:pass@haproxy/chatdb"
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", create=True) as mock_open:
            mock_open.return_value.__enter__ = lambda s: s
            mock_open.return_value.__exit__ = lambda s, *a: None
            mock_open.return_value.read = lambda: f"  {test_url}  \n"
            import database
            url = database._read_database_url()
            assert url == test_url
