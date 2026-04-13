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

from database import get_pool, close_pool, check_db_health
from auth import get_current_user
from infra import init_sentry
import config

# ---------------------------------------------------------------------------
# LOGGING SETUP
# ---------------------------------------------------------------------------
# Configure Python's logging to show timestamps, log level, and messages.
# This output appears in `docker logs <container>` and in CI deploy logs.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SENTRY ERROR TRACKING
# ---------------------------------------------------------------------------
# Sentry catches all unhandled exceptions and sends them to apm.yral.com.
# If SENTRY_DSN is not set, this does nothing (safe for local dev).
init_sentry()


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
        await get_pool()
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
    await close_pool()
    logger.info("Shutdown complete")


# ---------------------------------------------------------------------------
# CREATE THE FASTAPI APP
# ---------------------------------------------------------------------------
# This is the "app" variable that uvicorn looks for.
# lifespan=lifespan tells FastAPI to run our startup/shutdown logic.
app = FastAPI(
    title=config.APP_NAME,
    version=config.APP_VERSION,
    lifespan=lifespan,
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# AUTH TEST ENDPOINT (temporary — helps verify JWT validation works)
# ---------------------------------------------------------------------------

@app.get("/api/v1/auth/me")
async def auth_me(request: Request):
    """Test endpoint to verify JWT authentication is working."""
    user_id = get_current_user(request)
    return {"user_id": user_id}


# ---------------------------------------------------------------------------
# ROUTE REGISTRATION
# ---------------------------------------------------------------------------
# Each route module handles one area of the API. We import them and register
# them with the app so FastAPI knows about all endpoints.
# ---------------------------------------------------------------------------
from routes.health import router as health_router
from routes.influencers import router as influencers_router
from routes.chat_v1 import router as chat_router

app.include_router(health_router)
app.include_router(influencers_router)
app.include_router(chat_router)
