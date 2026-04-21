# ---------------------------------------------------------------------------
# human_chat.py — Human-to-human chat endpoints.
#
# WHAT THIS FILE DOES:
# Enables direct messaging between two human users — no AI involved.
# This is a NEW FEATURE that doesn't exist in the original Rust service.
#
# HOW IT DIFFERS FROM AI CHAT:
#   AI Chat:                              Human Chat:
#   1. User sends message                 1. User A sends message
#   2. Save to DB                         2. Save to DB
#   3. Call Gemini → get AI response      3. (NO AI call — instant)
#   4. Save AI response                   4. WebSocket → notify User B
#   5. WebSocket → notify user            5. Push notification → User B
#   6. Return both messages               6. Return user's message only
#
# KEY DESIGN DECISIONS:
# - Uses the SAME conversations and messages tables (unified schema)
# - conversation_type = 'human_chat' distinguishes from AI chat
# - participant_b_id stores the other human's principal ID
# - assistant_message is null in the response (mobile app handles this)
# - Messages have sender_id to track who sent what
#
# ENDPOINTS:
#   POST /api/v1/chat/human/conversations              — Create conversation
#   GET  /api/v1/chat/human/conversations              — List conversations
#   POST /api/v1/chat/human/conversations/{id}/messages — Send message
# ---------------------------------------------------------------------------

import asyncio
import json
import uuid
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, Query

from database import get_pool
from auth import get_current_user
from repositories import message_repo
from services import websocket_manager, push_notifications

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/chat/human")


# ---------------------------------------------------------------------------
# Helper: Format a message for the API response
# ---------------------------------------------------------------------------

def _format_message(msg: dict) -> dict:
    """Format a message DB record for the API response."""
    media_urls = msg.get("media_urls")
    if isinstance(media_urls, str):
        try:
            media_urls = json.loads(media_urls)
        except (json.JSONDecodeError, TypeError):
            media_urls = []
    if media_urls == []:
        media_urls = None

    # Presign S3 keys → HTTP URLs so mobile app can display images
    if media_urls:
        from services import storage
        media_urls = [storage.generate_presigned_url(u) for u in media_urls if u]
        if not any(media_urls):
            media_urls = None

    audio_url = msg.get("audio_url")
    if audio_url and not audio_url.startswith("http"):
        from services import storage
        audio_url = storage.generate_presigned_url(audio_url)

    created_at = msg["created_at"]
    if isinstance(created_at, datetime):
        created_at = created_at.isoformat()

    return {
        "id": msg["id"],
        "conversation_id": msg.get("conversation_id"),
        "role": msg["role"],
        "content": msg.get("content"),
        "message_type": msg["message_type"],
        "media_urls": media_urls,
        "audio_url": audio_url,
        "audio_duration_seconds": msg.get("audio_duration_seconds"),
        "token_count": None,  # No AI tokens in human chat
        "created_at": created_at,
    }


# =========================================================================
# CREATE HUMAN CONVERSATION
# =========================================================================

@router.post("/conversations", status_code=201)
async def create_human_conversation(request: Request):
    """
    Create a human-to-human conversation.

    REQUEST BODY:
        { "participant_id": "other-user-principal-id" }

    If a conversation already exists between these two users,
    returns the existing one (no duplicate created).

    The conversation is stored in the same conversations table as AI chats,
    with conversation_type = 'human_chat' and participant_b_id set.
    """
    user_id = get_current_user(request)
    pool = await get_pool()

    # Parse request body
    body = await request.json()
    participant_id = body.get("participant_id")
    if not participant_id:
        raise HTTPException(status_code=422, detail="participant_id is required")

    if participant_id == user_id:
        raise HTTPException(status_code=422, detail="Cannot create conversation with yourself")

    # Check for existing conversation (in either direction)
    existing = await pool.fetchrow(
        """
        SELECT id, user_id, influencer_id, conversation_type, participant_b_id,
               created_at, updated_at, metadata
        FROM conversations
        WHERE conversation_type = 'human_chat'
          AND ((user_id = $1 AND participant_b_id = $2)
               OR (user_id = $2 AND participant_b_id = $1))
        """,
        user_id, participant_id,
    )

    if existing:
        msg_count = await message_repo.count_by_conversation(pool, existing["id"])
        return {
            "id": existing["id"],
            "user_id": existing["user_id"],
            "conversation_type": "human_chat",
            "participant_b_id": existing["participant_b_id"],
            "created_at": existing["created_at"].isoformat() if isinstance(existing["created_at"], datetime) else str(existing["created_at"]),
            "updated_at": existing["updated_at"].isoformat() if isinstance(existing["updated_at"], datetime) else str(existing["updated_at"]),
            "message_count": msg_count,
        }

    # Create new conversation
    conversation_id = str(uuid.uuid4())
    await pool.execute(
        """
        INSERT INTO conversations (id, user_id, conversation_type, participant_b_id)
        VALUES ($1, $2, 'human_chat', $3)
        """,
        conversation_id, user_id, participant_id,
    )

    return {
        "id": conversation_id,
        "user_id": user_id,
        "conversation_type": "human_chat",
        "participant_b_id": participant_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "message_count": 0,
    }


# =========================================================================
# LIST HUMAN CONVERSATIONS
# =========================================================================

@router.get("/conversations")
async def list_human_conversations(
    request: Request,
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0, ge=0),
):
    """
    List the user's human-to-human conversations.

    Returns conversations where the user is either user_id or participant_b_id.
    Sorted by most recent activity.
    """
    user_id = get_current_user(request)
    pool = await get_pool()

    rows = await pool.fetch(
        """
        SELECT c.id, c.user_id, c.participant_b_id, c.conversation_type,
               c.created_at, c.updated_at, c.metadata,
               COUNT(m.id) as message_count,
               (SELECT COUNT(*) FROM messages m2
                WHERE m2.conversation_id = c.id
                AND m2.is_read = FALSE AND m2.sender_id != $1) as unread_count
        FROM conversations c
        LEFT JOIN messages m ON c.id = m.conversation_id
        WHERE c.conversation_type = 'human_chat'
          AND (c.user_id = $1 OR c.participant_b_id = $1)
        GROUP BY c.id
        ORDER BY c.updated_at DESC
        LIMIT $2 OFFSET $3
        """,
        user_id, limit, offset,
    )

    total = await pool.fetchval(
        """
        SELECT COUNT(*) FROM conversations
        WHERE conversation_type = 'human_chat'
          AND (user_id = $1 OR participant_b_id = $1)
        """,
        user_id,
    )

    conversations = []
    for r in rows:
        # Determine who the "other person" is
        peer_id = r["participant_b_id"] if r["user_id"] == user_id else r["user_id"]

        created_at = r["created_at"]
        if isinstance(created_at, datetime):
            created_at = created_at.isoformat()
        updated_at = r["updated_at"]
        if isinstance(updated_at, datetime):
            updated_at = updated_at.isoformat()

        conversations.append({
            "id": r["id"],
            "user_id": r["user_id"],
            "conversation_type": "human_chat",
            "participant_b_id": r["participant_b_id"],
            "peer_id": peer_id,
            "created_at": created_at,
            "updated_at": updated_at,
            "message_count": r["message_count"],
            "unread_count": r.get("unread_count", 0),
        })

    return {
        "conversations": conversations,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


# =========================================================================
# SEND HUMAN MESSAGE
# =========================================================================

@router.post("/conversations/{conversation_id}/messages")
async def send_human_message(conversation_id: str, request: Request):
    """
    Send a message to another human (no AI involved).

    This is MUCH simpler than the AI chat send_message:
    1. Validate access
    2. Save message to DB
    3. Broadcast via WebSocket to the OTHER person
    4. Send push notification to the OTHER person
    5. Return the message (NO assistant_message)

    REQUEST BODY:
        {
            "content": "Hey!",
            "message_type": "text",
            "media_urls": null,
            "audio_url": null,
            "client_message_id": "optional-dedup-id"
        }
    """
    user_id = get_current_user(request)
    pool = await get_pool()

    # Parse request body
    body = await request.json()
    content = body.get("content")
    message_type = body.get("message_type", "text")
    media_urls = body.get("media_urls")
    audio_url = body.get("audio_url")
    audio_duration_seconds = body.get("audio_duration_seconds")
    client_message_id = body.get("client_message_id")

    # ---------------------------------------------------------------
    # Step 1: Verify conversation exists and user has access
    # ---------------------------------------------------------------
    conv = await pool.fetchrow(
        """
        SELECT id, user_id, participant_b_id, conversation_type
        FROM conversations WHERE id = $1
        """,
        conversation_id,
    )
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conv["conversation_type"] != "human_chat":
        raise HTTPException(status_code=400, detail="Not a human chat conversation")
    if conv["user_id"] != user_id and conv["participant_b_id"] != user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Determine the OTHER person (recipient)
    recipient_id = conv["participant_b_id"] if conv["user_id"] == user_id else conv["user_id"]

    # ---------------------------------------------------------------
    # Step 2: Deduplication check
    # ---------------------------------------------------------------
    if client_message_id:
        existing = await message_repo.get_by_client_id(
            pool, conversation_id, client_message_id,
        )
        if existing:
            return {
                "user_message": _format_message(existing),
                "assistant_message": None,
            }

    # ---------------------------------------------------------------
    # Step 3: Save message
    # ---------------------------------------------------------------
    user_msg = await message_repo.create(
        pool,
        conversation_id=conversation_id,
        role="user",
        content=content,
        message_type=message_type,
        media_urls=media_urls,
        audio_url=audio_url,
        audio_duration_seconds=audio_duration_seconds,
        client_message_id=client_message_id,
        sender_id=user_id,
    )

    # ---------------------------------------------------------------
    # Step 4: Notify the recipient via WebSocket + push notification
    # ---------------------------------------------------------------
    formatted_msg = _format_message(user_msg)

    # WebSocket: broadcast to the RECIPIENT (not the sender)
    asyncio.create_task(websocket_manager.broadcast_new_message(
        user_id=recipient_id,
        conversation_id=conversation_id,
        message=formatted_msg,
        influencer={
            "id": user_id,
            "display_name": user_id[:8] + "...",  # Placeholder until user profiles are fetched
            "avatar_url": None,
            "is_online": True,
        },
        unread_count=await message_repo.count_unread(pool, conversation_id),
    ))

    # Push notification to the recipient
    asyncio.create_task(push_notifications.send_new_message_notification(
        user_id=recipient_id,
        influencer_name="Someone",  # Will be replaced with actual username
        message_content=content or "[Media message]",
        conversation_id=conversation_id,
        influencer_id=user_id,
    ))

    # ---------------------------------------------------------------
    # Step 5: Return the message (NO assistant_message for human chat)
    # ---------------------------------------------------------------
    return {
        "user_message": formatted_msg,
        "assistant_message": None,
    }
