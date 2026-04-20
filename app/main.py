# ---------------------------------------------------------------------------
# main.py — The entry point of the YRAL chat service.
#
# This is the FIRST file that runs when the app starts. The command
# "uvicorn main:app" tells the web server: "load the file main.py and
# find the variable called 'app' in it."
#
# WHAT THIS FILE DOES:
#   1. Sets up the FastAPI web application
#   2. Configures CORS (which websites/apps can call our API)
#   3. Initializes the database connection pool on startup
#   4. Closes the database connection pool on shutdown
#   5. Defines the health check endpoint (used by Docker, CI, monitoring)
#   6. Will include all route modules as the service grows (Phase 3+)
#
# ARCHITECTURE:
#   Browser/Mobile App
#     -> Caddy (HTTPS reverse proxy)
#       -> This FastAPI app (port 8000)
#         -> PostgreSQL (via HAProxy -> Patroni leader)
#         -> Gemini API (for AI chat responses)
#         -> S3 (for media storage)
#
# PORTED FROM: yral-ai-chat/src/main.rs
# ---------------------------------------------------------------------------

import logging
from contextlib import asynccontextmanager

import sentry_sdk
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware

import database
from auth import get_current_user
import config

# ---------------------------------------------------------------------------
# LOGGING SETUP
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SENTRY ERROR TRACKING — initialized BEFORE app = FastAPI()
# ---------------------------------------------------------------------------
# Must be called before FastAPI() is created so Sentry can hook into the
# framework. Uses the exact pattern from Sentry's FastAPI setup guide.
import os
_sentry_dsn = os.environ.get("SENTRY_DSN", "").strip()
if _sentry_dsn:
    sentry_sdk.init(
        dsn=_sentry_dsn,
        send_default_pii=True,
        traces_sample_rate=1.0,
        profiles_sample_rate=1.0,
        environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
        release=os.environ.get("SENTRY_RELEASE"),
    )
    logger.info(f"Sentry initialized: {_sentry_dsn[:40]}...")
else:
    logger.info("Sentry not configured (SENTRY_DSN not set)")


# ---------------------------------------------------------------------------
# APP LIFESPAN (startup and shutdown events)
# ---------------------------------------------------------------------------
# The @asynccontextmanager decorator creates a "lifespan" function that runs:
#   - BEFORE the app starts serving requests (everything before "yield")
#   - AFTER the app stops serving requests (everything after "yield")
#
# We use this to:
#   - Create the database connection pool at startup
#   - Close the database connection pool at shutdown
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    App startup and shutdown logic.

    STARTUP (before yield):
    - Creates the database connection pool
    - Logs that the service is ready

    SHUTDOWN (after yield):
    - Closes all database connections cleanly
    - Logs that the service is shutting down
    """
    # --- STARTUP ---
    logger.info(f"Starting {config.APP_NAME} v{config.APP_VERSION}")
    logger.info(f"Environment: {config.ENVIRONMENT}")

    # Create the database connection pool.
    # This opens 2-10 connections to PostgreSQL via HAProxy.
    try:
        await database.get_pool()
        logger.info("Database pool initialized successfully")
    except Exception as e:
        # If we can't connect to the database at startup, log the error
        # but DON'T crash. The health check will report unhealthy, and
        # the pool will retry on the first real request.
        logger.error(f"Failed to initialize database pool at startup: {e}")

    # "yield" means: "startup is done, start serving requests."
    # Everything after yield runs when the app is shutting down.
    yield

    # --- SHUTDOWN ---
    logger.info("Shutting down...")
    await database.close_pool()
    logger.info("Shutdown complete")


# ---------------------------------------------------------------------------
# CREATE THE FASTAPI APP
# ---------------------------------------------------------------------------
# This is the "app" variable that uvicorn looks for.
# lifespan=lifespan tells FastAPI to run our startup/shutdown logic.
# Disable FastAPI's auto-exposed API docs (/docs, /redoc, /openapi.json).
# These leak internal route shapes, request/response schemas, and admin
# endpoints to any unauthenticated caller. The old Rust service hid these;
# we match that behavior. Mobile clients and integrations already know the
# contract — OpenAPI is only useful during development.
app = FastAPI(
    title=config.APP_NAME,
    version=config.APP_VERSION,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


# ---------------------------------------------------------------------------
# CORS MIDDLEWARE
# ---------------------------------------------------------------------------
# CORS (Cross-Origin Resource Sharing) controls which websites/apps can
# call our API. The mobile app needs this to make HTTP requests.
# "*" means "allow everyone" — appropriate for a public API.
#
# WHY ALL THESE OPTIONS?
#   allow_origins: which domains can call us
#   allow_credentials: whether cookies/auth headers are allowed
#   allow_methods: which HTTP methods are allowed (GET, POST, etc.)
#   allow_headers: which request headers are allowed (Authorization, etc.)
if config.CORS_ORIGINS == "*":
    origins = ["*"]
else:
    origins = [o.strip() for o in config.CORS_ORIGINS.split(",")]

# allow_credentials=False when origins is "*" (security: prevents credential
# leaking to arbitrary origins). The mobile app uses Bearer tokens in headers,
# not cookies, so credentials=False is fine.
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=("*" not in origins),
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# SENTRY VERIFICATION ENDPOINT (remove after confirming Sentry works)
# ---------------------------------------------------------------------------
@app.get("/sentry-debug")
async def trigger_error():
    division_by_zero = 1 / 0


# ---------------------------------------------------------------------------
# AUTH TEST ENDPOINT
# ---------------------------------------------------------------------------

@app.get("/api/v1/auth/me")
async def auth_me(request: Request):
    """Test endpoint to verify JWT authentication is working."""
    user_id = get_current_user(request)
    return {"user_id": user_id}


# /debug/routes endpoint REMOVED — it was exposing all API routes publicly.
# Use `curl https://chat-ai.rishi.yral.com/openapi.json` for API docs instead
# (FastAPI's built-in OpenAPI spec, which is standard practice).


# ---------------------------------------------------------------------------
# ROUTE REGISTRATION
# ---------------------------------------------------------------------------
# Each route module handles one area of the API. We import them and register
# them with the app so FastAPI knows about all endpoints.
# ---------------------------------------------------------------------------
from routes.health import router as health_router
app.include_router(health_router)

try:
    from routes.influencers import router as influencers_router
    app.include_router(influencers_router)
    logger.info("Influencer routes loaded")
except Exception as e:
    logger.error(f"Failed to load influencer routes: {e}")

try:
    from routes.chat_v1 import router as chat_router
    app.include_router(chat_router)
    logger.info("Chat routes loaded")
except Exception as e:
    logger.error(f"Failed to load chat routes: {e}")

try:
    from routes.media import router as media_router
    app.include_router(media_router)
    logger.info("Media routes loaded")
except Exception as e:
    logger.error(f"Failed to load media routes: {e}")

try:
    from routes.websocket import router as ws_router
    app.include_router(ws_router)
    logger.info("WebSocket routes loaded")
except Exception as e:
    logger.error(f"Failed to load WebSocket routes: {e}")

try:
    from routes.chat_v2 import router as chat_v2_router
    app.include_router(chat_v2_router)
    logger.info("Chat V2 routes loaded")
except Exception as e:
    logger.error(f"Failed to load chat V2 routes: {e}")

try:
    from routes.human_chat import router as human_chat_router
    app.include_router(human_chat_router)
    logger.info("Human chat routes loaded")
except Exception as e:
    logger.error(f"Failed to load human chat routes: {e}")

try:
    from routes.chat_v3 import router as chat_v3_router
    app.include_router(chat_v3_router)
    logger.info("Chat V3 (unified inbox) routes loaded")
except Exception as e:
    logger.error(f"Failed to load chat V3 routes: {e}")
