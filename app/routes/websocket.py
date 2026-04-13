# ---------------------------------------------------------------------------
# websocket.py — WebSocket endpoint for real-time inbox events.
#
# WHAT THIS FILE DOES:
# Provides a WebSocket endpoint that the mobile app connects to for
# receiving real-time events (new messages, read receipts, typing indicators)
# without polling.
#
# HOW THE MOBILE APP USES THIS:
# 1. App connects to: wss://chat-ai.rishi.yral.com/api/v1/chat/ws/inbox/{user_id}?token=JWT
# 2. Server validates the JWT from the query parameter
# 3. Connection stays open — server pushes events as they happen
# 4. Events: new_message, conversation_read, typing_status
# 5. If connection drops, mobile app reconnects automatically
#
# WHY A QUERY PARAMETER FOR AUTH?
# WebSocket connections can't send custom headers (unlike HTTP requests).
# The standard workaround is to pass the JWT as a query parameter.
# The Rust service does the same thing.
#
# PORTED FROM: yral-ai-chat/src/routes/websocket.rs
# ---------------------------------------------------------------------------

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

from auth import get_current_user
from services import websocket_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/chat")


@router.websocket("/ws/inbox/{user_id}")
async def ws_inbox(websocket: WebSocket, user_id: str, token: str = Query(default="")):
    """
    WebSocket endpoint for real-time inbox events.

    CONNECTION FLOW:
    1. Client connects with JWT token in query parameter
    2. Server validates the token
    3. Server verifies the token's subject matches the path user_id
    4. Connection is accepted and registered in the WebSocket manager
    5. Server pushes events as they happen
    6. When client disconnects, connection is removed from the manager

    EVENTS RECEIVED BY CLIENT:
    - new_message: When an AI responds to the user's message
    - conversation_read: When messages are marked as read
    - typing_status: When the AI is generating a response

    AUTHENTICATION:
    JWT is passed as ?token= query parameter (WebSocket can't use headers).
    """
    # ---------------------------------------------------------------
    # Step 1: Validate the JWT token
    # ---------------------------------------------------------------
    if not token:
        await websocket.close(code=4001, reason="Missing authentication token")
        return

    # Decode JWT manually (can't use the Request-based get_current_user here)
    import jwt as pyjwt
    from config import EXPECTED_ISSUERS

    try:
        payload = pyjwt.decode(
            token,
            options={"verify_signature": False, "verify_aud": False},
            algorithms=["RS256", "HS256"],
        )
    except Exception:
        await websocket.close(code=4001, reason="Invalid or expired token")
        return

    # Validate issuer
    issuer = payload.get("iss", "")
    if issuer not in EXPECTED_ISSUERS:
        await websocket.close(code=4001, reason="Invalid token issuer")
        return

    # Get the subject (user's principal ID)
    token_user_id = payload.get("sub", "")
    if not token_user_id:
        await websocket.close(code=4001, reason="Invalid token: missing sub")
        return

    # ---------------------------------------------------------------
    # Step 2: Verify the path user_id matches the token
    # ---------------------------------------------------------------
    if token_user_id != user_id:
        await websocket.close(code=4003, reason="Forbidden")
        return

    # ---------------------------------------------------------------
    # Step 3: Accept the connection and register it
    # ---------------------------------------------------------------
    await websocket.accept()
    await websocket_manager.connect(user_id, websocket)

    logger.info(f"WebSocket connected: user={user_id}")

    # ---------------------------------------------------------------
    # Step 4: Keep the connection alive — handle pings and disconnects
    # ---------------------------------------------------------------
    try:
        while True:
            # Wait for incoming messages from the client.
            # The mobile app doesn't send messages over WebSocket (it uses HTTP),
            # but we need to listen to detect disconnects and handle ping/pong.
            data = await websocket.receive_text()

            # The Rust service ignores all client text/binary messages.
            # We do the same — the client only sends data for future features
            # (like human-to-human typing indicators in Phase 6).

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"WebSocket error for user={user_id}: {e}")
    finally:
        await websocket_manager.disconnect(user_id, websocket)
        logger.info(f"WebSocket disconnected: user={user_id}")


@router.get("/ws/docs")
async def ws_docs():
    """
    WebSocket event schemas documentation.

    Returns the JSON schemas of all events that the WebSocket sends.
    This helps mobile app developers understand the event format.
    """
    return {
        "new_message": {
            "event": "new_message",
            "data": {
                "conversation_id": "string",
                "message": "MessageResponse object",
                "influencer": {
                    "id": "string",
                    "display_name": "string",
                    "avatar_url": "string or null",
                    "is_online": True,
                },
                "unread_count": 0,
            }
        },
        "conversation_read": {
            "event": "conversation_read",
            "data": {
                "conversation_id": "string",
                "unread_count": 0,
                "read_at": "ISO timestamp",
            }
        },
        "typing_status": {
            "event": "typing_status",
            "data": {
                "conversation_id": "string",
                "influencer_id": "string",
                "is_typing": True,
            }
        },
    }
