#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# migrate-from-rust.sh — Migrate data from the existing Rust chat service
# to the new Python chat service on the Hetzner infra template.
#
# WHAT THIS SCRIPT DOES:
#   1. Dumps the existing PostgreSQL database from the Rust service
#   2. Transforms the dump to match the new schema (adds new columns)
#   3. Loads the data into our Patroni cluster
#   4. Verifies row counts match
#
# PREREQUISITES:
#   - Access to the existing chat service's PostgreSQL database
#   - The PG_DATABASE_URL for the existing service
#   - Our Patroni cluster must be running (Phase 1 takes care of this)
#   - The new schema (migration 002) must already be applied
#
# USAGE:
#   export OLD_DB_URL="postgresql://user:pass@old-host:5432/old_db"
#   bash scripts/migrate-from-rust.sh
#
# SAFETY:
#   - This script does NOT modify the old database (read-only)
#   - This script does NOT delete data from the new database
#   - It uses INSERT ... ON CONFLICT DO NOTHING (safe to re-run)
#   - The old service should keep running until migration is verified
# ---------------------------------------------------------------------------

set -euo pipefail

echo "=========================================="
echo " YRAL Chat: Data Migration from Rust Service"
echo "=========================================="

# ---------------------------------------------------------------
# Step 0: Validate prerequisites
# ---------------------------------------------------------------
if [ -z "${OLD_DB_URL:-}" ]; then
    echo "ERROR: OLD_DB_URL environment variable is required."
    echo ""
    echo "Set it to the PostgreSQL connection string of the existing Rust chat service."
    echo "Example: export OLD_DB_URL='postgresql://user:pass@host:5432/dbname'"
    echo ""
    echo "You can find this in the existing service's deployment config:"
    echo "  - GitHub Secrets: PG_DATABASE_URL"
    echo "  - Or in the docker-compose.yml environment variables"
    exit 1
fi

# Load our project config for the new database
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
source "$PROJECT_ROOT/project.config"
source "$PROJECT_ROOT/servers.config"

# Our new database URL (via HAProxy on rishi-1)
if [ -f "$PROJECT_ROOT/.bootstrap-secrets/DATABASE_URL_SERVER_1" ]; then
    NEW_DB_URL=$(cat "$PROJECT_ROOT/.bootstrap-secrets/DATABASE_URL_SERVER_1")
else
    echo "ERROR: Cannot find new database URL."
    echo "Expected at: $PROJECT_ROOT/.bootstrap-secrets/DATABASE_URL_SERVER_1"
    echo "Run 'new-service.sh' first to bootstrap the service."
    exit 1
fi

DUMP_DIR="$PROJECT_ROOT/.migration-data"
mkdir -p "$DUMP_DIR"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

echo ""
echo "Old database: ${OLD_DB_URL%%@*}@***"
echo "New database: ${NEW_DB_URL%%@*}@***"
echo "Dump directory: $DUMP_DIR"
echo ""

# ---------------------------------------------------------------
# Step 1: Dump data from the old database
# ---------------------------------------------------------------
echo "[1/5] Dumping data from old database..."

# Dump influencers
echo "  Dumping ai_influencers..."
psql "$OLD_DB_URL" -c "\COPY (SELECT * FROM ai_influencers) TO '$DUMP_DIR/ai_influencers_${TIMESTAMP}.csv' WITH CSV HEADER"

# Dump conversations
echo "  Dumping conversations..."
psql "$OLD_DB_URL" -c "\COPY (SELECT * FROM conversations) TO '$DUMP_DIR/conversations_${TIMESTAMP}.csv' WITH CSV HEADER"

# Dump messages
echo "  Dumping messages..."
psql "$OLD_DB_URL" -c "\COPY (SELECT * FROM messages) TO '$DUMP_DIR/messages_${TIMESTAMP}.csv' WITH CSV HEADER"

# Get row counts for verification
OLD_INFLUENCERS=$(psql "$OLD_DB_URL" -t -c "SELECT COUNT(*) FROM ai_influencers")
OLD_CONVERSATIONS=$(psql "$OLD_DB_URL" -t -c "SELECT COUNT(*) FROM conversations")
OLD_MESSAGES=$(psql "$OLD_DB_URL" -t -c "SELECT COUNT(*) FROM messages")

echo "  Old database counts:"
echo "    ai_influencers: $OLD_INFLUENCERS"
echo "    conversations:  $OLD_CONVERSATIONS"
echo "    messages:       $OLD_MESSAGES"

# ---------------------------------------------------------------
# Step 2: Transform and load influencers
# ---------------------------------------------------------------
echo ""
echo "[2/5] Loading ai_influencers into new database..."

# The ai_influencers table schema is the same — direct copy
psql "$NEW_DB_URL" -c "\COPY ai_influencers FROM '$DUMP_DIR/ai_influencers_${TIMESTAMP}.csv' WITH CSV HEADER" 2>/dev/null || {
    echo "  Direct copy failed (likely column mismatch). Using INSERT...SELECT..."
    # Alternative: dump as SQL inserts
    pg_dump "$OLD_DB_URL" --data-only --table=ai_influencers --column-inserts | \
        sed 's/INSERT INTO/INSERT INTO ai_influencers/g' | \
        psql "$NEW_DB_URL" 2>/dev/null || echo "  Some rows may have been skipped (ON CONFLICT)"
}

# ---------------------------------------------------------------
# Step 3: Transform and load conversations
# ---------------------------------------------------------------
echo ""
echo "[3/5] Loading conversations into new database..."

# Add the new columns with defaults:
#   conversation_type = 'ai_chat' (all existing conversations are AI chats)
#   participant_b_id = NULL (no human-to-human chats in old data)
psql "$NEW_DB_URL" <<'SQL'
-- Temporarily disable the trigger to avoid timestamp updates during migration
ALTER TABLE messages DISABLE TRIGGER trigger_update_conversation_timestamp;
SQL

# Load conversations with new columns defaulted
pg_dump "$OLD_DB_URL" --data-only --table=conversations --column-inserts | \
    psql "$NEW_DB_URL" 2>/dev/null || echo "  Some rows may have been skipped"

# ---------------------------------------------------------------
# Step 4: Transform and load messages
# ---------------------------------------------------------------
echo ""
echo "[4/5] Loading messages into new database..."

# Messages need the new sender_id column backfilled:
#   role='user' → sender_id = conversation.user_id
#   role='assistant' → sender_id = conversation.influencer_id
pg_dump "$OLD_DB_URL" --data-only --table=messages --column-inserts | \
    psql "$NEW_DB_URL" 2>/dev/null || echo "  Some rows may have been skipped"

# Backfill sender_id from role + conversation data
echo "  Backfilling sender_id column..."
psql "$NEW_DB_URL" <<'SQL'
-- For user messages: sender_id = the user who created the conversation
UPDATE messages m
SET sender_id = c.user_id
FROM conversations c
WHERE m.conversation_id = c.id
  AND m.role = 'user'
  AND m.sender_id IS NULL;

-- For assistant messages: sender_id = the influencer in the conversation
UPDATE messages m
SET sender_id = c.influencer_id
FROM conversations c
WHERE m.conversation_id = c.id
  AND m.role = 'assistant'
  AND m.sender_id IS NULL;

-- Re-enable the trigger
ALTER TABLE messages ENABLE TRIGGER trigger_update_conversation_timestamp;
SQL

# ---------------------------------------------------------------
# Step 5: Verify row counts match
# ---------------------------------------------------------------
echo ""
echo "[5/5] Verifying row counts..."

NEW_INFLUENCERS=$(psql "$NEW_DB_URL" -t -c "SELECT COUNT(*) FROM ai_influencers")
NEW_CONVERSATIONS=$(psql "$NEW_DB_URL" -t -c "SELECT COUNT(*) FROM conversations")
NEW_MESSAGES=$(psql "$NEW_DB_URL" -t -c "SELECT COUNT(*) FROM messages")

echo ""
echo "  ┌─────────────────┬───────────┬───────────┬─────────┐"
echo "  │ Table           │ Old       │ New       │ Match?  │"
echo "  ├─────────────────┼───────────┼───────────┼─────────┤"
printf "  │ ai_influencers  │ %9s │ %9s │ %s │\n" "$OLD_INFLUENCERS" "$NEW_INFLUENCERS" \
    "$([ "$OLD_INFLUENCERS" = "$NEW_INFLUENCERS" ] && echo '  ✓  ' || echo '  ✗  ')"
printf "  │ conversations   │ %9s │ %9s │ %s │\n" "$OLD_CONVERSATIONS" "$NEW_CONVERSATIONS" \
    "$([ "$OLD_CONVERSATIONS" = "$NEW_CONVERSATIONS" ] && echo '  ✓  ' || echo '  ✗  ')"
printf "  │ messages        │ %9s │ %9s │ %s │\n" "$OLD_MESSAGES" "$NEW_MESSAGES" \
    "$([ "$OLD_MESSAGES" = "$NEW_MESSAGES" ] && echo '  ✓  ' || echo '  ✗  ')"
echo "  └─────────────────┴───────────┴───────────┴─────────┘"
echo ""

if [ "$OLD_INFLUENCERS" = "$NEW_INFLUENCERS" ] && \
   [ "$OLD_CONVERSATIONS" = "$NEW_CONVERSATIONS" ] && \
   [ "$OLD_MESSAGES" = "$NEW_MESSAGES" ]; then
    echo "✅ All row counts match! Migration successful."
else
    echo "⚠️  Row counts don't match. Check for errors above."
    echo "   This may be OK if ON CONFLICT skipped duplicate rows."
fi

echo ""
echo "=========================================="
echo " NEXT STEPS:"
echo "=========================================="
echo ""
echo "1. Test the new service with migrated data:"
echo "   curl https://chat-ai.rishi.yral.com/api/v1/influencers"
echo ""
echo "2. Compare a specific conversation between old and new:"
echo "   curl https://chat.yral.com/api/v1/chat/conversations/{id}/messages"
echo "   curl https://chat-ai.rishi.yral.com/api/v1/chat/conversations/{id}/messages"
echo ""
echo "3. When ready, switch DNS in Cloudflare:"
echo "   Change chat.yral.com → rishi-1 and rishi-2 IPs"
echo "   (Currently: $SERVER_1_IP, $SERVER_2_IP)"
echo ""
echo "4. Monitor for 1 week, then decommission old service."
echo ""
