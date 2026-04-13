# ---------------------------------------------------------------------------
# websocket_manager.py — WebSocket connection manager.
#
# WHAT THIS FILE DOES:
# Manages all active WebSocket connections and broadcasts real-time events
# to connected users. When an AI responds to a message, we broadcast a
# "new_message" event to the user's WebSocket so their inbox updates
# instantly without refreshing.
#
# HOW IT WORKS:
# 1. When a user opens the app, the mobile app connects a WebSocket to
#    /api/v1/chat/ws/inbox/{user_id}
# 2. We store this connection in an in-memory dictionary (user_id -> list of connections)
# 3. When events happen (new message, read receipt, typing), we look up the
#    user's connections and send the event as JSON
# 4. When the WebSocket disconnects, we remove it from the dictionary
#
# CROSS-NODE DELIVERY:
# With 2 app nodes (rishi-1, rishi-2), a user's WebSocket might be on
# rishi-1 while a message is processed on rishi-2. For now, we use
# in-memory-only delivery (same as the Rust service). Phase 5 of the plan
# describes adding PostgreSQL LISTEN/NOTIFY for cross-node delivery.
#
# PORTED FROM: yral-ai-chat/src/services/websocket.rs
# ---------------------------------------------------------------------------

import json
import asyncio
import logging
from typing import Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connection tracking
# ---------------------------------------------------------------------------
# _connections maps user_id -> list of WebSocket objects.
# A user can have multiple connections (e.g., multiple browser tabs or devices).
# When we broadcast, we send to ALL connections for that user.

_connections: dict[str, list[WebSocket]] = {}
_lock = asyncio.Lock()


async def connect(user_id: str, websocket: WebSocket):
    """
    Register a new WebSocket connection for a user.

    A user can have multiple simultaneous connections (multiple devices,
    multiple tabs). Each gets a copy of every event.
    """
    async with _lock:
        if user_id not in _connections:
            _connections[user_id] = []
        _connections[user_id].append(websocket)
    logger.info(f"WebSocket connected: user={user_id}, total={len(_connections.get(user_id, []))}")


async def disconnect(user_id: str, websocket: WebSocket):
    """
    Remove a WebSocket connection when it disconnects.

    If this was the user's last connection, remove the user from the map entirely.
    """
    async with _lock:
        if user_id in _connections:
            _connections[user_id] = [
                ws for ws in _connections[user_id] if ws is not websocket
            ]
            if not _connections[user_id]:
                del _connections[user_id]
    logger.info(f"WebSocket disconnected: user={user_id}")


async def _send_to_user(user_id: str, message: str):
    """
    Send a JSON message to ALL WebSocket connections for a user.

    If sending fails (connection died), remove that connection.
    This is the core broadcast function — all event types call this.
    """
    if user_id not in _connections:
        return

    dead_connections = []
    for ws in _connections.get(user_id, []):
        try:
            await ws.send_text(message)
        except Exception:
            dead_connections.append(ws)

    # Clean up dead connections
    if dead_connections:
        async with _lock:
            if user_id in _connections:
                _connections[user_id] = [
                    ws for ws in _connections[user_id] if ws not in dead_connections
                ]
                if not _connections[user_id]:
                    del _connections[user_id]


# ---------------------------------------------------------------------------
# Event broadcasters
# ---------------------------------------------------------------------------
# These functions create the specific JSON event shapes that the mobile app
# expects. The event format matches the Rust service exactly.

async def broadcast_new_message(
    user_id: str,
    conversation_id: str,
    message: dict,
    influencer: dict,
    unread_count: int,
):
    """
    Broadcast a new_message event when the AI responds.

    The mobile app uses this to:
    - Update the inbox with the latest message preview
    - Show the new message bubble in the chat screen
    - Update the unread badge count
    """
    event = json.dumps({
        "event": "new_message",
        "data": {
            "conversation_id": conversation_id,
            "message": message,
            "influencer": influencer,
            "unread_count": unread_count,
        }
    })
    await _send_to_user(user_id, event)


async def broadcast_conversation_read(
    user_id: str,
    conversation_id: str,
    read_at: str,
):
    """
    Broadcast a conversation_read event when messages are marked as read.

    The mobile app uses this to:
    - Clear the unread badge on this conversation
    - Update the message status from "delivered" to "read"
    """
    event = json.dumps({
        "event": "conversation_read",
        "data": {
            "conversation_id": conversation_id,
            "unread_count": 0,
            "read_at": read_at,
        }
    })
    await _send_to_user(user_id, event)


async def broadcast_typing_status(
    user_id: str,
    conversation_id: str,
    influencer_id: str,
    is_typing: bool,
):
    """
    Broadcast a typing_status event when the AI is generating a response.

    The mobile app uses this to show/hide the "..." typing indicator
    in the chat screen.

    is_typing=True is sent BEFORE calling the AI.
    is_typing=False is sent AFTER the AI response is saved.
    """
    event = json.dumps({
        "event": "typing_status",
        "data": {
            "conversation_id": conversation_id,
            "influencer_id": influencer_id,
            "is_typing": is_typing,
        }
    })
    await _send_to_user(user_id, event)
