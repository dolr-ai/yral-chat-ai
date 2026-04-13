# ---------------------------------------------------------------------------
# conversation_repo.py — Database queries for conversations.
#
# A conversation is a chat thread between a user and an AI influencer
# (or between two humans in the future). This file contains all SQL
# queries for creating, listing, and managing conversations.
#
# PORTED FROM: yral-ai-chat/src/db/repositories/conversation_repository.rs
# ---------------------------------------------------------------------------

import json
import uuid
import logging

logger = logging.getLogger(__name__)


def _row_to_dict(row) -> dict:
    """Convert an asyncpg Record to a Python dictionary."""
    return dict(row)


async def create(pool, user_id: str, influencer_id: str) -> dict:
    """
    Create a new conversation between a user and an AI influencer.

    Generates a UUID for the conversation ID. Returns the created
    conversation joined with influencer info.
    """
    conversation_id = str(uuid.uuid4())
    await pool.execute(
        """
        INSERT INTO conversations (id, user_id, influencer_id)
        VALUES ($1, $2, $3)
        """,
        conversation_id, user_id, influencer_id,
    )
    return await get_by_id(pool, conversation_id)


async def get_by_id(pool, conversation_id: str) -> dict | None:
    """
    Get a conversation by ID, joined with its influencer info.

    Returns influencer name, display_name, avatar_url, and suggested_messages
    so the mobile app can display the conversation header.
    """
    row = await pool.fetchrow(
        """
        SELECT c.id, c.user_id, c.influencer_id, c.created_at, c.updated_at,
               c.metadata, c.conversation_type, c.participant_b_id,
               i.id as inf_id, i.name as inf_name,
               i.display_name as inf_display_name,
               i.avatar_url as inf_avatar_url,
               i.category as inf_category,
               i.suggested_messages as inf_suggested_messages
        FROM conversations c
        LEFT JOIN ai_influencers i ON c.influencer_id = i.id
        WHERE c.id = $1
        """,
        conversation_id,
    )
    return _row_to_dict(row) if row else None


async def get_existing(pool, user_id: str, influencer_id: str) -> dict | None:
    """
    Check if a conversation already exists between this user and influencer.

    This prevents creating duplicate conversations. If one exists, we
    return it instead of creating a new one.
    """
    row = await pool.fetchrow(
        """
        SELECT c.id, c.user_id, c.influencer_id, c.created_at, c.updated_at,
               c.metadata, c.conversation_type, c.participant_b_id,
               i.id as inf_id, i.name as inf_name,
               i.display_name as inf_display_name,
               i.avatar_url as inf_avatar_url,
               i.category as inf_category,
               i.suggested_messages as inf_suggested_messages
        FROM conversations c
        LEFT JOIN ai_influencers i ON c.influencer_id = i.id
        WHERE c.user_id = $1 AND c.influencer_id = $2
        """,
        user_id, influencer_id,
    )
    return _row_to_dict(row) if row else None


async def list_by_user(
    pool, user_id: str, influencer_id: str | None = None,
    limit: int = 20, offset: int = 0,
) -> list[dict]:
    """
    List a user's conversations with message counts and unread counts.

    This is the INBOX query — it powers the Message Inbox screen.
    Results are sorted by most recent activity (updated_at DESC).

    The query excludes:
    - Conversations with discontinued (banned/deleted) influencers
    - Conversations where the "user" is actually a bot (AI influencers
      don't have their own inbox in V1)
    """
    if influencer_id:
        rows = await pool.fetch(
            """
            SELECT c.id, c.user_id, c.influencer_id, c.created_at, c.updated_at,
                   c.metadata, c.conversation_type,
                   i.id as inf_id, i.name as inf_name,
                   i.display_name as inf_display_name,
                   i.avatar_url as inf_avatar_url,
                   i.category as inf_category,
                   i.suggested_messages as inf_suggested_messages,
                   COUNT(m.id) as message_count,
                   (SELECT COUNT(*) FROM messages m2
                    WHERE m2.conversation_id = c.id
                    AND m2.is_read = FALSE AND m2.role = 'assistant') as unread_count
            FROM conversations c
            JOIN ai_influencers i ON c.influencer_id = i.id
            LEFT JOIN messages m ON c.id = m.conversation_id
            WHERE c.user_id = $1 AND c.influencer_id = $2
                  AND i.is_active != 'discontinued'
                  AND c.user_id NOT IN (SELECT id FROM ai_influencers)
            GROUP BY c.id, i.id
            ORDER BY c.updated_at DESC
            LIMIT $3 OFFSET $4
            """,
            user_id, influencer_id, limit, offset,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT c.id, c.user_id, c.influencer_id, c.created_at, c.updated_at,
                   c.metadata, c.conversation_type,
                   i.id as inf_id, i.name as inf_name,
                   i.display_name as inf_display_name,
                   i.avatar_url as inf_avatar_url,
                   i.category as inf_category,
                   i.suggested_messages as inf_suggested_messages,
                   COUNT(m.id) as message_count,
                   (SELECT COUNT(*) FROM messages m2
                    WHERE m2.conversation_id = c.id
                    AND m2.is_read = FALSE AND m2.role = 'assistant') as unread_count
            FROM conversations c
            JOIN ai_influencers i ON c.influencer_id = i.id
            LEFT JOIN messages m ON c.id = m.conversation_id
            WHERE c.user_id = $1
                  AND i.is_active != 'discontinued'
                  AND c.user_id NOT IN (SELECT id FROM ai_influencers)
            GROUP BY c.id, i.id
            ORDER BY c.updated_at DESC
            LIMIT $2 OFFSET $3
            """,
            user_id, limit, offset,
        )
    return [_row_to_dict(r) for r in rows]


async def count_by_user(pool, user_id: str, influencer_id: str | None = None) -> int:
    """Count a user's conversations (for pagination total)."""
    if influencer_id:
        return await pool.fetchval(
            """
            SELECT COUNT(*) FROM conversations c
            JOIN ai_influencers i ON c.influencer_id = i.id
            WHERE c.user_id = $1 AND c.influencer_id = $2
                  AND i.is_active != 'discontinued'
                  AND c.user_id NOT IN (SELECT id FROM ai_influencers)
            """,
            user_id, influencer_id,
        )
    return await pool.fetchval(
        """
        SELECT COUNT(*) FROM conversations c
        JOIN ai_influencers i ON c.influencer_id = i.id
        WHERE c.user_id = $1
              AND i.is_active != 'discontinued'
              AND c.user_id NOT IN (SELECT id FROM ai_influencers)
        """,
        user_id,
    )


async def list_by_influencer(
    pool, influencer_id: str, limit: int = 20, offset: int = 0,
) -> list[dict]:
    """
    List all conversations with a specific influencer (bot owner's view).

    Used by the "Chat as Human" feature — the influencer's creator can
    see all conversations between other users and their AI influencer.
    """
    rows = await pool.fetch(
        """
        SELECT c.id, c.user_id, c.influencer_id, c.created_at, c.updated_at,
               c.metadata, c.conversation_type,
               COUNT(m.id) as message_count,
               (SELECT COUNT(*) FROM messages m2
                WHERE m2.conversation_id = c.id
                AND m2.is_read = FALSE AND m2.role = 'user') as unread_count
        FROM conversations c
        LEFT JOIN messages m ON c.id = m.conversation_id
        WHERE c.influencer_id = $1
        GROUP BY c.id
        ORDER BY c.updated_at DESC
        LIMIT $2 OFFSET $3
        """,
        influencer_id, limit, offset,
    )
    return [_row_to_dict(r) for r in rows]


async def count_by_influencer(pool, influencer_id: str) -> int:
    """Count conversations with a specific influencer."""
    return await pool.fetchval(
        "SELECT COUNT(*) FROM conversations WHERE influencer_id = $1",
        influencer_id,
    )


async def get_last_messages_batch(pool, conversation_ids: list[str]) -> list[dict]:
    """
    Batch-fetch the last message for multiple conversations.

    This is used when listing conversations in the inbox — each row
    shows a preview of the last message. Instead of querying one-by-one,
    we batch all conversation IDs into a single efficient query.
    """
    if not conversation_ids:
        return []
    rows = await pool.fetch(
        """
        SELECT m1.conversation_id, m1.content, m1.role,
               m1.created_at, m1.status, m1.is_read
        FROM messages m1
        INNER JOIN (
            SELECT conversation_id, MAX(created_at) as max_created
            FROM messages
            WHERE conversation_id = ANY($1)
            GROUP BY conversation_id
        ) m2 ON m1.conversation_id = m2.conversation_id
           AND m1.created_at = m2.max_created
        """,
        conversation_ids,
    )
    return [_row_to_dict(r) for r in rows]


async def update_metadata(pool, conversation_id: str, metadata: dict):
    """
    Update the metadata JSON for a conversation.

    Currently used to store extracted memories:
    {"memories": {"user_name": "Rahul", "user_goal": "lose 10kg"}}
    """
    await pool.execute(
        """
        UPDATE conversations
        SET metadata = $1, updated_at = NOW()
        WHERE id = $2
        """,
        json.dumps(metadata), conversation_id,
    )


async def delete(pool, conversation_id: str):
    """
    Delete a conversation and all its messages.

    Messages are deleted automatically via ON DELETE CASCADE in the
    foreign key constraint. We just delete the conversation row.
    """
    await pool.execute(
        "DELETE FROM conversations WHERE id = $1",
        conversation_id,
    )
