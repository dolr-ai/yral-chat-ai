#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# incremental-sync.sh — Sync new data from old Rust DB to our Patroni DB.
#
# Runs INSIDE the Patroni container (which has psql).
# Uses temp FILES (not pipes) to avoid session/temp-table issues.
#
# ENV VARS REQUIRED:
#   OLD_DB_URL — connection string to old Rust service Postgres
#   NEW_DB_URL — connection string to our Patroni leader
# ---------------------------------------------------------------------------
set -euo pipefail

STATE_FILE="/tmp/yral-chat-sync-state"
TMPDIR="/tmp/sync-data"
mkdir -p "$TMPDIR"
START=$(date +%s)

# Last sync timestamp (default: first migration date)
LAST_SYNC="2026-04-15T00:00:00"
[ -f "$STATE_FILE" ] && LAST_SYNC=$(cat "$STATE_FILE")

echo "[sync] Since: $LAST_SYNC"

# Capture current time BEFORE querying (so we don't miss rows)
SYNC_POINT=$(psql "$OLD_DB_URL" -t -c "SELECT NOW()::text" | tr -d ' ')

# Count what needs syncing
INF=$(psql "$OLD_DB_URL" -t -c "SELECT COUNT(*) FROM ai_influencers WHERE created_at > '$LAST_SYNC' OR updated_at > '$LAST_SYNC'" | tr -d ' ')
CONV=$(psql "$OLD_DB_URL" -t -c "SELECT COUNT(*) FROM conversations WHERE created_at > '$LAST_SYNC'" | tr -d ' ')
MSG=$(psql "$OLD_DB_URL" -t -c "SELECT COUNT(*) FROM messages WHERE created_at > '$LAST_SYNC'" | tr -d ' ')

echo "[sync] To sync: $INF influencers, $CONV conversations, $MSG messages"

if [ "$INF" = "0" ] && [ "$CONV" = "0" ] && [ "$MSG" = "0" ]; then
    echo "$SYNC_POINT" > "$STATE_FILE"
    echo "[sync] Nothing to sync. Done."
    exit 0
fi

# ---- INFLUENCERS (upsert — catches ban/unban) ----
if [ "${INF:-0}" -gt 0 ] 2>/dev/null; then
    psql "$OLD_DB_URL" -c "\COPY (
        SELECT id,name,display_name,avatar_url,description,category,
               system_instructions,personality_traits,initial_greeting,
               suggested_messages,is_active,is_nsfw,parent_principal_id,
               source,created_at,updated_at,metadata
        FROM ai_influencers
        WHERE created_at > '$LAST_SYNC' OR updated_at > '$LAST_SYNC'
    ) TO '$TMPDIR/inf.csv' WITH CSV"

    psql "$NEW_DB_URL" <<SQL
CREATE UNLOGGED TABLE IF NOT EXISTS _si (LIKE ai_influencers INCLUDING DEFAULTS);
TRUNCATE _si;
\COPY _si FROM '$TMPDIR/inf.csv' WITH CSV
INSERT INTO ai_influencers SELECT * FROM _si
ON CONFLICT (id) DO UPDATE SET
    display_name=EXCLUDED.display_name,
    is_active=EXCLUDED.is_active,
    avatar_url=EXCLUDED.avatar_url,
    description=EXCLUDED.description,
    system_instructions=EXCLUDED.system_instructions,
    updated_at=EXCLUDED.updated_at;
DROP TABLE _si;
SQL
    echo "[sync] Influencers: $INF synced"
fi

# ---- CONVERSATIONS ----
if [ "${CONV:-0}" -gt 0 ] 2>/dev/null; then
    psql "$OLD_DB_URL" -c "\COPY (
        SELECT id,user_id,influencer_id,created_at,updated_at,metadata
        FROM conversations WHERE created_at > '$LAST_SYNC'
    ) TO '$TMPDIR/conv.csv' WITH CSV"

    psql "$NEW_DB_URL" <<SQL
CREATE UNLOGGED TABLE IF NOT EXISTS _sc (
    id VARCHAR(255), user_id VARCHAR(255), influencer_id VARCHAR(255),
    created_at TIMESTAMP, updated_at TIMESTAMP, metadata JSONB
);
TRUNCATE _sc;
\COPY _sc FROM '$TMPDIR/conv.csv' WITH CSV
INSERT INTO conversations (id,user_id,influencer_id,created_at,updated_at,metadata)
SELECT id,user_id,influencer_id,created_at,updated_at,metadata FROM _sc s
WHERE NOT EXISTS (
    SELECT 1 FROM conversations c
    WHERE c.user_id=s.user_id AND c.influencer_id=s.influencer_id
)
ON CONFLICT (id) DO NOTHING;
DROP TABLE _sc;
SQL
    echo "[sync] Conversations: $CONV synced"
fi

# ---- MESSAGES ----
if [ "${MSG:-0}" -gt 0 ] 2>/dev/null; then
    psql "$OLD_DB_URL" -c "\COPY (
        SELECT id,conversation_id,role,content,message_type,media_urls,
               audio_url,audio_duration_seconds,token_count,client_message_id,
               created_at,metadata,status,is_read
        FROM messages WHERE created_at > '$LAST_SYNC'
    ) TO '$TMPDIR/msg.csv' WITH CSV"

    psql "$NEW_DB_URL" <<SQL
ALTER TABLE messages DISABLE TRIGGER trigger_update_conversation_timestamp;

CREATE UNLOGGED TABLE IF NOT EXISTS _sm (
    id VARCHAR(255), conversation_id VARCHAR(255), role VARCHAR(20),
    content TEXT, message_type VARCHAR(20), media_urls JSONB,
    audio_url TEXT, audio_duration_seconds INTEGER, token_count INTEGER,
    client_message_id VARCHAR(255), created_at TIMESTAMP,
    metadata JSONB, status VARCHAR(20), is_read BOOLEAN
);
TRUNCATE _sm;
\COPY _sm FROM '$TMPDIR/msg.csv' WITH CSV

INSERT INTO messages (id,conversation_id,role,content,message_type,
    media_urls,audio_url,audio_duration_seconds,token_count,
    client_message_id,created_at,metadata,status,is_read)
SELECT * FROM _sm
WHERE EXISTS (SELECT 1 FROM conversations WHERE id=_sm.conversation_id)
ON CONFLICT (id) DO NOTHING;

-- Backfill sender_id for new messages
UPDATE messages m SET sender_id=c.user_id FROM conversations c
WHERE m.conversation_id=c.id AND m.role='user' AND m.sender_id IS NULL;
UPDATE messages m SET sender_id=c.influencer_id FROM conversations c
WHERE m.conversation_id=c.id AND m.role='assistant' AND m.sender_id IS NULL;

ALTER TABLE messages ENABLE TRIGGER trigger_update_conversation_timestamp;
DROP TABLE _sm;
SQL
    echo "[sync] Messages: $MSG synced"
fi

# Cleanup temp files
rm -f "$TMPDIR"/*.csv

# Update state
echo "$SYNC_POINT" > "$STATE_FILE"
ELAPSED=$(( $(date +%s) - START ))
echo "[sync] Done in ${ELAPSED}s (next from: $SYNC_POINT)"
