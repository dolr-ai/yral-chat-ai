# ---------------------------------------------------------------------------
# sentry.py — sets up error tracking via Sentry (apm.yral.com).
#
# WHAT IS SENTRY?
# Sentry is an error-tracking service. When your app crashes or throws
# an exception, Sentry captures the error, the stack trace (which line
# of code caused it), and sends it to a dashboard at apm.yral.com.
# You can see all errors across all servers in one place.
#
# HOW TO USE:
#   In app/main.py, just call: init_sentry()
#   If SENTRY_DSN is set → errors are tracked.
#   If SENTRY_DSN is empty → this does nothing (safe for local development).
#
# DEPENDENCIES:
#   - sentry-sdk (installed via requirements.txt)
# ---------------------------------------------------------------------------

# "logging" is Python's built-in logging library.
# We tell Sentry to capture log messages at ERROR level and above.
import logging

# "os" lets us read environment variables (like SENTRY_DSN).
import os

# "re" is Python's regex (text-pattern) library. Used below to find URLs
# embedded inside log messages so we can scrub their query parameters.
import re

# "socket" lets us get the server's hostname (e.g., "rishi-1") so we can
# see which server an error came from in the Sentry dashboard.
import socket

# urllib helpers parse URLs into pieces (scheme, host, path, query, ...) so
# we can rewrite the query part without touching the rest. Used to redact
# sensitive query-string values like ?key=... before they hit Sentry.
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

# The main Sentry SDK — this is what sends errors to apm.yral.com.
import sentry_sdk

# These "integrations" tell Sentry how to hook into specific frameworks:
# - FastApiIntegration: captures errors in FastAPI route handlers
# - LoggingIntegration: captures Python log messages
# - StarletteIntegration: captures errors in Starlette (the library FastAPI is built on)
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration


# ---------------------------------------------------------------------------
# Query-parameter scrubbing for outgoing Sentry events and breadcrumbs.
#
# WHY THIS EXISTS:
# Sentry's httpx integration records every outbound HTTP request as a
# "breadcrumb" — a small log entry attached to error events. By default it
# stores the full URL, including the query string. Some upstream APIs use
# the query string to pass secrets (Google Gemini takes the API key as
# `?key=AQ.Ab8...`). That meant our live Gemini API key was landing in
# every Sentry event, readable by anyone with access to the dashboard.
#
# The hooks below run on every breadcrumb / event before it leaves the
# app, find any URL with a sensitive query key (key, token, api_key, etc.),
# and replace the value with `[REDACTED]`. The host, path, method, status,
# and other debug-useful info are preserved.
#
# Defence in depth: we also scrub URLs that appear inside log message
# strings (httpx writes "POST <full-url> 200 OK" into a log line, which the
# logging integration turns into a breadcrumb whose `message` field has
# the URL again).
# ---------------------------------------------------------------------------

_SENSITIVE_QUERY_KEYS = {
    "key", "api_key", "apikey", "token", "access_token",
    "auth", "secret", "password", "signature",
}

_URL_IN_TEXT_RE = re.compile(r"https?://\S+")


def _redact_url(url: str) -> str:
    """
    Replace values of sensitive query parameters in `url` with `[REDACTED]`.

    Returns the original string unchanged if it isn't a parseable URL or
    has no query string. Never raises — Sentry hooks must not throw.
    """
    if not isinstance(url, str) or "?" not in url:
        return url
    try:
        parsed = urlparse(url)
        if not parsed.query:
            return url
        cleaned = [
            (k, "[REDACTED]" if k.lower() in _SENSITIVE_QUERY_KEYS else v)
            for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        ]
        return urlunparse(parsed._replace(query=urlencode(cleaned)))
    except Exception:
        return url


def _redact_urls_in_text(text: str) -> str:
    """Find any http(s) URLs embedded in a free-form string and redact them."""
    if not isinstance(text, str) or "http" not in text:
        return text
    return _URL_IN_TEXT_RE.sub(lambda m: _redact_url(m.group(0)), text)


def _scrub_breadcrumb(crumb, _hint):
    """Sentry hook: rewrite URLs in HTTP breadcrumbs and message strings."""
    data = crumb.get("data") or {}
    if isinstance(data, dict) and isinstance(data.get("url"), str):
        data["url"] = _redact_url(data["url"])
    msg = crumb.get("message")
    if isinstance(msg, str):
        crumb["message"] = _redact_urls_in_text(msg)
    return crumb


def _scrub_event(event, _hint):
    """Sentry hook: rewrite URLs in the request context and any tags."""
    req = event.get("request")
    if isinstance(req, dict) and isinstance(req.get("url"), str):
        req["url"] = _redact_url(req["url"])

    # Tags can be either a list-of-pairs or a dict. Handle both shapes.
    tags = event.get("tags")
    if isinstance(tags, dict):
        if isinstance(tags.get("url"), str):
            tags["url"] = _redact_url(tags["url"])
    elif isinstance(tags, list):
        for tag in tags:
            if isinstance(tag, list) and len(tag) == 2 and tag[0] == "url" and isinstance(tag[1], str):
                tag[1] = _redact_url(tag[1])

    # Catch any breadcrumbs that slipped past before_breadcrumb (e.g.,
    # constructed inside a different SDK integration).
    bc = event.get("breadcrumbs")
    if isinstance(bc, dict):
        for crumb in bc.get("values", []) or []:
            _scrub_breadcrumb(crumb, None)
    return event


def init_sentry() -> None:
    """
    Initialize Sentry error tracking. Call this once at app startup.

    If the SENTRY_DSN environment variable is not set (or is empty),
    this function does NOTHING — it's a safe no-op for local development.

    "-> None" means this function doesn't return anything.
    """
    # Read the SENTRY_DSN from the environment. The DSN (Data Source Name)
    # is a URL that tells the Sentry SDK WHERE to send errors.
    # .get("SENTRY_DSN", "") means "get the value, or empty string if not set."
    # .strip() removes any whitespace.
    dsn = os.environ.get("SENTRY_DSN", "").strip()

    if not dsn:
        # No DSN configured → Sentry is opt-in. Do nothing.
        # This is the case during local development.
        return

    # Configure the Sentry SDK with all our settings.
    sentry_sdk.init(
        # WHERE to send errors (the project-specific URL from apm.yral.com)
        dsn=dsn,

        # WHICH environment this is (e.g., "production", "staging", "local")
        # This lets you filter errors by environment in the Sentry dashboard.
        environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),

        # WHICH version of the code is running (the git SHA from CI).
        # This lets you see "this error started happening in commit abc123."
        release=os.environ.get("SENTRY_RELEASE"),

        # WHICH server the error came from (e.g., "rishi-1" or "rishi-2").
        # socket.gethostname() returns the container's hostname.
        server_name=socket.gethostname(),

        # Integrations: tell Sentry how to hook into our frameworks.
        integrations=[
            # Capture errors in Starlette (FastAPI's underlying framework).
            # transaction_style="endpoint" means transactions are named by the
            # route (e.g., "GET /") rather than the URL path.
            StarletteIntegration(transaction_style="endpoint"),

            # Same for FastAPI specifically (builds on top of Starlette).
            FastApiIntegration(transaction_style="endpoint"),

            # Capture Python log messages at INFO level and above.
            # Any log.error() call will create a Sentry event.
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
        ],

        # PERFORMANCE MONITORING: what percentage of requests to trace.
        # 1.0 = 100% (trace every request). For high-traffic services, use
        # 0.1 (10%) to control cost. Low-traffic services can afford 100%.
        traces_sample_rate=float(os.environ.get("SENTRY_TRACES_RATE", "1.0")),

        # PROFILING: what percentage of requests to profile (CPU profiling).
        # Shows which functions are slow. Same cost consideration as traces.
        profiles_sample_rate=float(os.environ.get("SENTRY_PROFILES_RATE", "1.0")),

        # PRIVACY: do NOT send personally identifiable information (PII)
        # like IP addresses, cookies, or request headers to Sentry.
        send_default_pii=False,

        # Include the full stack trace in error events (not just the error message).
        # This makes debugging much easier.
        attach_stacktrace=True,

        # Scrub sensitive query-string values (e.g. Gemini ?key=...) from
        # every breadcrumb and event before they're sent to Sentry. See the
        # block at the top of this file for the full reasoning.
        before_breadcrumb=_scrub_breadcrumb,
        before_send=_scrub_event,
    )
