# ---------------------------------------------------------------------------
# chat_v1.py — Core AI chat endpoints.
#
# This file contains the HEART of the YRAL chat service — the endpoints
# that power every conversation between users and AI influencers.
#
# ENDPOINTS:
#   POST /api/v1/chat/conversations                     — Create/get conversation
#   GET  /api/v1/chat/conversations                     — List conversations (inbox)
#   GET  /api/v1/chat/conversations/{id}/messages       — List messages
#   POST /api/v1/chat/conversations/{id}/messages       — Send message + get AI reply
#   POST /api/v1/chat/conversations/{id}/read           — Mark as read
#   DELETE /api/v1/chat/conversations/{id}              — Delete conversation
#
# THE SEND MESSAGE FLOW (the most complex endpoint):
#   1. Validate JWT → get user_id
#   2. Check client_message_id for deduplication
#   3. If audio: transcribe via Gemini
#   4. Save user message to DB
#   5. Fetch last 10 messages for context
#   6. Fetch memories from conversation.metadata
#   7. Enhance system_instructions with memories
#   8. Call Gemini Flash (or OpenRouter if NSFW)
#   9. Save AI response to DB
#   10. Background: extract memories
#   11. Background: send push notification
#   12. Return { user_message, assistant_message }
#
# PORTED FROM: yral-ai-chat/src/routes/chat.rs (942 lines)
# ---------------------------------------------------------------------------

import asyncio
import json
import logging
from datetime import datetime

import httpx
from fastapi import APIRouter, HTTPException, Request, Query

import config
from database import get_pool
from auth import get_current_user
from repositories import influencer_repo, conversation_repo, message_repo
from services import ai_client, push_notifications, replicate, storage, websocket_manager
from models import (
    CreateConversationRequest, GenerateImageRequest, SendMessageRequest,
    SendMessageResponse, ChatMessage, ConversationResponse,
    ConversationsListResponse, ConversationInfluencer,
    ConversationLastMessage, ConversationMessagesResponse,
    DeleteConversationResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/chat")


# ---------------------------------------------------------------------------
# Helper: Format a DB message row into the mobile app's expected JSON
# ---------------------------------------------------------------------------

def _format_message(msg: dict) -> dict:
    """
    Format a message DB record for the API response.

    Handles JSONB parsing and datetime formatting to match the mobile
    app's ChatMessageDto.kt exactly.
    """
    # Parse media_urls from JSONB (asyncpg may return string or list)
    media_urls = msg.get("media_urls")
    if isinstance(media_urls, str):
        try:
            media_urls = json.loads(media_urls)
        except (json.JSONDecodeError, TypeError):
            media_urls = []
    if media_urls == []:
        media_urls = None  # Mobile app expects null, not empty list

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
        "audio_url": msg.get("audio_url"),
        "audio_duration_seconds": msg.get("audio_duration_seconds"),
        "token_count": msg.get("token_count"),
        "created_at": created_at,
    }


def _format_conversation(conv: dict, message_count: int = 0,
                         last_message: dict | None = None,
                         recent_messages: list[dict] | None = None,
                         show_suggestions: bool = False) -> dict:
    """
    Format a conversation DB record for the API response.

    Matches ConversationDto.kt in the mobile app.
    """
    # Parse suggested_messages
    suggested = conv.get("inf_suggested_messages")
    if isinstance(suggested, str):
        try:
            suggested = json.loads(suggested)
        except (json.JSONDecodeError, TypeError):
            suggested = None

    # Only show suggested_messages if conversation has <= 1 message
    if not show_suggestions or message_count > 1:
        suggested = None

    created_at = conv["created_at"]
    if isinstance(created_at, datetime):
        created_at = created_at.isoformat()

    updated_at = conv["updated_at"]
    if isinstance(updated_at, datetime):
        updated_at = updated_at.isoformat()

    influencer = {
        "id": conv.get("inf_id") or conv.get("influencer_id") or "",
        "name": conv.get("inf_name") or "",
        "display_name": conv.get("inf_display_name") or "",
        "avatar_url": conv.get("inf_avatar_url") or "",
        "category": conv.get("inf_category"),
        "suggested_messages": suggested,
    }

    result = {
        "id": conv["id"],
        "user_id": conv["user_id"],
        "influencer": influencer,
        "created_at": created_at,
        "updated_at": updated_at,
        "message_count": message_count,
        "last_message": last_message,
        "recent_messages": recent_messages,
    }
    return result


# ---------------------------------------------------------------------------
# Access control helper
# ---------------------------------------------------------------------------

async def _can_access_conversation(pool, user_id: str, conv: dict) -> bool:
    """
    Check if a user can access a conversation.

    A user can access a conversation if:
    1. They are the user in the conversation (conv.user_id == user_id)
    2. They are the influencer (conv.influencer_id == user_id) — bot accessing
    3. They created the influencer (parent_principal_id == user_id) — "Chat as Human"
    """
    if conv["user_id"] == user_id:
        return True
    if conv.get("influencer_id") == user_id:
        return True
    # Check if user is the bot's creator
    if conv.get("influencer_id"):
        parent = await influencer_repo.get_parent_principal(pool, conv["influencer_id"])
        if parent == user_id:
            return True
    return False


# =========================================================================
# CREATE / GET CONVERSATION
# =========================================================================

@router.post("/conversations", status_code=201)
async def create_conversation(body: CreateConversationRequest, request: Request):
    """
    Create a new conversation with an AI influencer, or return existing one.

    If a conversation already exists between this user and influencer,
    returns the existing conversation (no duplicate created).
    """
    user_id = get_current_user(request)
    pool = await get_pool()

    # Check if influencer exists
    inf = await influencer_repo.get_by_id(pool, body.influencer_id)
    if not inf:
        raise HTTPException(status_code=404, detail="Influencer not found")

    # Check for existing conversation
    existing = await conversation_repo.get_existing(pool, user_id, body.influencer_id)
    if existing:
        # Return existing conversation with message count and recent messages
        msg_count = await message_repo.count_by_conversation(pool, existing["id"])
        recent = await message_repo.get_recent_for_context(pool, existing["id"], 10)
        formatted_recent = [_format_message(m) for m in recent] if recent else None

        return _format_conversation(
            existing, message_count=msg_count,
            recent_messages=formatted_recent,
            show_suggestions=True,
        )

    # Create new conversation
    conv = await conversation_repo.create(pool, user_id, body.influencer_id)

    # If the influencer has an initial greeting, save it as the first message
    if inf.get("initial_greeting"):
        await message_repo.create(
            pool,
            conversation_id=conv["id"],
            role="assistant",
            content=inf["initial_greeting"],
            message_type="text",
            sender_id=body.influencer_id,
        )

    # Fetch the conversation with all joined data
    conv = await conversation_repo.get_by_id(pool, conv["id"])
    msg_count = await message_repo.count_by_conversation(pool, conv["id"])

    return _format_conversation(conv, message_count=msg_count, show_suggestions=True)


# =========================================================================
# LIST CONVERSATIONS (INBOX)
# =========================================================================

@router.get("/conversations")
async def list_conversations(
    request: Request,
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0, ge=0),
    influencer_id: str | None = Query(default=None),
):
    """
    List the user's conversations (the Message Inbox).

    Returns conversations sorted by most recent activity, with:
    - Message count per conversation
    - Last message preview
    - Recent messages (up to 10)
    - Suggested starter messages (only if <= 1 message in conversation)
    """
    user_id = get_current_user(request)
    pool = await get_pool()

    conversations = await conversation_repo.list_by_user(
        pool, user_id, influencer_id, limit, offset,
    )
    total = await conversation_repo.count_by_user(pool, user_id, influencer_id)

    if not conversations:
        return {
            "conversations": [],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    # Batch-fetch last messages and recent messages for all conversations
    conv_ids = [c["id"] for c in conversations]
    last_messages = await conversation_repo.get_last_messages_batch(pool, conv_ids)
    recent_messages = await message_repo.get_recent_for_conversations_batch(pool, conv_ids, 10)

    # Index last messages by conversation_id
    last_msg_map = {}
    for lm in last_messages:
        lm_created = lm["created_at"]
        if isinstance(lm_created, datetime):
            lm_created = lm_created.isoformat()
        last_msg_map[lm["conversation_id"]] = {
            "content": lm.get("content") or "",
            "role": lm["role"],
            "created_at": lm_created,
        }

    # Group recent messages by conversation_id
    recent_map: dict[str, list] = {}
    for rm in recent_messages:
        cid = rm["conversation_id"]
        if cid not in recent_map:
            recent_map[cid] = []
        recent_map[cid].append(_format_message(rm))

    # Format conversations
    formatted = []
    for c in conversations:
        msg_count = c.get("message_count", 0)
        last_msg = last_msg_map.get(c["id"])
        recent = recent_map.get(c["id"])

        formatted.append(_format_conversation(
            c, message_count=msg_count,
            last_message=last_msg,
            recent_messages=recent,
            show_suggestions=True,
        ))

    return {
        "conversations": formatted,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


# =========================================================================
# LIST MESSAGES
# =========================================================================

@router.get("/conversations/{conversation_id}/messages")
async def list_messages(
    conversation_id: str,
    request: Request,
    limit: int = Query(default=50, le=100),
    offset: int = Query(default=0, ge=0),
    order: str = Query(default="desc"),
):
    """
    List messages in a conversation (paginated).

    The 'order' parameter controls sort direction:
    - "desc" (default): newest first (for scrolling up to load older messages)
    - "asc": oldest first (for reading the conversation in order)
    """
    user_id = get_current_user(request)
    pool = await get_pool()

    # Verify access
    conv = await conversation_repo.get_by_id(pool, conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if not await _can_access_conversation(pool, user_id, conv):
        raise HTTPException(status_code=403, detail="Access denied")

    messages = await message_repo.list_by_conversation(
        pool, conversation_id, limit, offset, order,
    )
    total = await message_repo.count_by_conversation(pool, conversation_id)

    return {
        "conversation_id": conversation_id,
        "messages": [_format_message(m) for m in messages],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


# =========================================================================
# SEND MESSAGE (THE MOST COMPLEX ENDPOINT)
# =========================================================================

@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: str, body: SendMessageRequest, request: Request,
):
    """
    Send a message and get an AI response.

    This is the CORE ENDPOINT of the entire chat service. See the flow
    diagram at the top of this file for the 12-step process.
    """
    user_id = get_current_user(request)
    pool = await get_pool()

    # ---------------------------------------------------------------
    # Step 1: Verify conversation exists and user has access
    # ---------------------------------------------------------------
    conv = await conversation_repo.get_by_id(pool, conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if not await _can_access_conversation(pool, user_id, conv):
        raise HTTPException(status_code=403, detail="Access denied")

    # Get the influencer for this conversation
    influencer_id = conv.get("influencer_id")
    if not influencer_id:
        raise HTTPException(status_code=400, detail="Not an AI chat conversation")

    inf = await influencer_repo.get_by_id(pool, influencer_id)
    if not inf:
        raise HTTPException(status_code=404, detail="Influencer not found")

    # ---------------------------------------------------------------
    # Step 2: Deduplication check
    # ---------------------------------------------------------------
    if body.client_message_id:
        existing = await message_repo.get_by_client_id(
            pool, conversation_id, body.client_message_id,
        )
        if existing:
            # Found a duplicate — return the existing message pair
            reply = await message_repo.get_assistant_reply(pool, existing["id"])
            return {
                "user_message": _format_message(existing),
                "assistant_message": _format_message(reply) if reply else None,
            }

    # ---------------------------------------------------------------
    # Step 3: Audio transcription (if voice message)
    # ---------------------------------------------------------------
    content = body.content
    if body.message_type == "audio" and body.audio_url:
        transcription = await ai_client.transcribe_audio(body.audio_url)
        if transcription:
            content = f"[Transcribed: {transcription}]"
        else:
            content = "[Audio message - transcription unavailable]"

    # ---------------------------------------------------------------
    # Step 4: Save user message
    # ---------------------------------------------------------------
    user_msg = await message_repo.create(
        pool,
        conversation_id=conversation_id,
        role="user",
        content=content,
        message_type=body.message_type,
        media_urls=body.media_urls,
        audio_url=body.audio_url,
        audio_duration_seconds=body.audio_duration_seconds,
        client_message_id=body.client_message_id,
        sender_id=user_id,
    )

    # ---------------------------------------------------------------
    # Step 5: Fetch conversation history (last 10 messages for context)
    # ---------------------------------------------------------------
    history = await message_repo.get_recent_for_context(pool, conversation_id, 11)
    # Filter out the message we just saved (it's already the "current" message)
    history = [m for m in history if m["id"] != user_msg["id"]]
    history = history[-10:]  # Keep at most 10

    # ---------------------------------------------------------------
    # Step 6: Fetch memories and enhance system instructions
    # ---------------------------------------------------------------
    metadata = conv.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    elif metadata is None:
        metadata = {}

    memories = metadata.get("memories", {})
    if isinstance(memories, str):
        try:
            memories = json.loads(memories)
        except (json.JSONDecodeError, TypeError):
            memories = {}

    system_instructions = inf.get("system_instructions", "")
    if memories:
        memories_text = "\n".join(f"- {k}: {v}" for k, v in memories.items())
        system_instructions += f"\n\n**MEMORIES:**\n{memories_text}"

    # ---------------------------------------------------------------
    # Step 7: Broadcast typing indicator (START)
    # ---------------------------------------------------------------
    await websocket_manager.broadcast_typing_status(
        user_id=user_id,
        conversation_id=conversation_id,
        influencer_id=influencer_id,
        is_typing=True,
    )

    # ---------------------------------------------------------------
    # Step 8: Call AI model (Gemini or OpenRouter)
    # ---------------------------------------------------------------
    is_nsfw = inf.get("is_nsfw", False)
    response_text, token_count, is_fallback = await ai_client.generate_response(
        system_instructions=system_instructions,
        conversation_history=history,
        user_message=content or "",
        is_nsfw=is_nsfw,
        media_urls=body.media_urls,
    )

    # ---------------------------------------------------------------
    # Step 8b: Broadcast typing indicator (STOP)
    # ---------------------------------------------------------------
    await websocket_manager.broadcast_typing_status(
        user_id=user_id,
        conversation_id=conversation_id,
        influencer_id=influencer_id,
        is_typing=False,
    )

    # ---------------------------------------------------------------
    # Step 9: Save AI response
    # ---------------------------------------------------------------
    assistant_msg = await message_repo.create(
        pool,
        conversation_id=conversation_id,
        role="assistant",
        content=response_text,
        message_type="text",
        token_count=token_count,
        sender_id=influencer_id,
    )

    # ---------------------------------------------------------------
    # Step 10-11: Background tasks (memory extraction + push notification)
    # ---------------------------------------------------------------
    # These run asynchronously — they don't block the API response.
    asyncio.create_task(_background_memory_extraction(
        pool, conversation_id, content or "", response_text,
        memories, is_nsfw,
    ))

    # Broadcast new_message event via WebSocket
    unread_count = await message_repo.count_unread(pool, conversation_id)
    asyncio.create_task(websocket_manager.broadcast_new_message(
        user_id=user_id,
        conversation_id=conversation_id,
        message=_format_message(assistant_msg),
        influencer={
            "id": influencer_id,
            "display_name": inf.get("display_name", ""),
            "avatar_url": inf.get("avatar_url"),
            "is_online": True,
        },
        unread_count=unread_count,
    ))

    asyncio.create_task(push_notifications.send_new_message_notification(
        user_id=user_id,
        influencer_name=inf.get("display_name", "AI"),
        message_content=response_text,
        conversation_id=conversation_id,
        influencer_id=influencer_id,
    ))

    # ---------------------------------------------------------------
    # Step 12: Return both messages
    # ---------------------------------------------------------------
    status_code = 503 if is_fallback else 200
    return SendMessageResponse(
        user_message=ChatMessage(**_format_message(user_msg)),
        assistant_message=ChatMessage(**_format_message(assistant_msg)),
    )


async def _background_memory_extraction(
    pool, conversation_id: str, user_message: str,
    assistant_response: str, existing_memories: dict, is_nsfw: bool,
):
    """
    Background task: extract memories from the conversation.

    This runs AFTER the API response has been sent to the user.
    It uses AI to identify facts about the user (name, goals, etc.)
    and stores them for future conversations.
    """
    try:
        updated_memories = await ai_client.extract_memories(
            user_message, assistant_response, existing_memories, is_nsfw,
        )
        if updated_memories != existing_memories:
            await conversation_repo.update_metadata(
                pool, conversation_id, {"memories": updated_memories},
            )
            logger.info(f"Updated memories for conversation {conversation_id}")
    except Exception as e:
        logger.warning(f"Memory extraction failed (non-fatal): {e}")


# =========================================================================
# GENERATE IMAGE IN CONVERSATION
# =========================================================================
# Ported from Rust `yral-ai-chat/src/routes/chat.rs::generate_image`.
#
# NOTE (2026-04-20): The YRAL mobile client does NOT call this endpoint
# (grepped entire yral-mobile repo — zero matches for /images or
# generateImage). It exists purely for API parity with the old Rust
# service and for any future admin/web/internal tooling. Keeping it
# here unblocks the migration from "100% feature-parity" concern without
# introducing new contract obligations on any active caller.

async def _generate_image_prompt_from_context(pool, conversation_id: str) -> str:
    """
    Ask Gemini to synthesize an image-generation prompt from the last
    ~10 messages of the conversation. Used when the caller doesn't
    supply an explicit prompt.
    """
    messages = await message_repo.list_by_conversation(
        pool, conversation_id, limit=10, offset=0, order="desc",
    )
    messages.reverse()
    context_lines = [
        f"{m['role']}: {m['content']}" for m in messages if m.get("content")
    ]
    context_str = "\n".join(context_lines)

    system = (
        "You are an AI assistant helping to visualize a scene. Based on "
        "the recent conversation, generate a detailed image generation "
        "prompt that captures the current context, action, or requested "
        "visual. Output ONLY the prompt, no other text."
    )
    user = f"Conversation Context:\n{context_str}\n\nGenerate an image prompt:"

    text, _, _ = await ai_client.generate_response(
        system_instructions=system,
        conversation_history=[],
        user_message=user,
        is_nsfw=False,
        media_urls=None,
    )
    return text.strip()


@router.post("/conversations/{conversation_id}/images", status_code=201)
async def generate_conversation_image(
    conversation_id: str,
    body: GenerateImageRequest,
    request: Request,
):
    """
    Generate an AI image inside a conversation, saved as an assistant
    message of type `image`. Uses the influencer's avatar as a reference
    image (via flux-kontext-dev) when available, so the generated scene
    is visually consistent with the character.

    Flow:
      1. Validate Replicate is configured (else 503).
      2. Load + authorize the conversation (404/403).
      3. Load the influencer; reject discontinued bots (403).
      4. Resolve the prompt — use body.prompt if provided, else
         synthesize from conversation context via Gemini.
      5. Call Replicate (with or without reference image).
      6. Download the Replicate result, re-upload to our S3.
      7. Persist as assistant message with media_urls=[s3_key].
      8. Return 201 + the message.

    This is API-parity with the old Rust service; mobile client does not
    currently invoke it (see module-level note above).
    """
    user_id = get_current_user(request)
    pool = await get_pool()

    if not config.REPLICATE_API_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Image generation service not available",
        )

    conv = await conversation_repo.get_by_id(pool, conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if not await _can_access_conversation(pool, user_id, conv):
        raise HTTPException(status_code=403, detail="Not your conversation")

    influencer_id = conv.get("influencer_id")
    if not influencer_id:
        raise HTTPException(status_code=404, detail="Influencer not found")
    inf = await influencer_repo.get_by_id(pool, influencer_id)
    if not inf:
        raise HTTPException(status_code=404, detail="Influencer not found")
    if inf.get("is_active") == "discontinued":
        raise HTTPException(
            status_code=403,
            detail="This bot has been deleted and can no longer generate images.",
        )

    # 1. Determine prompt
    final_prompt = (body.prompt or "").strip()
    if not final_prompt:
        final_prompt = await _generate_image_prompt_from_context(pool, conversation_id)
    logger.info(f"Generating image for conversation {conversation_id}: {final_prompt[:100]}")

    # 2. Resolve influencer avatar to a URL Replicate can fetch (if any)
    avatar_raw = (inf.get("avatar_url") or "").strip()
    input_image_url: str | None = None
    if avatar_raw:
        if avatar_raw.startswith("http"):
            input_image_url = avatar_raw
        else:
            input_image_url = storage.generate_presigned_url(avatar_raw) or None

    # 3. Call Replicate — use reference variant when we have an avatar
    if input_image_url:
        image_url = await replicate.generate_image_with_reference(
            final_prompt, input_image_url, aspect_ratio="9:16",
        )
    else:
        image_url = await replicate.generate_image(final_prompt, aspect_ratio="9:16")

    if not image_url:
        raise HTTPException(
            status_code=503,
            detail="Failed to generate image from upstream provider",
        )

    # 4. Download the generated image, re-upload to our S3 (stable URL + ACL)
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
            resp = await http.get(image_url)
            resp.raise_for_status()
    except Exception as e:
        logger.error(f"Failed to download generated image from {image_url[:80]}: {e}")
        raise HTTPException(status_code=503, detail="Failed to fetch generated image")

    image_bytes = resp.content
    if not image_bytes:
        raise HTTPException(status_code=503, detail="Generated image was empty")
    content_type = (resp.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
    if not content_type.startswith("image/"):
        content_type = "image/jpeg"

    s3_key, _ = await storage.upload(
        user_id=user_id,
        file_bytes=image_bytes,
        file_extension=".jpg",
        content_type=content_type,
    )

    # 5. Save as assistant message of type `image`
    msg = await message_repo.create(
        pool,
        conversation_id=conversation_id,
        role="assistant",
        content="",
        message_type="image",
        media_urls=[s3_key],
        sender_id=influencer_id,
        token_count=0,
    )
    return _format_message(msg)


# =========================================================================
# MARK AS READ
# =========================================================================

@router.post("/conversations/{conversation_id}/read")
async def mark_as_read(conversation_id: str, request: Request):
    """
    Mark all assistant messages in a conversation as read.

    Called by the mobile app when the user opens a conversation.
    Returns the updated unread count (should be 0 after this call).
    """
    user_id = get_current_user(request)
    pool = await get_pool()

    conv = await conversation_repo.get_by_id(pool, conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if not await _can_access_conversation(pool, user_id, conv):
        raise HTTPException(status_code=403, detail="Access denied")

    await message_repo.mark_as_read(pool, conversation_id)
    unread = await message_repo.count_unread(pool, conversation_id)

    # Broadcast read receipt via WebSocket
    from datetime import datetime, timezone
    read_at = datetime.now(timezone.utc).isoformat()
    asyncio.create_task(websocket_manager.broadcast_conversation_read(
        user_id=user_id,
        conversation_id=conversation_id,
        read_at=read_at,
    ))

    return {"unread_count": unread}


# =========================================================================
# DELETE CONVERSATION
# =========================================================================

@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str, request: Request):
    """
    Delete a conversation and all its messages.

    Only the user who created the conversation can delete it.
    """
    user_id = get_current_user(request)
    pool = await get_pool()

    conv = await conversation_repo.get_by_id(pool, conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conv["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Only the conversation creator can delete it")

    # Count messages before deleting (for the response)
    msg_count = await message_repo.delete_by_conversation(pool, conversation_id)
    await conversation_repo.delete(pool, conversation_id)

    return {
        "success": True,
        "message": "Conversation deleted successfully",
        "deleted_conversation_id": conversation_id,
        "deleted_messages_count": msg_count,
    }
