# ---------------------------------------------------------------------------
# influencers.py — API endpoints for managing AI influencers.
#
# WHAT THESE ENDPOINTS DO:
# Create, list, update, and delete AI influencers. These are the AI
# personalities that users chat with on the YRAL app.
#
# ENDPOINTS:
#   GET  /api/v1/influencers            — List all influencers
#   GET  /api/v1/influencers/trending   — List trending influencers
#   GET  /api/v1/influencers/{id}       — Get one influencer
#   POST /api/v1/influencers/create     — Create new influencer
#   POST /api/v1/influencers/generate-prompt — Generate personality from concept
#   POST /api/v1/influencers/validate-and-generate-metadata — Validate + gen metadata
#   PATCH /api/v1/influencers/{id}/system-prompt — Update personality
#   POST /api/v1/influencers/{id}/generate-video-prompt — Generate video prompt
#   DELETE /api/v1/influencers/{id}     — Delete (owner only)
#   POST /api/v1/admin/influencers/{id} — Admin ban
#   POST /api/v1/admin/influencers/{id}/unban — Admin unban
#
# PORTED FROM: yral-ai-chat/src/routes/influencers.rs
# ---------------------------------------------------------------------------

import json
import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, Query, Header
from fastapi.responses import JSONResponse

from database import get_pool
from auth import get_current_user
from repositories import influencer_repo
from services import character_generator, moderation
from models import (
    InfluencerResponse, InfluencersListResponse, InfluencerDetailResponse,
    CreateInfluencerRequest, GeneratePromptRequest, GeneratePromptResponse,
    ValidateAndGenerateRequest, ValidateAndGenerateResponse,
    UpdateSystemPromptRequest, GenerateVideoPromptRequest,
    GenerateVideoPromptResponse,
)
import config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")


# ---------------------------------------------------------------------------
# Helper: Convert a DB row dict to the mobile app's expected JSON shape
# ---------------------------------------------------------------------------

def _format_influencer_response(inf: dict) -> dict:
    """
    Format an influencer DB record for the API response.

    The mobile app expects specific field names and types.
    This function ensures we return the EXACT shape the app needs.
    """
    # Strip moderation guardrails from system_instructions for display
    system_instructions = inf.get("system_instructions", "")
    system_prompt_display = moderation.strip_guardrails(system_instructions) if system_instructions else ""

    return {
        "id": inf["id"],
        "name": inf["name"],
        "display_name": inf["display_name"],
        "avatar_url": inf.get("avatar_url") or "",
        "description": inf.get("description") or "",
        "category": inf.get("category") or "",
        "is_active": inf.get("is_active", "active"),  # String, not bool!
        "parent_principal_id": inf.get("parent_principal_id"),
        "source": inf.get("source"),
        "system_prompt": system_prompt_display,  # Old service calls it system_prompt
        "created_at": inf["created_at"].isoformat() if isinstance(inf["created_at"], datetime) else str(inf["created_at"]),
        "conversation_count": inf.get("conversation_count"),
        "message_count": inf.get("message_count"),
    }


def _format_influencer_detail(inf: dict) -> dict:
    """Format an influencer for the detailed response (includes system_instructions)."""
    # Parse JSONB fields that asyncpg returns as strings
    personality_traits = inf.get("personality_traits")
    if isinstance(personality_traits, str):
        try:
            personality_traits = json.loads(personality_traits)
        except (json.JSONDecodeError, TypeError):
            personality_traits = {}

    suggested_messages = inf.get("suggested_messages")
    if isinstance(suggested_messages, str):
        try:
            suggested_messages = json.loads(suggested_messages)
        except (json.JSONDecodeError, TypeError):
            suggested_messages = []

    metadata = inf.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            metadata = {}

    # Strip moderation guardrails from system_instructions for display
    system_instructions = inf.get("system_instructions", "")
    system_instructions_display = moderation.strip_guardrails(system_instructions)

    return {
        "id": inf["id"],
        "name": inf["name"],
        "display_name": inf["display_name"],
        "avatar_url": inf.get("avatar_url"),
        "description": inf.get("description"),
        "category": inf.get("category"),
        "system_instructions": system_instructions_display,
        "personality_traits": personality_traits,
        "initial_greeting": inf.get("initial_greeting"),
        "suggested_messages": suggested_messages,
        "is_active": inf.get("is_active", "active"),
        "is_nsfw": inf.get("is_nsfw", False),
        "parent_principal_id": inf.get("parent_principal_id"),
        "source": inf.get("source"),
        "created_at": inf["created_at"].isoformat() if isinstance(inf["created_at"], datetime) else str(inf["created_at"]),
        "updated_at": inf["updated_at"].isoformat() if isinstance(inf["updated_at"], datetime) else str(inf["updated_at"]),
        "metadata": metadata,
        "conversation_count": inf.get("conversation_count"),
    }


# =========================================================================
# LIST ENDPOINTS
# =========================================================================

@router.get("/influencers")
async def list_influencers(
    limit: int = Query(default=50, le=100),
    offset: int = Query(default=0, ge=0),
):
    """
    List all active influencers.

    Cached for 5 minutes (Cache-Control header tells the mobile app and CDN
    to reuse the response instead of hitting our server on every scroll).
    """
    try:
        pool = await get_pool()
        influencers = await influencer_repo.list_all(pool, limit, offset)
        total = await influencer_repo.count_all(pool)

        response = JSONResponse(content={
            "influencers": [_format_influencer_response(i) for i in influencers],
            "total": total,
            "limit": limit,
            "offset": offset,
        })
        response.headers["Cache-Control"] = "public, max-age=300"
        return response
    except Exception as e:
        logger.error(f"list_influencers failed: {type(e).__name__}: {e}")
        import sentry_sdk
        sentry_sdk.capture_exception(e)
        raise HTTPException(status_code=500, detail=f"Internal error: {type(e).__name__}: {e}")


@router.get("/influencers/trending")
async def list_trending(
    limit: int = Query(default=50, le=100),
    offset: int = Query(default=0, ge=0),
):
    """
    List trending influencers, ordered by message count (popularity).

    Cached for 5 minutes.
    """
    pool = await get_pool()
    influencers = await influencer_repo.list_trending(pool, limit, offset)
    total = await influencer_repo.count_trending(pool)

    formatted = []
    for i in influencers:
        inf = _format_influencer_response(i)
        inf["message_count"] = i.get("message_count", 0)
        inf["conversation_count"] = i.get("conversation_count", 0)
        formatted.append(inf)

    response = JSONResponse(content={
        "influencers": formatted,
        "total": total,
        "limit": limit,
        "offset": offset,
    })
    response.headers["Cache-Control"] = "public, max-age=300"
    return response


# =========================================================================
# DETAIL ENDPOINT
# =========================================================================

@router.get("/influencers/{influencer_id}")
async def get_influencer(influencer_id: str):
    """Get detailed info about a specific influencer."""
    pool = await get_pool()
    inf = await influencer_repo.get_with_conversation_count(pool, influencer_id)
    if not inf:
        raise HTTPException(status_code=404, detail="Influencer not found")

    response = JSONResponse(content=_format_influencer_detail(inf))
    response.headers["Cache-Control"] = "public, max-age=300"
    return response


# =========================================================================
# CREATION ENDPOINTS
# =========================================================================

@router.post("/influencers/generate-prompt")
async def generate_prompt(body: GeneratePromptRequest, request: Request):
    """
    Generate full system instructions from a short concept.

    Example input:  "a wise astrologer who gives daily guidance"
    Example output: 500 words of detailed personality instructions
    """
    get_current_user(request)  # Auth required

    instructions = await character_generator.generate_system_instructions(body.concept)
    if not instructions:
        raise HTTPException(status_code=500, detail="Failed to generate system instructions")

    return {"system_instructions": instructions}


@router.post("/influencers/validate-and-generate-metadata")
async def validate_and_generate(body: ValidateAndGenerateRequest, request: Request):
    """
    Validate system instructions and generate all metadata.

    Checks safety, then generates: name, display_name, description,
    greeting, starter messages, personality traits, category.
    """
    get_current_user(request)  # Auth required

    result = await character_generator.validate_and_generate_metadata(
        body.concept,  # The field is called 'concept' in our model
    )
    if not result:
        raise HTTPException(status_code=500, detail="Failed to validate and generate metadata")

    return result


@router.post("/influencers/create", status_code=201)
async def create_influencer(body: CreateInfluencerRequest, request: Request):
    """
    Create a new AI influencer.

    Steps:
    1. Validate the request
    2. Check name uniqueness
    3. Append safety guardrails to system instructions
    4. Generate greeting and starter messages if not provided
    5. Save to database
    6. Return the created influencer
    """
    user_id = get_current_user(request)
    pool = await get_pool()

    # Check name uniqueness
    existing = await influencer_repo.get_by_name(pool, body.name)
    if existing:
        raise HTTPException(status_code=409, detail=f"Name '{body.name}' is already taken")

    # Append safety guardrails
    safe_instructions = moderation.with_guardrails(body.system_instructions)

    # Generate greeting and suggestions if not provided
    greeting = body.initial_greeting
    suggestions = body.suggested_messages
    if not greeting or not suggestions:
        gen_greeting, gen_suggestions = await character_generator.generate_initial_greeting(
            body.display_name, body.system_instructions,
        )
        if not greeting:
            greeting = gen_greeting
        if not suggestions:
            suggestions = gen_suggestions

    # Build the influencer record
    influencer_data = {
        "id": body.bot_principal_id,
        "name": body.name,
        "display_name": body.display_name,
        "avatar_url": body.avatar_url,
        "description": body.description,
        "category": body.category,
        "system_instructions": safe_instructions,
        "personality_traits": body.personality_traits,
        "initial_greeting": greeting,
        "suggested_messages": suggestions,
        "is_active": "active",
        "is_nsfw": False,  # Always false — enforced
        "parent_principal_id": user_id,  # Always the authenticated user
        "source": body.source or "user_created",
        "metadata": body.metadata,
    }

    created = await influencer_repo.create(pool, influencer_data)
    if not created:
        raise HTTPException(status_code=500, detail="Failed to create influencer")

    return _format_influencer_detail(created)


# =========================================================================
# UPDATE ENDPOINTS
# =========================================================================

@router.patch("/influencers/{influencer_id}/system-prompt")
async def update_system_prompt(
    influencer_id: str, body: UpdateSystemPromptRequest, request: Request,
):
    """
    Update an influencer's personality (system instructions).

    Only the creator (parent_principal_id) can update.
    """
    user_id = get_current_user(request)
    pool = await get_pool()

    # Check ownership
    inf = await influencer_repo.get_by_id(pool, influencer_id)
    if not inf:
        raise HTTPException(status_code=404, detail="Influencer not found")
    if inf.get("parent_principal_id") != user_id:
        raise HTTPException(status_code=403, detail="Only the creator can update this influencer")

    # Append guardrails and update
    safe_instructions = moderation.with_guardrails(body.system_instructions)
    await influencer_repo.update_system_prompt(pool, influencer_id, safe_instructions)

    updated = await influencer_repo.get_with_conversation_count(pool, influencer_id)
    return _format_influencer_detail(updated)


@router.post("/influencers/{influencer_id}/generate-video-prompt")
async def generate_video_prompt_endpoint(
    influencer_id: str, body: GenerateVideoPromptRequest, request: Request,
):
    """Generate a cinematic video prompt for the LTX video model."""
    get_current_user(request)  # Auth required
    pool = await get_pool()

    inf = await influencer_repo.get_by_id(pool, influencer_id)
    if not inf:
        raise HTTPException(status_code=404, detail="Influencer not found")

    prompt = await character_generator.generate_video_prompt(
        inf["display_name"], inf["system_instructions"],
    )
    if not prompt:
        raise HTTPException(status_code=500, detail="Failed to generate video prompt")

    return {"prompt": prompt}


# =========================================================================
# DELETE ENDPOINTS
# =========================================================================

@router.delete("/influencers/{influencer_id}")
async def delete_influencer(influencer_id: str, request: Request):
    """
    Soft-delete an influencer (owner only).

    The influencer is marked as 'discontinued' and renamed to 'Deleted Bot'.
    Existing conversations are preserved for history.
    """
    user_id = get_current_user(request)
    pool = await get_pool()

    inf = await influencer_repo.get_by_id(pool, influencer_id)
    if not inf:
        raise HTTPException(status_code=404, detail="Influencer not found")
    if inf.get("parent_principal_id") != user_id:
        raise HTTPException(status_code=403, detail="Only the creator can delete this influencer")

    await influencer_repo.soft_delete(pool, influencer_id)
    deleted = await influencer_repo.get_by_id(pool, influencer_id)
    return _format_influencer_detail(deleted)


# =========================================================================
# ADMIN ENDPOINTS
# =========================================================================

@router.post("/admin/influencers/{influencer_id}")
async def admin_ban(
    influencer_id: str,
    x_admin_key: str = Header(None, alias="X-Admin-Key"),
):
    """Admin: ban an influencer (requires X-Admin-Key header)."""
    if not config.ADMIN_KEY or x_admin_key != config.ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Invalid admin key")

    pool = await get_pool()
    inf = await influencer_repo.get_by_id_or_name(pool, influencer_id)
    if not inf:
        raise HTTPException(status_code=404, detail="Influencer not found")

    await influencer_repo.ban(pool, inf["id"])
    updated = await influencer_repo.get_by_id(pool, inf["id"])
    return _format_influencer_detail(updated)


@router.post("/admin/influencers/{influencer_id}/unban")
async def admin_unban(
    influencer_id: str,
    x_admin_key: str = Header(None, alias="X-Admin-Key"),
):
    """Admin: unban an influencer (requires X-Admin-Key header)."""
    if not config.ADMIN_KEY or x_admin_key != config.ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Invalid admin key")

    pool = await get_pool()
    inf = await influencer_repo.get_by_id_or_name(pool, influencer_id)
    if not inf:
        raise HTTPException(status_code=404, detail="Influencer not found")

    await influencer_repo.unban(pool, inf["id"])
    updated = await influencer_repo.get_by_id(pool, inf["id"])
    return _format_influencer_detail(updated)
