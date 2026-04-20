#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# incremental-sync.sh — Sync new data from old Rust chat DB to our Patroni DB.
#
# WHAT THIS DOES:
# Queries the old database for rows created SINCE the last sync, and inserts
# them into our database. Runs every 5 minutes via GitHub Actions during the
# Alpha testing period.
#
# WHY INCREMENTAL (not full dump)?
# The old DB is 3GB. Full pg_dump every 5 min would overload it.
# Incremental sync only processes new rows (~80 per 5-min window) — <5 seconds.
#
# SAFETY:
# - Read-only on the old database (only SELECTs)
# - Uses ON CONFLICT DO NOTHING (safe to re-run unlimited times)
# - Human-to-human chats on our service are NOT touched
# - If it fails, next run catches up automatically
#
# USAGE:
#   export OLD_DB_URL="postgresql://..."
#   export NEW_DB_URL="postgresql://..."
#   bash scripts/incremental-sync.sh
#
# The script stores last sync timestamp in /tmp/yral-chat-sync-state
# (inside the Patroni container where it runs).
# ---------------------------------------------------------------------------

set -euo pipefail

# Validate env vars
if [ -z "${OLD_DB_URL:-}" ] || [ -z "${NEW_DB_URL:-}" ]; then
    echo "ERROR: OLD_DB_URL and NEW_DB_URL must be set"
    exit 1
fi

STATE_FILE="/tmp/yral-chat-sync-state"
START_TIME=$(date +%s)

# Read last sync timestamp (default: our first migration date)
if [ -f "$STATE_FILE" ]; then
    LAST_SYNC=$(cat "$STATE_FILE")
else
    LAST_SYNC="2026-04-15T00:00:00"
fi

echo "[sync] Starting incremental sync (since $LAST_SYNC)"

# Record the current time BEFORE querying (so we don't miss rows created during sync)
SYNC_POINT=$(psql "$OLD_DB_URL" -t -c "SELECT NOW()::text" | tr -d ' ')

# ---------------------------------------------------------------
# Step 1: Sync influencers (new + updated)
# ---------------------------------------------------------------
# Use ON CONFLICT DO UPDATE for influencers because bans/unbans change is_active
INF_COUNT=$(psql "$OLD_DB_URL" -t -c "
    SELECT COUNT(*) FROM ai_influencers
    WHERE created_at > '$LAST_SYNC' OR updated_at > '$LAST_SYNC'
" | tr -d ' ')

if [ "$INF_COUNT" -gt 0 ] 2>/dev/null; then
    echo "[sync] Syncing $INF_COUNT influencers..."
    psql "$OLD_DB_URL" -c "\COPY (
        SELECT id, name, display_name, avatar_url, description, category,
               system_instructions, personality_traits, initial_greeting,
               suggested_messages, is_active, is_nsfw, parent_principal_id,
               source, created_at, updated_at, metadata
        FROM ai_influencers
        WHERE created_at > '$LAST_SYNC' OR updated_at > '$LAST_SYNC'
    ) TO STDOUT WITH CSV" | \
    psql "$NEW_DB_URL" -c "\COPY _sync_influencers FROM STDIN WITH CSV" 2>/dev/null || true

    # Create temp table, load, upsert
    psql "$NEW_DB_URL" <<SQL
        CREATE TEMP TABLE IF NOT EXISTS _sync_influencers (LIKE ai_influencers INCLUDING DEFAULTS);
        DELETE FROM _sync_influencers;
SQL

    psql "$OLD_DB_URL" -c "\COPY (
        SELECT id, name, display_name, avatar_url, description, category,
               system_instructions, personality_traits, initial_greeting,
               suggested_messages, is_active, is_nsfw, parent_principal_id,
               source, created_at, updated_at, metadata
        FROM ai_influencers
        WHERE created_at > '$LAST_SYNC' OR updated_at > '$LAST_SYNC'
    ) TO '/tmp/_sync_inf.csv' WITH CSV" 2>/dev/null

    # Direct INSERT with ON CONFLICT UPDATE for ban/unban changes
    psql "$NEW_DB_URL" <<SQL
        CREATE TEMP TABLE _sync_inf (
            id VARCHAR(255), name VARCHAR(255), display_name VARCHAR(255),
            avatar_url TEXT, description TEXT, category VARCHAR(100),
            system_instructions TEXT, personality_traits JSONB,
            initial_greeting TEXT, suggested_messages JSONB,
            is_active VARCHAR(20), is_nsfw BOOLEAN,
            parent_principal_id VARCHAR(255), source VARCHAR(100),
            created_at TIMESTAMP, updated_at TIMESTAMP, metadata JSONB
        );
SQL

    # Pipe directly between databases (no temp file)
    psql "$OLD_DB_URL" -c "\COPY (
        SELECT id, name, display_name, avatar_url, description, category,
               system_instructions, personality_traits, initial_greeting,
               suggested_messages, is_active, is_nsfw, parent_principal_id,
               source, created_at, updated_at, metadata
        FROM ai_influencers
        WHERE created_at > '$LAST_SYNC' OR updated_at > '$LAST_SYNC'
    ) TO STDOUT WITH CSV" | \
    psql "$NEW_DB_URL" -c "\COPY _sync_inf FROM STDIN WITH CSV"

    psql "$NEW_DB_URL" -c "
        INSERT INTO ai_influencers
        SELECT * FROM _sync_inf
        ON CONFLICT (id) DO UPDATE SET
            display_name = EXCLUDED.display_name,
            is_active = EXCLUDED.is_active,
            avatar_url = EXCLUDED.avatar_url,
            description = EXCLUDED.description,
            system_instructions = EXCLUDED.system_instructions,
            updated_at = EXCLUDED.updated_at;
        DROP TABLE _sync_inf;
    "
    echo "[sync] Influencers: $INF_COUNT synced"
else
    echo "[sync] Influencers: 0 new/updated"
fi

# ---------------------------------------------------------------
# Step 2: Sync conversations (new only)
# ---------------------------------------------------------------
CONV_COUNT=$(psql "$OLD_DB_URL" -t -c "
    SELECT COUNT(*) FROM conversations WHERE created_at > '$LAST_SYNC'
" | tr -d ' ')

if [ "$CONV_COUNT" -gt 0 ] 2>/dev/null; then
    echo "[sync] Syncing $CONV_COUNT conversations..."

    psql "$NEW_DB_URL" -c "
        CREATE TEMP TABLE _sync_conv (
            id VARCHAR(255), user_id VARCHAR(255), influencer_id VARCHAR(255),
            created_at TIMESTAMP, updated_at TIMESTAMP, metadata JSONB
        );
    "

    psql "$OLD_DB_URL" -c "\COPY (
        SELECT id, user_id, influencer_id, created_at, updated_at, metadata
        FROM conversations WHERE created_at > '$LAST_SYNC'
    ) TO STDOUT WITH CSV" | \
    psql "$NEW_DB_URL" -c "\COPY _sync_conv FROM STDIN WITH CSV"

    # Insert with conflict handling for both primary key AND unique index
    psql "$NEW_DB_URL" -c "
        INSERT INTO conversations (id, user_id, influencer_id, created_at, updated_at, metadata)
        SELECT id, user_id, influencer_id, created_at, updated_at, metadata
        FROM _sync_conv s
        WHERE NOT EXISTS (
            SELECT 1 FROM conversations c
            WHERE c.user_id = s.user_id AND c.influencer_id = s.influencer_id
        )
        ON CONFLICT (id) DO NOTHING;
        DROP TABLE _sync_conv;
    "
    echo "[sync] Conversations: $CONV_COUNT synced"
else
    echo "[sync] Conversations: 0 new"
fi

# ---------------------------------------------------------------
# Step 3: Sync messages (new only)
# ---------------------------------------------------------------
MSG_COUNT=$(psql "$OLD_DB_URL" -t -c "
    SELECT COUNT(*) FROM messages WHERE created_at > '$LAST_SYNC'
" | tr -d ' ')

if [ "$MSG_COUNT" -gt 0 ] 2>/dev/null; then
    echo "[sync] Syncing $MSG_COUNT messages..."

    # Disable trigger during bulk insert
    psql "$NEW_DB_URL" -c "ALTER TABLE messages DISABLE TRIGGER trigger_update_conversation_timestamp;" 2>/dev/null || true

    psql "$NEW_DB_URL" -c "
        CREATE TEMP TABLE _sync_msg (
            id VARCHAR(255), conversation_id VARCHAR(255), role VARCHAR(20),
            content TEXT, message_type VARCHAR(20), media_urls JSONB,
            audio_url TEXT, audio_duration_seconds INTEGER, token_count INTEGER,
            client_message_id VARCHAR(255), created_at TIMESTAMP,
            metadata JSONB, status VARCHAR(20), is_read BOOLEAN
        );
    "

    psql "$OLD_DB_URL" -c "\COPY (
        SELECT id, conversation_id, role, content, message_type, media_urls,
               audio_url, audio_duration_seconds, token_count, client_message_id,
               created_at, metadata, status, is_read
        FROM messages WHERE created_at > '$LAST_SYNC'
    ) TO STDOUT WITH CSV" | \
    psql "$NEW_DB_URL" -c "\COPY _sync_msg FROM STDIN WITH CSV"

    # Insert only messages whose conversation exists in our DB
    psql "$NEW_DB_URL" -c "
        INSERT INTO messages (id, conversation_id, role, content, message_type,
            media_urls, audio_url, audio_duration_seconds, token_count,
            client_message_id, created_at, metadata, status, is_read)
        SELECT id, conversation_id, role, content, message_type,
            media_urls, audio_url, audio_duration_seconds, token_count,
            client_message_id, created_at, metadata, status, is_read
        FROM _sync_msg
        WHERE EXISTS (SELECT 1 FROM conversations WHERE id = _sync_msg.conversation_id)
        ON CONFLICT (id) DO NOTHING;
        DROP TABLE _sync_msg;
    "

    # Backfill sender_id for newly synced messages
    psql "$NEW_DB_URL" -c "
        UPDATE messages m SET sender_id = c.user_id
        FROM conversations c
        WHERE m.conversation_id = c.id AND m.role = 'user' AND m.sender_id IS NULL;

        UPDATE messages m SET sender_id = c.influencer_id
        FROM conversations c
        WHERE m.conversation_id = c.id AND m.role = 'assistant' AND m.sender_id IS NULL;
    "

    # Re-enable trigger
    psql "$NEW_DB_URL" -c "ALTER TABLE messages ENABLE TRIGGER trigger_update_conversation_timestamp;" 2>/dev/null || true

    echo "[sync] Messages: $MSG_COUNT synced"
else
    echo "[sync] Messages: 0 new"
fi

# ---------------------------------------------------------------
# Step 4: Update sync state
# ---------------------------------------------------------------
echo "$SYNC_POINT" > "$STATE_FILE"

ELAPSED=$(( $(date +%s) - START_TIME ))
echo "[sync] Done in ${ELAPSED}s (next sync from: $SYNC_POINT)"
