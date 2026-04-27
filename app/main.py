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

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware

import database
from auth import get_current_user
import config
# Reusable Sentry helper from infra/sentry.py — shared across every
# service built from the dolr-ai template. See the init call below.
from infra import init_sentry

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
# Must be called before FastAPI() is created so Sentry can hook the
# framework's request/response lifecycle. We delegate to the shared
# helper in infra/sentry.py (re-exported by infra/__init__.py as
# `init_sentry`) — that helper is the single source of truth for how
# any dolr-ai service initializes Sentry. It:
#
#   - Reads SENTRY_DSN, SENTRY_ENVIRONMENT, SENTRY_RELEASE,
#     SENTRY_TRACES_RATE, SENTRY_PROFILES_RATE from env vars.
#   - Adds FastAPI + Starlette + Logging integrations so exceptions
#     in HTTP handlers and logger.error(...) calls flow to Sentry
#     automatically.
#   - Sets send_default_pii=False (safer default than the inline
#     version had previously — request bodies and query params with
#     user info no longer attach to events by default).
#   - No-ops silently if SENTRY_DSN is empty (local dev friendly).
#
# WHY we stopped using an inline sentry_sdk.init(...) here: the inline
# version drifted from the template's shared helper — it was missing
# the FastApi/Starlette/Logging integrations AND was hardcoded with
# send_default_pii=True, exposing PII in events. Cutting over to
# self-hosted Sentry was the right moment to also realign with the
# helper. See yral-rishi-sentry/PROGRESS.md Phase 6 for context.
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
        await database.get_pool()
        logger.info("Database pool initialized successfully")
    except Exception as e:
        # If we can't connect to the database at startup, log the error
        # but DON'T crash. The health check will report unhealthy, and
        # the pool will retry on the first real request.
        logger.error(f"Failed to initialize database pool at startup: {e}")

    # Start the background task that keeps the influencer_trending_stats
    # materialized view fresh. It does an initial REFRESH (so the empty
    # view from migration 003 gets populated within 1-2 min of app boot),
    # then refreshes CONCURRENTLY every 15 min thereafter. See
    # _trending_stats_refresher() docstring below for the full rationale.
    trending_refresher_task = asyncio.create_task(_trending_stats_refresher())

    # "yield" means: "startup is done, start serving requests."
    # Everything after yield runs when the app is shutting down.
    yield

    # --- SHUTDOWN ---
    logger.info("Shutting down...")
    trending_refresher_task.cancel()
    try:
        await trending_refresher_task
    except asyncio.CancelledError:
        pass
    await database.close_pool()
    logger.info("Shutdown complete")


# ---------------------------------------------------------------------------
# BACKGROUND TASK: refresh the influencer_trending_stats materialized view
# ---------------------------------------------------------------------------
async def _trending_stats_refresher():
    """Keep the influencer_trending_stats materialized view fresh.

    Background task launched by the lifespan startup. Runs forever (or
    until the lifespan shutdown cancels it).

    SCHEDULE:
      - First run: REFRESH (no CONCURRENTLY) — needed because right after
        migration 003 the view exists but has zero rows; a CONCURRENT
        refresh requires existing data to compute the diff against.
        First run holds an exclusive lock on the view for ~30s-2min;
        list_trending requests during that window get an empty list
        (LEFT JOIN + COALESCE 0). Acceptable cold start.
      - Every 15 min thereafter: REFRESH CONCURRENTLY — re-computes
        without blocking concurrent reads.

    BOTH RISHI-1 AND RISHI-2 RUN THIS TASK:
      Each app replica has its own loop. They'll race on the periodic
      refresh — Postgres handles this via row-level locking on the view's
      MV-tracking tables. The losing replica's REFRESH waits for the
      winner, then immediately runs again (no-op-ish — it just sees the
      already-refreshed state). Wastes a few CPU-seconds every 15 min;
      not worth the complexity of an advisory-lock dedup at our scale.

    ON FAILURE:
      Refresh failures are logged at error level (so Sentry catches them)
      but never crash the app. The view will be stale until the next
      successful refresh or a manual `REFRESH MATERIALIZED VIEW
      influencer_trending_stats` from psql.
    """
    REFRESH_INTERVAL_SEC = 15 * 60  # 15 minutes

    # First run: empty view → must be NOT CONCURRENTLY.
    # Wrap in try so a failure here doesn't abort the loop forever.
    try:
        pool = await database.get_pool()
        async with pool.acquire() as conn:
            await conn.execute("REFRESH MATERIALIZED VIEW influencer_trending_stats")
        logger.info("influencer_trending_stats: initial refresh complete")
    except Exception:
        logger.exception("influencer_trending_stats: initial refresh failed (will retry on next interval)")

    # Subsequent runs: CONCURRENTLY so reads aren't blocked.
    while True:
        await asyncio.sleep(REFRESH_INTERVAL_SEC)
        try:
            pool = await database.get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "REFRESH MATERIALIZED VIEW CONCURRENTLY influencer_trending_stats"
                )
            logger.info("influencer_trending_stats: concurrent refresh complete")
        except Exception:
            # Log + continue. Sentry's logging integration sends this
            # to Sentry as an error event so we notice if refreshes
            # silently start failing.
            logger.exception("influencer_trending_stats: concurrent refresh failed")


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
# 4XX VALIDATION CAPTURE TO SENTRY
# ---------------------------------------------------------------------------
# WHY THIS EXISTS:
#   FastAPI's default behavior for body-validation errors (Pydantic
#   schema mismatches) is to raise RequestValidationError → catch it
#   internally → return a 422 JSON response. The exception NEVER
#   propagates out of the framework, so Sentry's FastAPI integration
#   (which only catches escaped exceptions) doesn't see these failures.
#
#   That blind spot bit us 2026-04-27: every "Create AI Influencer"
#   call from the mobile app was 422-ing at /influencers/generate-prompt
#   because mobile sent {"prompt": "..."} but the schema requires
#   {"concept": "..."}. Hundreds of failed calls per hour, NONE of them
#   visible in Sentry. Found via Firebase Crashlytics + chat-ai access
#   logs after the fact.
#
# WHAT THIS HANDLER DOES:
#   On every RequestValidationError (i.e., every 422), capture a
#   warning-level event to Sentry with the request path, method, the
#   validation failures, and the offending body input. Then let
#   FastAPI's default JSON 422 response continue downstream — we don't
#   change the wire response, we just add observability.
#
# DESIGN NOTES:
#   - level="warning" not "error". 422s are bad-client behaviour, not
#     server bugs — won't trip alerting thresholds tuned for real errors.
#   - Group by path+method via fingerprint so a wave of identical bad
#     requests collapses to a single Sentry issue, not 10,000 separate
#     events.
#   - exc.errors() includes the offending input (Pydantic v2). That's
#     exactly what we need to see what mobile / web is sending wrong.
#     Note: send_default_pii=False in init_sentry, so no other request
#     PII (auth headers, query params) attaches by default.
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
import sentry_sdk


@app.exception_handler(RequestValidationError)
async def sentry_capture_validation_error(request: Request, exc: RequestValidationError):
    # Set rich context BEFORE capture so Sentry has it on the event.
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("http.status_code", "422")
        scope.set_tag("http.method", request.method)
        scope.set_tag("http.route", request.url.path)
        # Group all 422s on the same path+method together (collapses
        # noise from a misconfigured client into one issue).
        scope.fingerprint = ["422", request.method, request.url.path]
        scope.set_context(
            "validation",
            {
                "path": str(request.url.path),
                "method": request.method,
                "errors": exc.errors(),
            },
        )
        sentry_sdk.capture_message(
            f"422 {request.method} {request.url.path}",
            level="warning",
        )

    # Preserve FastAPI's default 422 response shape so existing clients
    # (mobile, web, integrations) keep parsing the response identically.
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors()},
    )


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
