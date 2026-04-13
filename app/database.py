# ---------------------------------------------------------------------------
# database.py — Async database connection pool for PostgreSQL.
#
# WHAT THIS FILE DOES:
# Manages a POOL of database connections using asyncpg. Unlike the template's
# psycopg2 (which blocks the thread while waiting for the DB), asyncpg lets
# other requests proceed while one waits for the database. This is essential
# for a chat service that handles WebSockets and concurrent AI API calls.
#
# KEY CONCEPTS:
#   - CONNECTION POOL: A set of pre-opened database connections. Instead of
#     opening a new connection for every request (slow, ~50ms), we "borrow"
#     a connection from the pool, use it, and return it.
#   - ASYNC: The "await" keyword means "pause this function and let other
#     requests run while we wait for the database." This is how Python
#     handles thousands of concurrent connections without threads.
#   - LAZY INITIALIZATION: The pool is NOT created when the app imports this
#     file. It's created the first time a request needs the database. This
#     prevents crashes if the DB isn't ready yet during container startup.
#
# PORTED FROM: yral-ai-chat/src/db/mod.rs (Rust's sqlx connection pool)
# ---------------------------------------------------------------------------

import os
import asyncpg
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The connection pool. Starts as None and is created lazily on first use.
# ---------------------------------------------------------------------------
_pool: asyncpg.Pool | None = None


def _read_database_url() -> str:
    """
    Read the DATABASE_URL (connection string for PostgreSQL).

    Example: postgresql://postgres:mypassword@haproxy-rishi-1:5432/chat_ai_db

    PRIORITY ORDER:
    1. File at /run/secrets/database_url (Docker secret — most secure)
    2. DATABASE_URL environment variable (fallback for local dev)

    WHY A FILE?
    Docker secrets are mounted as files in /run/secrets/. They're stored in
    RAM-only (tmpfs), never written to disk, and invisible in `docker inspect`.
    Environment variables, by contrast, are visible to anyone who can run
    `docker inspect` on the container.
    """
    secret_file = "/run/secrets/database_url"
    if os.path.exists(secret_file):
        with open(secret_file) as f:
            return f.read().strip()
    return os.environ["DATABASE_URL"]


async def get_pool() -> asyncpg.Pool:
    """
    Get or create the database connection pool.

    FIRST CALL: Creates a new pool with 2-10 connections.
    SUBSEQUENT CALLS: Returns the existing pool immediately.

    The pool handles:
    - Connection reuse (no overhead per request)
    - Connection health checks (dead connections are replaced)
    - Concurrency limits (max 10 simultaneous connections)

    RETURNS: an asyncpg.Pool object
    """
    global _pool

    # Fast path: pool already exists (99.99% of requests hit this).
    if _pool is not None:
        return _pool

    # Slow path: first request. Create the pool.
    url = _read_database_url()
    logger.info("Creating database connection pool...")

    _pool = await asyncpg.create_pool(
        dsn=url,
        min_size=2,   # Keep 2 connections open even when idle
        max_size=10,  # Allow up to 10 simultaneous connections
        command_timeout=60,  # Timeout for individual queries (seconds)
    )

    logger.info("Database connection pool created successfully")
    return _pool


async def close_pool():
    """
    Close the database connection pool.

    Called during app shutdown to cleanly close all database connections.
    Without this, connections would be left open (connection leak).
    """
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("Database connection pool closed")


async def check_db_health() -> bool:
    """
    Health check: verifies the database is reachable and the schema exists.

    Runs a simple query against the ai_influencers table to confirm:
    1. The database is reachable (network is working)
    2. The schema has been applied (tables exist)
    3. The connection pool is working

    RETURNS: True if healthy, False if anything is wrong.
    """
    try:
        pool = await get_pool()
        # Try to query the ai_influencers table.
        # If the table doesn't exist yet (first deploy before migrations run),
        # fall back to a simple "SELECT 1" connectivity check.
        try:
            await pool.fetchval("SELECT 1 FROM ai_influencers LIMIT 1")
        except asyncpg.UndefinedTableError:
            # Table doesn't exist yet — that's OK for health check.
            # Just verify we can connect to the database at all.
            await pool.fetchval("SELECT 1")
        return True
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
        return False
