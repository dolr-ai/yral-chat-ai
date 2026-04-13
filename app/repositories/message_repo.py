# ---------------------------------------------------------------------------
# message_repo.py — Database queries for chat messages.
#
# WHAT THIS FILE DOES:
# Contains all SQL queries for creating, listing, and managing messages
# within conversations. Messages are the individual chat bubbles you see
# in the chat screen.
#
# KEY CONCEPTS:
#   - Deduplication: The mobile app sends a client_message_id to prevent
#     duplicate messages from network retries.
#   - Ordering: Messages can be fetched in ascending (oldest first) or
#     descending (newest first) order.
#   - Context: The AI needs the last 10 messages as context when generating
#     a response. get_recent_for_context() fetches these.
#
# PORTED FROM: yral-ai-chat/src/db/repositories/message_repository.rs
# ---------------------------------------------------------------------------

import json
import uuid
import logging

logger = logging.getLogger(__name__)


def _row_to_dict(row) -> dict:
    """Convert an asyncpg Record to a Python dictionary."""
    return dict(row)


async def create(
    pool,
    conversation_id: str,
    role: str,
    content: str | None,
    message_type: str,
    media_urls: list[str] | None = None,
    audio_url: str | None = None,
    audio_duration_seconds: int | None = None,
    token_count: int | None = None,
    client_message_id: str | None = None,
    sender_id: str | None = None,
) -> dict:
    """
    Create a new message in a conversation.

    Generates a UUID for the message ID. The conversation's updated_at
    timestamp is automatically bumped by the database trigger.

    PARAMETERS:
        pool: asyncpg connection pool
        conversation_id: which conversation this message belongs to
        role: "user" (from human) or "assistant" (from AI)
        content: the text of the message (can be None for image-only)
        message_type: "text", "multimodal", "image", or "audio"
        media_urls: list of S3 keys for attached images/files
        audio_url: S3 key for voice message audio file
        audio_duration_seconds: how long the audio is
        token_count: AI tokens used (only for assistant messages)
        client_message_id: dedup key from the mobile app
        sender_id: who sent this message (principal ID or influencer ID)

    RETURNS: The created message as a dict
    """
    message_id = str(uuid.uuid4())
    await pool.execute(
        """
        INSERT INTO messages (
            id, conversation_id, role, sender_id, content, message_type,
            media_urls, audio_url, audio_duration_seconds, token_count,
            client_message_id, status, is_read
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 'delivered', FALSE)
        """,
        message_id,
        conversation_id,
        role,
        sender_id,
        content,
        message_type,
        json.dumps(media_urls or []),
        audio_url,
        audio_duration_seconds,
        token_count,
        client_message_id,
    )
    return await get_by_id(pool, message_id)


async def get_by_id(pool, message_id: str) -> dict | None:
    """Get a single message by its ID."""
    row = await pool.fetchrow(
        """
        SELECT id, conversation_id, role, sender_id, content, message_type,
               media_urls, audio_url, audio_duration_seconds, token_count,
               client_message_id, created_at, metadata, status, is_read
        FROM messages WHERE id = $1
        """,
        message_id,
    )
    return _row_to_dict(row) if row else None


async def get_by_client_id(
    pool, conversation_id: str, client_message_id: str,
) -> dict | None:
    """
    Find a message by its client-generated deduplication ID.

    When the mobile app retries sending a message (due to network issues),
    it sends the same client_message_id. We use this to detect the retry
    and return the existing message instead of creating a duplicate.
    """
    row = await pool.fetchrow(
        """
        SELECT id, conversation_id, role, sender_id, content, message_type,
               media_urls, audio_url, audio_duration_seconds, token_count,
               client_message_id, created_at, metadata, status, is_read
        FROM messages
        WHERE conversation_id = $1 AND client_message_id = $2
        """,
        conversation_id, client_message_id,
    )
    return _row_to_dict(row) if row else None


async def get_assistant_reply(pool, message_id: str) -> dict | None:
    """
    Find the AI's reply to a specific user message.

    Used during deduplication: when we find a duplicate user message,
    we also need to find the AI's response to return both together.

    Logic: Find the first assistant message in the same conversation
    that was created AFTER the user message.
    """
    # First, get the original message to know its conversation and timestamp
    original = await get_by_id(pool, message_id)
    if not original:
        return None

    row = await pool.fetchrow(
        """
        SELECT id, conversation_id, role, sender_id, content, message_type,
               media_urls, audio_url, audio_duration_seconds, token_count,
               client_message_id, created_at, metadata, status, is_read
        FROM messages
        WHERE conversation_id = $1 AND role = 'assistant'
              AND created_at >= $2 AND id != $3
        ORDER BY created_at ASC LIMIT 1
        """,
        original["conversation_id"],
        original["created_at"],
        message_id,
    )
    return _row_to_dict(row) if row else None


async def list_by_conversation(
    pool, conversation_id: str,
    limit: int = 50, offset: int = 0, order: str = "desc",
) -> list[dict]:
    """
    List messages in a conversation with pagination.

    PARAMETERS:
        order: "asc" (oldest first) or "desc" (newest first, default)
    """
    order_clause = "ASC" if order.lower() == "asc" else "DESC"
    rows = await pool.fetch(
        f"""
        SELECT id, conversation_id, role, sender_id, content, message_type,
               media_urls, audio_url, audio_duration_seconds, token_count,
               client_message_id, created_at, metadata, status, is_read
        FROM messages
        WHERE conversation_id = $1
        ORDER BY created_at {order_clause}
        LIMIT $2 OFFSET $3
        """,
        conversation_id, limit, offset,
    )
    return [_row_to_dict(r) for r in rows]


async def get_recent_for_context(pool, conversation_id: str, limit: int = 11) -> list[dict]:
    """
    Get the most recent messages for AI context.

    The AI needs conversation history to generate relevant responses.
    We fetch the last 11 messages (we'll exclude the current user message
    to get 10 context messages) in reverse chronological order, then
    reverse them to chronological order for the AI.

    WHY 11? We fetch 11 and filter out the current user message in the
    route handler, leaving 10 context messages.
    """
    rows = await pool.fetch(
        """
        SELECT id, conversation_id, role, sender_id, content, message_type,
               media_urls, audio_url, audio_duration_seconds, token_count,
               client_message_id, created_at, metadata, status, is_read
        FROM messages
        WHERE conversation_id = $1
        ORDER BY created_at DESC
        LIMIT $2
        """,
        conversation_id, limit,
    )
    # Reverse to chronological order (oldest first) for the AI
    return [_row_to_dict(r) for r in reversed(rows)]


async def get_recent_for_conversations_batch(
    pool, conversation_ids: list[str], limit_per_conv: int = 10,
) -> list[dict]:
    """
    Batch-fetch recent messages for multiple conversations.

    Used when listing the inbox — each conversation shows its last
    few messages. Instead of N separate queries, we do one efficient
    query with a window function.
    """
    if not conversation_ids:
        return []
    rows = await pool.fetch(
        """
        WITH RankedMessages AS (
            SELECT id, conversation_id, role, sender_id, content, message_type,
                   media_urls, audio_url, audio_duration_seconds, token_count,
                   client_message_id, created_at, metadata, status, is_read,
                   ROW_NUMBER() OVER (
                       PARTITION BY conversation_id ORDER BY created_at DESC
                   ) as rn
            FROM messages WHERE conversation_id = ANY($1)
        )
        SELECT id, conversation_id, role, sender_id, content, message_type,
               media_urls, audio_url, audio_duration_seconds, token_count,
               client_message_id, created_at, metadata, status, is_read
        FROM RankedMessages
        WHERE rn <= $2
        ORDER BY conversation_id, created_at ASC
        """,
        conversation_ids, limit_per_conv,
    )
    return [_row_to_dict(r) for r in rows]


async def count_by_conversation(pool, conversation_id: str) -> int:
    """Count total messages in a conversation (for pagination)."""
    return await pool.fetchval(
        "SELECT COUNT(*) FROM messages WHERE conversation_id = $1",
        conversation_id,
    )


async def count_unread(pool, conversation_id: str) -> int:
    """
    Count unread assistant messages in a conversation.

    Only ASSISTANT messages count as "unread" from the user's perspective.
    User messages are never "unread" (the user sent them, of course they've
    seen them).
    """
    return await pool.fetchval(
        """
        SELECT COUNT(*) FROM messages
        WHERE conversation_id = $1 AND is_read = FALSE AND role = 'assistant'
        """,
        conversation_id,
    )


async def mark_as_read(pool, conversation_id: str):
    """
    Mark all assistant messages in a conversation as read.

    Called when the user opens a conversation in the app.
    """
    await pool.execute(
        """
        UPDATE messages
        SET is_read = TRUE, status = 'read'
        WHERE conversation_id = $1 AND is_read = FALSE AND role = 'assistant'
        """,
        conversation_id,
    )


async def delete_by_conversation(pool, conversation_id: str) -> int:
    """
    Delete all messages in a conversation. Returns the count of deleted messages.

    Note: This is also handled by ON DELETE CASCADE on the foreign key,
    but having an explicit delete lets us return the count.
    """
    count = await pool.fetchval(
        "SELECT COUNT(*) FROM messages WHERE conversation_id = $1",
        conversation_id,
    )
    if count > 0:
        await pool.execute(
            "DELETE FROM messages WHERE conversation_id = $1",
            conversation_id,
        )
    return count
