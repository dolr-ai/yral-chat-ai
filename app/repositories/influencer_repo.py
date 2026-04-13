# ---------------------------------------------------------------------------
# influencer_repo.py — Database queries for AI influencers.
#
# WHAT THIS FILE DOES:
# Contains all SQL queries for creating, reading, updating, and deleting
# AI influencers. Each function takes a database pool, runs a query, and
# returns the result as a dictionary (or list of dictionaries).
#
# WHY SEPARATE FROM ROUTES?
# Keeping SQL queries in one place means:
#   1. Easy to find and modify queries
#   2. Routes stay clean (just business logic, no SQL)
#   3. Can swap the database without changing routes
#
# PORTED FROM: yral-ai-chat/src/db/repositories/influencer_repository.rs
# ---------------------------------------------------------------------------

import json
import logging

logger = logging.getLogger(__name__)


def _row_to_dict(row) -> dict:
    """
    Convert an asyncpg Record to a Python dictionary.

    asyncpg returns rows as Record objects (like named tuples).
    We convert them to dicts so they're easy to work with in Python.
    """
    return dict(row)


async def create(pool, influencer: dict) -> dict:
    """
    Create a new AI influencer in the database.

    PARAMETERS:
        pool: asyncpg connection pool
        influencer: dict with all influencer fields

    RETURNS: The created influencer as a dict

    SQL: INSERT INTO ai_influencers (...) VALUES (...) ON CONFLICT DO NOTHING
    """
    await pool.execute(
        """
        INSERT INTO ai_influencers (
            id, name, display_name, avatar_url, description, category,
            system_instructions, personality_traits, initial_greeting,
            suggested_messages, is_active, is_nsfw, parent_principal_id,
            source, metadata
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
        ON CONFLICT (id) DO NOTHING
        """,
        influencer["id"],
        influencer["name"],
        influencer["display_name"],
        influencer.get("avatar_url"),
        influencer.get("description"),
        influencer.get("category"),
        influencer["system_instructions"],
        json.dumps(influencer.get("personality_traits") or {}),
        influencer.get("initial_greeting"),
        json.dumps(influencer.get("suggested_messages") or []),
        influencer.get("is_active", "active"),
        influencer.get("is_nsfw", False),
        influencer.get("parent_principal_id"),
        influencer.get("source"),
        json.dumps(influencer.get("metadata") or {}),
    )
    return await get_by_id(pool, influencer["id"])


async def get_by_id(pool, influencer_id: str) -> dict | None:
    """
    Get a single influencer by their ID.

    RETURNS: Influencer dict, or None if not found.
    """
    row = await pool.fetchrow(
        """
        SELECT id, name, display_name, avatar_url, description, category,
               system_instructions, personality_traits, initial_greeting,
               suggested_messages, is_active, is_nsfw, parent_principal_id,
               source, created_at, updated_at, metadata
        FROM ai_influencers WHERE id = $1
        """,
        influencer_id,
    )
    return _row_to_dict(row) if row else None


async def get_by_name(pool, name: str) -> dict | None:
    """Get an influencer by their URL-friendly name."""
    row = await pool.fetchrow(
        """
        SELECT id, name, display_name, avatar_url, description, category,
               system_instructions, personality_traits, initial_greeting,
               suggested_messages, is_active, is_nsfw, parent_principal_id,
               source, created_at, updated_at, metadata
        FROM ai_influencers WHERE name = $1
        """,
        name,
    )
    return _row_to_dict(row) if row else None


async def get_by_id_or_name(pool, id_or_name: str) -> dict | None:
    """Get an influencer by either ID or name (used by admin endpoints)."""
    row = await pool.fetchrow(
        """
        SELECT id, name, display_name, avatar_url, description, category,
               system_instructions, personality_traits, initial_greeting,
               suggested_messages, is_active, is_nsfw, parent_principal_id,
               source, created_at, updated_at, metadata
        FROM ai_influencers WHERE id = $1 OR name = $1 LIMIT 1
        """,
        id_or_name,
    )
    return _row_to_dict(row) if row else None


async def get_parent_principal(pool, influencer_id: str) -> str | None:
    """
    Get the parent principal ID (the human who created this influencer).

    Used for:
    - Ownership checks (only creator can delete/modify)
    - "Chat as Human" access control
    - Revenue attribution
    """
    row = await pool.fetchrow(
        "SELECT parent_principal_id FROM ai_influencers WHERE id = $1",
        influencer_id,
    )
    if row and row["parent_principal_id"]:
        return row["parent_principal_id"]
    return None


async def get_with_conversation_count(pool, influencer_id: str) -> dict | None:
    """
    Get an influencer with their total conversation count.

    The conversation count shows how many users have chatted with this
    influencer. It's displayed on the influencer's profile page.
    """
    row = await pool.fetchrow(
        """
        SELECT i.id, i.name, i.display_name, i.avatar_url, i.description,
               i.category, i.system_instructions, i.personality_traits,
               i.initial_greeting, i.suggested_messages,
               i.is_active, i.is_nsfw, i.parent_principal_id, i.source,
               i.created_at, i.updated_at, i.metadata,
               COUNT(c.id) as conversation_count
        FROM ai_influencers i
        LEFT JOIN conversations c ON i.id = c.influencer_id
        WHERE i.id = $1
        GROUP BY i.id
        """,
        influencer_id,
    )
    return _row_to_dict(row) if row else None


async def list_all(pool, limit: int = 50, offset: int = 0) -> list[dict]:
    """
    List all active influencers, ordered by status then creation date.

    Active influencers appear first, then "coming soon" ones.
    Discontinued (banned/deleted) influencers are excluded.
    """
    rows = await pool.fetch(
        """
        SELECT id, name, display_name, avatar_url, description, category,
               system_instructions, personality_traits, initial_greeting,
               suggested_messages, is_active, is_nsfw, parent_principal_id,
               source, created_at, updated_at, metadata
        FROM ai_influencers
        WHERE is_active != 'discontinued'
        ORDER BY CASE is_active
            WHEN 'active' THEN 1
            WHEN 'coming_soon' THEN 2
        END, created_at DESC
        LIMIT $1 OFFSET $2
        """,
        limit, offset,
    )
    return [_row_to_dict(r) for r in rows]


async def count_all(pool) -> int:
    """Count all non-discontinued influencers."""
    return await pool.fetchval(
        "SELECT COUNT(*) FROM ai_influencers WHERE is_active != 'discontinued'"
    )


async def list_trending(pool, limit: int = 50, offset: int = 0) -> list[dict]:
    """
    List trending influencers, ordered by message count (popularity).

    Trending = most messages received from users. Only active influencers.
    """
    rows = await pool.fetch(
        """
        SELECT i.id, i.name, i.display_name, i.avatar_url, i.description,
               i.category, i.system_instructions, i.personality_traits,
               i.initial_greeting, i.suggested_messages,
               i.is_active, i.is_nsfw, i.parent_principal_id, i.source,
               i.created_at, i.updated_at, i.metadata,
               (SELECT COUNT(c.id) FROM conversations c
                WHERE c.influencer_id = i.id) as conversation_count,
               (SELECT COUNT(m.id)
                FROM conversations c
                JOIN messages m ON c.id = m.conversation_id
                WHERE c.influencer_id = i.id AND m.role = 'user') as message_count
        FROM ai_influencers i
        WHERE i.is_active = 'active'
        ORDER BY message_count DESC, i.created_at DESC
        LIMIT $1 OFFSET $2
        """,
        limit, offset,
    )
    return [_row_to_dict(r) for r in rows]


async def count_trending(pool) -> int:
    """Count all active influencers (trending pool)."""
    return await pool.fetchval(
        "SELECT COUNT(*) FROM ai_influencers WHERE is_active = 'active'"
    )


async def update_system_prompt(pool, influencer_id: str, instructions: str):
    """Update an influencer's system instructions (personality prompt)."""
    await pool.execute(
        """
        UPDATE ai_influencers
        SET system_instructions = $1, updated_at = NOW()
        WHERE id = $2
        """,
        instructions, influencer_id,
    )


async def soft_delete(pool, influencer_id: str):
    """
    Soft-delete an influencer (mark as discontinued, rename to "Deleted Bot").

    We don't actually DELETE the row because:
    - Existing conversations reference this influencer
    - Revenue history needs the influencer record
    - Users might want to restore it later
    """
    await pool.execute(
        """
        UPDATE ai_influencers
        SET is_active = 'discontinued', display_name = 'Deleted Bot',
            updated_at = NOW()
        WHERE id = $1
        """,
        influencer_id,
    )


async def ban(pool, influencer_id: str):
    """Admin: ban an influencer (mark as discontinued)."""
    await pool.execute(
        """
        UPDATE ai_influencers
        SET is_active = 'discontinued', updated_at = NOW()
        WHERE id = $1
        """,
        influencer_id,
    )


async def unban(pool, influencer_id: str):
    """Admin: unban an influencer (mark as active)."""
    await pool.execute(
        """
        UPDATE ai_influencers
        SET is_active = 'active', updated_at = NOW()
        WHERE id = $1
        """,
        influencer_id,
    )
