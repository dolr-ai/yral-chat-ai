# ---------------------------------------------------------------------------
# health.py — Health check and status endpoints.
#
# These endpoints are used by Docker, CI, and monitoring systems to verify
# that the service is running and the database is reachable.
# They do NOT require authentication.
# ---------------------------------------------------------------------------

from fastapi import APIRouter, HTTPException
import database
import config

router = APIRouter()


@router.get("/")
async def root():
    """Root endpoint — basic info about the service."""
    return {
        "service": config.APP_NAME,
        "version": config.APP_VERSION,
        "status": "running",
    }


@router.get("/health")
async def health():
    """Health check — returns 200 if DB reachable, 503 if not."""
    if not await database.check_db_health():
        raise HTTPException(
            status_code=503,
            detail={"status": "ERROR", "database": "unreachable"},
        )
    return {"status": "OK", "database": "reachable"}


@router.get("/status")
async def status():
    """Detailed status with service info."""
    db_healthy = await database.check_db_health()
    return {
        "service": config.APP_NAME,
        "version": config.APP_VERSION,
        "environment": config.ENVIRONMENT,
        "database": "reachable" if db_healthy else "unreachable",
        "gemini_model": config.GEMINI_MODEL,
    }
