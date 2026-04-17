# ---------------------------------------------------------------------------
# config.py — All configuration settings for the chat service.
#
# HOW IT WORKS:
# Every setting is read from an ENVIRONMENT VARIABLE. This means:
#   - In production: values come from docker-compose.yml or Docker secrets
#   - In local dev: values come from a .env file or shell exports
#   - In CI/tests: values come from GitHub Secrets
#
# WHY ENVIRONMENT VARIABLES?
# The Twelve-Factor App methodology says: "Store config in the environment."
# This keeps secrets out of code and lets us change settings without
# rebuilding the Docker image.
#
# HOW TO ADD A NEW SETTING:
# 1. Add a field below with a type annotation and default value
# 2. Set the environment variable in docker-compose.yml
# 3. If it's a secret, add it as a GitHub Secret
#
# PORTED FROM: yral-ai-chat/src/config.rs (the Rust service's config)
# ---------------------------------------------------------------------------

import os


def _env(key: str, default: str = "") -> str:
    """Read an environment variable, return default if not set or empty."""
    return os.environ.get(key, default) or default


def _env_int(key: str, default: int = 0) -> int:
    """Read an environment variable as an integer."""
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float = 0.0) -> float:
    """Read an environment variable as a float."""
    try:
        return float(_env(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    """Read an environment variable as a boolean."""
    return _env(key, str(default)).lower() in ("true", "1", "yes")


# =========================================================================
# APP SETTINGS
# =========================================================================

APP_NAME = _env("APP_NAME", "Yral AI Chat API")
APP_VERSION = _env("APP_VERSION", "1.0.0")
ENVIRONMENT = _env("ENVIRONMENT", "development")
DEBUG = _env_bool("DEBUG", False)
HOST = _env("HOST", "0.0.0.0")
PORT = _env_int("PORT", 8000)

# =========================================================================
# GEMINI (Primary AI model for chat)
# =========================================================================
# Gemini Flash is Google's fast AI model. We use it through the
# OpenAI-compatible API endpoint, which means we can use the standard
# OpenAI Python SDK with a custom base_url.

GEMINI_API_KEY = _env("GEMINI_API_KEY")
GEMINI_MODEL = _env("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_MAX_TOKENS = _env_int("GEMINI_MAX_TOKENS", 2048)
GEMINI_TEMPERATURE = _env_float("GEMINI_TEMPERATURE", 0.7)
GEMINI_TIMEOUT = _env_int("GEMINI_TIMEOUT", 60)

# The OpenAI-compatible endpoint for Gemini.
# This lets us use the standard OpenAI SDK instead of Google's custom SDK.
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"

# =========================================================================
# OPENROUTER (AI model for NSFW content)
# =========================================================================
# OpenRouter is a proxy that routes to various AI models.
# We use it for NSFW influencers because it has fewer content restrictions
# than Gemini's direct API.

OPENROUTER_API_KEY = _env("OPENROUTER_API_KEY")
OPENROUTER_MODEL = _env("OPENROUTER_MODEL", "google/gemini-2.5-flash")
OPENROUTER_MAX_TOKENS = _env_int("OPENROUTER_MAX_TOKENS", 2048)
OPENROUTER_TEMPERATURE = _env_float("OPENROUTER_TEMPERATURE", 0.7)
OPENROUTER_TIMEOUT = _env_int("OPENROUTER_TIMEOUT", 30)
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# =========================================================================
# S3 STORAGE (for media files — images, audio)
# =========================================================================
# We use Hetzner Object Storage, which is S3-compatible.
# The boto3 library talks to it the same way it talks to AWS S3.

AWS_ACCESS_KEY_ID = _env("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = _env("AWS_SECRET_ACCESS_KEY")
AWS_S3_BUCKET = _env("AWS_S3_BUCKET")
AWS_REGION = _env("AWS_REGION", "eu-central-1")
S3_ENDPOINT_URL = _env("S3_ENDPOINT_URL")
S3_PUBLIC_URL_BASE = _env("S3_PUBLIC_URL_BASE")
S3_URL_EXPIRES_SECONDS = _env_int("S3_URL_EXPIRES_SECONDS", 900)  # 15 minutes

# =========================================================================
# MEDIA LIMITS
# =========================================================================

MAX_IMAGE_SIZE_MB = _env_int("MAX_IMAGE_SIZE_MB", 10)
MAX_AUDIO_SIZE_MB = _env_int("MAX_AUDIO_SIZE_MB", 20)
MAX_AUDIO_DURATION_SECONDS = _env_int("MAX_AUDIO_DURATION_SECONDS", 300)

MAX_IMAGE_SIZE_BYTES = MAX_IMAGE_SIZE_MB * 1024 * 1024
MAX_AUDIO_SIZE_BYTES = MAX_AUDIO_SIZE_MB * 1024 * 1024

# =========================================================================
# REPLICATE (Image Generation)
# =========================================================================

REPLICATE_API_TOKEN = _env("REPLICATE_API_TOKEN")
REPLICATE_MODEL = _env("REPLICATE_MODEL", "black-forest-labs/flux-dev")

# =========================================================================
# PUSH NOTIFICATIONS
# =========================================================================
# The metadata server handles sending push notifications to mobile devices.
# We POST to it after saving each AI response.

METADATA_URL = _env("METADATA_URL", "https://metadata.yral.com")
METADATA_AUTH_TOKEN = _env("YRAL_METADATA_NOTIFICATION_API_KEY")

# =========================================================================
# CORS (Cross-Origin Resource Sharing)
# =========================================================================
# Controls which websites/apps can call our API.
# "*" means "allow everyone" — fine for a mobile app API.

CORS_ORIGINS = _env("CORS_ORIGINS", "*")

# =========================================================================
# RATE LIMITING
# =========================================================================

RATE_LIMIT_PER_MINUTE = _env_int("RATE_LIMIT_PER_MINUTE", 300)
RATE_LIMIT_PER_HOUR = _env_int("RATE_LIMIT_PER_HOUR", 5000)

# =========================================================================
# ADMIN
# =========================================================================

ADMIN_KEY = _env("ADMIN_KEY_TO_DELETE_INFLUENCER")

# =========================================================================
# GOOGLE CHAT WEBHOOK (admin notifications)
# =========================================================================
# Sends notifications to a Google Chat space when influencers are
# banned/unbanned. Set up a webhook in Google Chat and paste the URL here.

GOOGLE_CHAT_WEBHOOK_URL = _env("GOOGLE_CHAT_WEBHOOK_URL")

# =========================================================================
# JWT AUTH (expected issuers for token validation)
# =========================================================================
# These are the only issuers we trust. Tokens from any other issuer
# are rejected. The existing Rust service uses these same issuers.

EXPECTED_ISSUERS = ["https://auth.yral.com", "https://auth.dolr.ai"]
