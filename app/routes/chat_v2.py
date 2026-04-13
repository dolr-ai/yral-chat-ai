# ---------------------------------------------------------------------------
# chat_v2.py — V2 conversation listing (bot-aware).
#
# WHAT THIS FILE DOES:
# Provides an enhanced conversation listing endpoint that understands
# whether the caller is a regular user or a bot (AI influencer).
#
# WHY V2?
# V1's /api/v1/chat/conversations always returns conversations from
# the USER's perspective (the human user is always the "caller").
#
# V2 supports TWO perspectives:
# 1. USER view: "Show me all my conversations with AI influencers"
#    → Returns influencer info as the peer
# 2. BOT view: "Show me all conversations with humans chatting with my AI"
#    → Returns user info as the peer (for the "Chat as Human" feature)
#
# The mobile app calls this endpoint when a creator switches to their
# AI influencer profile and opens the Message Inbox.
#
# NOTE ON IC CANISTER INTEGRATION:
# The Rust service calls the Internet Computer's User Info Service canister
# to determine if a principal is a bot or user. For simplicity in the Python
# port, we determine this by checking if the principal exists in the
# ai_influencers table (if it does, it's a bot). This avoids requiring
# the IC agent dependency while achieving the same result.
#
# PORTED FROM: yral-ai-chat/src/routes/chat_v2.rs
# ---------------------------------------------------------------------------

import json
import logging
from datetime import datetime

import httpx

from fastapi import APIRouter, HTTPException, Request, Query

from database import get_pool
from auth import get_current_user
from repositories import influencer_repo, conversation_repo, message_repo
import config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/chat")


# ---------------------------------------------------------------------------
# Helper: Determine caller type (user or bot)
# ---------------------------------------------------------------------------

async def _is_bot(pool, principal_id: str) -> bool:
    """
    Determine if a principal ID belongs to a bot (AI influencer) or a user.

    We check if the principal exists in the ai_influencers table.
    If it does, the caller is a bot. If not, they're a regular user.

    The Rust service calls the IC canister for this, but checking the
    local DB is faster and achieves the same result (every bot has a
    row in ai_influencers with their principal as the ID).
    """
    inf = await influencer_repo.get_by_id(pool, principal_id)
    return inf is not None


# ---------------------------------------------------------------------------
# Helper: Fetch user profiles from metadata server
# ---------------------------------------------------------------------------

async def _fetch_user_profiles(user_ids: list[str]) -> dict[str, dict]:
    """
    Batch-fetch user profiles (usernames + profile pictures) from the
    metadata server.

    The metadata server is a separate YRAL service that stores user
    profile data (name, photo, etc.). We call it to enrich the conversation
    list with user info when a bot is viewing its conversations.

    RETURNS: Dict mapping principal_id -> {username, profile_picture_url}
    """
    if not user_ids or not config.METADATA_URL:
        return {}

    profiles = {uid: {"principal_id": uid, "username": None, "profile_picture_url": None}
                for uid in user_ids}

    try:
        url = f"{config.METADATA_URL.rstrip('/')}/metadata-bulk"
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(url, json={"users": user_ids})
            if response.status_code == 200:
                data = response.json()
                ok_data = data.get("Ok", {})
                if isinstance(ok_data, dict):
                    for principal, meta in ok_data.items():
                        if principal in profiles:
                            username = meta.get("user_name", "")
                            if username and username.strip():
                                profiles[principal]["username"] = username.strip()
    except Exception as e:
        logger.warning(f"Failed to fetch user profiles: {e}")

    return profiles


# ---------------------------------------------------------------------------
# Helper: Format timestamps
# ---------------------------------------------------------------------------

def _format_dt(dt) -> str:
    """Convert a datetime to ISO string."""
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt) if dt else ""


# =========================================================================
# V2 CONVERSATION LISTING
# =========================================================================

@router.get("/conversations")
async def list_conversations_v2(
    request: Request,
    principal: str = Query(..., description="Principal ID (user or bot)"),
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0, ge=0),
    influencer_id: str | None = Query(default=None),
):
    """
    List conversations with bot-awareness (V2).

    This endpoint determines whether the caller is a regular user or a bot,
    and returns the appropriate peer info:

    - USER calling: Returns conversations with influencer info as the peer
    - BOT calling: Returns conversations with user info as the peer

    PARAMETERS:
        principal: The caller's principal ID (required)
        limit: Max conversations to return (default 20, max 100)
        offset: Pagination offset
        influencer_id: Optional filter by influencer

    This is used by the mobile app when a creator switches to their AI
    influencer profile and views the Message Inbox — they see all humans
    who have chatted with their AI.
    """
    get_current_user(request)  # Auth required
    pool = await get_pool()

    # Determine if the caller is a bot or user
    is_bot_caller = await _is_bot(pool, principal)

    if is_bot_caller:
        return await _list_for_bot(pool, principal, limit, offset)
    else:
        return await _list_for_user(pool, principal, influencer_id, limit, offset)


async def _list_for_user(
    pool, user_id: str, influencer_id: str | None,
    limit: int, offset: int,
) -> dict:
    """
    List conversations from a USER's perspective.

    Returns influencer info as the peer for each conversation.
    This is the same view as V1, but with V2's response format
    (includes influencer.is_online field).
    """
    conversations = await conversation_repo.list_by_user(
        pool, user_id, influencer_id, limit, offset,
    )
    total = await conversation_repo.count_by_user(pool, user_id, influencer_id)

    # Batch fetch last messages
    conv_ids = [c["id"] for c in conversations]
    last_messages = await conversation_repo.get_last_messages_batch(pool, conv_ids) if conv_ids else []

    last_msg_map = {}
    for lm in last_messages:
        last_msg_map[lm["conversation_id"]] = {
            "content": lm.get("content") or "",
            "role": lm["role"],
            "created_at": _format_dt(lm["created_at"]),
        }

    formatted = []
    for c in conversations:
        influencer_info = {
            "id": c.get("inf_id") or c.get("influencer_id") or "",
            "name": c.get("inf_name") or "",
            "display_name": c.get("inf_display_name") or "",
            "avatar_url": c.get("inf_avatar_url"),
            "is_online": c.get("inf_is_active") != "discontinued" if c.get("inf_is_active") else True,
        }

        formatted.append({
            "id": c["id"],
            "user_id": c["user_id"],
            "influencer_id": c.get("influencer_id"),
            "influencer": influencer_info,
            "user": None,
            "created_at": _format_dt(c["created_at"]),
            "updated_at": _format_dt(c["updated_at"]),
            "message_count": c.get("message_count", 0),
            "unread_count": c.get("unread_count", 0),
            "last_message": last_msg_map.get(c["id"]),
        })

    return {
        "conversations": formatted,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


async def _list_for_bot(pool, bot_principal: str, limit: int, offset: int) -> dict:
    """
    List conversations from a BOT's perspective.

    The bot_principal IS the influencer_id in the conversations table.
    Returns user info as the peer for each conversation, so the
    creator can see WHO is chatting with their AI influencer.
    """
    conversations = await conversation_repo.list_by_influencer(
        pool, bot_principal, limit, offset,
    )
    total = await conversation_repo.count_by_influencer(pool, bot_principal)

    # Batch fetch last messages
    conv_ids = [c["id"] for c in conversations]
    last_messages = await conversation_repo.get_last_messages_batch(pool, conv_ids) if conv_ids else []

    last_msg_map = {}
    for lm in last_messages:
        last_msg_map[lm["conversation_id"]] = {
            "content": lm.get("content") or "",
            "role": lm["role"],
            "created_at": _format_dt(lm["created_at"]),
        }

    # Collect unique user IDs and batch-fetch their profiles
    unique_user_ids = list(set(c["user_id"] for c in conversations))
    user_profiles = await _fetch_user_profiles(unique_user_ids)

    formatted = []
    for c in conversations:
        user_info = user_profiles.get(c["user_id"], {
            "principal_id": c["user_id"],
            "username": None,
            "profile_picture_url": None,
        })

        formatted.append({
            "id": c["id"],
            "user_id": c["user_id"],
            "influencer_id": c.get("influencer_id") or bot_principal,
            "influencer": None,
            "user": user_info,
            "created_at": _format_dt(c["created_at"]),
            "updated_at": _format_dt(c["updated_at"]),
            "message_count": c.get("message_count", 0),
            "unread_count": c.get("unread_count", 0),
            "last_message": last_msg_map.get(c["id"]),
        })

    return {
        "conversations": formatted,
        "total": total,
        "limit": limit,
        "offset": offset,
    }
