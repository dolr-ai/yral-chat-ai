# ---------------------------------------------------------------------------
# auth.py — JWT authentication middleware.
#
# WHAT THIS FILE DOES:
# Every API request (except /health) must include a JWT token in the
# Authorization header. This file validates that token and extracts the
# user's identity (their "principal ID" from the Internet Computer).
#
# HOW THE MOBILE APP SENDS THE TOKEN:
# The mobile app adds this header to every request:
#   Authorization: Bearer eyJhbGciOiJSUzI1NiIs...
#
# The token is a JWT (JSON Web Token) that contains:
#   sub: "user-principal-id"  (the user's identity)
#   iss: "https://auth.yral.com"  (who issued the token)
#   exp: 1715000000  (when the token expires)
#
# IMPORTANT SECURITY NOTE:
# The existing Rust service SKIPS signature validation on the JWT.
# It trusts the issuer claim and only checks that the token is well-formed,
# not expired, and from a known issuer. We replicate this behavior exactly.
# This is intentional — the auth service that issues these tokens is trusted
# infrastructure, and adding signature validation would require distributing
# the signing key to every service.
#
# PORTED FROM: yral-ai-chat/src/middleware/auth.rs
# ---------------------------------------------------------------------------

import jwt  # PyJWT library — decodes JWT tokens
from fastapi import Request, HTTPException

from config import EXPECTED_ISSUERS


def get_current_user(request: Request) -> str:
    """
    Extract and validate the JWT from the request's Authorization header.

    HOW IT WORKS:
    1. Read the Authorization header
    2. Strip the "Bearer " prefix to get the raw JWT token
    3. Decode the JWT (without signature validation — matches Rust service)
    4. Check that the issuer (iss) is one we trust
    5. Check that the subject (sub) is not empty
    6. Return the user's principal ID (the sub claim)

    PARAMETERS:
        request: the incoming HTTP request (FastAPI injects this automatically)

    RETURNS:
        The user's principal ID as a string (e.g., "2vxsx-fae-...")

    RAISES:
        HTTPException(401) if the token is missing, malformed, expired,
        from an untrusted issuer, or has an empty subject.
    """
    # ---------------------------------------------------------------
    # STEP 1: Get the Authorization header
    # ---------------------------------------------------------------
    auth_header = request.headers.get("Authorization")

    # If there's no Authorization header at all, reject the request.
    if not auth_header:
        raise HTTPException(
            status_code=401,
            detail="Missing authorization header",
        )

    # ---------------------------------------------------------------
    # STEP 2: Strip the "Bearer " prefix
    # ---------------------------------------------------------------
    # The header looks like: "Bearer eyJhbGciOi..."
    # We need just the token part: "eyJhbGciOi..."
    # We check both "Bearer " and "bearer " (case-insensitive prefix).
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]  # Skip first 7 characters ("Bearer ")
    elif auth_header.startswith("bearer "):
        token = auth_header[7:]
    else:
        raise HTTPException(
            status_code=401,
            detail="Invalid authorization header format. Expected: Bearer <token>",
        )

    # ---------------------------------------------------------------
    # STEP 3: Decode the JWT
    # ---------------------------------------------------------------
    # algorithms=["RS256", "HS256"] — accept both algorithm types.
    # options={"verify_signature": False} — skip signature validation
    #   (matches the Rust service's insecure_disable_signature_validation).
    # options={"verify_aud": False} — don't check the audience claim
    #   (the Rust service also skips this).
    try:
        payload = jwt.decode(
            token,
            options={
                "verify_signature": False,  # Trust the issuer (matches Rust service)
                "verify_aud": False,        # Don't check audience claim
                "verify_exp": True,         # DO check token expiration
            },
            algorithms=["RS256", "HS256"],
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.DecodeError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")

    # ---------------------------------------------------------------
    # STEP 4: Validate the issuer
    # ---------------------------------------------------------------
    # The "iss" (issuer) claim tells us WHO created this token.
    # We only trust tokens from our own auth services.
    issuer = payload.get("iss", "")
    if issuer not in EXPECTED_ISSUERS:
        raise HTTPException(
            status_code=401,
            detail=f"Invalid token issuer: {issuer}",
        )

    # ---------------------------------------------------------------
    # STEP 5: Validate the subject
    # ---------------------------------------------------------------
    # The "sub" (subject) claim is the user's principal ID.
    # It must not be empty.
    user_id = payload.get("sub", "")
    if not user_id:
        raise HTTPException(
            status_code=401,
            detail="Invalid token: missing sub",
        )

    # ---------------------------------------------------------------
    # STEP 6: Return the user's principal ID
    # ---------------------------------------------------------------
    return user_id
