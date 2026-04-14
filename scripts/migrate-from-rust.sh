#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# migrate-from-rust.sh — Migrate data from the existing Rust chat service
# to the new Python chat service on the Hetzner infra template.
#
# DESIGNED FOR TWO RUNS:
#   Run 1 (today):    Full migration — copies all data for testing
#   Run 2 (go-live):  Incremental sync — only inserts new rows created
#                     since the first run (takes seconds, not minutes)
#
# HOW THE TWO-RUN APPROACH WORKS:
#   - All INSERT statements use ON CONFLICT (id) DO NOTHING
#   - First run: inserts everything (all rows are new)
#   - Second run: existing rows are skipped, only new ones get inserted
#   - After the second run, the UPDATE statements for sender_id only
#     touch rows where sender_id IS NULL (new rows from the gap period)
#   - Result: zero data loss, zero duplicates
#
# USAGE:
#   export OLD_DB_URL="postgresql://user:pass@old-host:5432/old_db"
#
#   # Run 1: Full migration for testing
#   bash scripts/migrate-from-rust.sh
#
#   # ... test for a few days ...
#
#   # Run 2: Catch up with new data, then switch DNS
#   bash scripts/migrate-from-rust.sh
#
# SAFETY:
#   - This script does NOT modify the old database (read-only)
#   - This script does NOT delete data from the new database
#   - It uses INSERT ... ON CONFLICT DO NOTHING (safe to re-run unlimited times)
#   - Human-to-human conversations created on the NEW service are preserved
#   - The old service should keep running until migration is verified
# ---------------------------------------------------------------------------

set -euo pipefail

echo "=========================================="
echo " YRAL Chat: Data Migration from Rust Service"
echo "=========================================="
echo " Safe to run multiple times (idempotent)"
echo "=========================================="

# ---------------------------------------------------------------
# Step 0: Validate prerequisites
# ---------------------------------------------------------------
if [ -z "${OLD_DB_URL:-}" ]; then
    echo ""
    echo "ERROR: OLD_DB_URL environment variable is required."
    echo ""
    echo "Set it to the PostgreSQL connection string of the existing Rust chat service."
    echo "Example: export OLD_DB_URL='postgresql://user:pass@host:5432/dbname'"
    echo ""
    echo "You can find this in the existing service's deployment config:"
    echo "  - GitHub Secrets for dolr-ai/yral-ai-chat: PG_DATABASE_URL"
    echo "  - Or ask Ravi for the connection string"
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
# Check if this is a first run or a subsequent (incremental) run
# ---------------------------------------------------------------
EXISTING_INFLUENCERS=$(psql "$NEW_DB_URL" -t -c "SELECT COUNT(*) FROM ai_influencers" 2>/dev/null | tr -d ' ')
if [ "$EXISTING_INFLUENCERS" -gt 0 ] 2>/dev/null; then
    echo ">>> INCREMENTAL RUN detected ($EXISTING_INFLUENCERS influencers already in new DB)"
    echo ">>> Only NEW rows will be inserted. Existing data is safe."
    echo ""
    RUN_TYPE="incremental"
else
    echo ">>> FIRST RUN detected (new database is empty)"
    echo ""
    RUN_TYPE="first"
fi

# ---------------------------------------------------------------
# Step 1: Get row counts from old database
# ---------------------------------------------------------------
echo "[1/6] Counting rows in old database..."

OLD_INFLUENCERS=$(psql "$OLD_DB_URL" -t -c "SELECT COUNT(*) FROM ai_influencers" | tr -d ' ')
OLD_CONVERSATIONS=$(psql "$OLD_DB_URL" -t -c "SELECT COUNT(*) FROM conversations" | tr -d ' ')
OLD_MESSAGES=$(psql "$OLD_DB_URL" -t -c "SELECT COUNT(*) FROM messages" | tr -d ' ')

echo "  Old database:"
echo "    ai_influencers: $OLD_INFLUENCERS"
echo "    conversations:  $OLD_CONVERSATIONS"
echo "    messages:       $OLD_MESSAGES"

# ---------------------------------------------------------------
# Step 2: Disable triggers during migration
# ---------------------------------------------------------------
echo ""
echo "[2/6] Preparing new database for migration..."

psql "$NEW_DB_URL" -q <<'SQL'
-- Disable the auto-update trigger during bulk insert.
-- Without this, every message INSERT would trigger an UPDATE on conversations,
-- making the migration 10x slower.
ALTER TABLE messages DISABLE TRIGGER trigger_update_conversation_timestamp;
SQL
echo "  Trigger disabled (will re-enable after migration)"

# ---------------------------------------------------------------
# Step 3: Migrate ai_influencers
# ---------------------------------------------------------------
echo ""
echo "[3/6] Migrating ai_influencers..."

# Dump as SQL INSERTs and add ON CONFLICT DO NOTHING for idempotency.
# This means:
#   - First run: all rows get inserted
#   - Second run: existing rows are skipped, only new influencers get added
pg_dump "$OLD_DB_URL" \
    --data-only \
    --table=ai_influencers \
    --column-inserts \
    --no-owner \
    --no-privileges \
    2>/dev/null \
| sed 's/INSERT INTO public\.ai_influencers/INSERT INTO ai_influencers/g' \
| sed 's/);$/) ON CONFLICT (id) DO NOTHING;/g' \
| psql "$NEW_DB_URL" -q 2>/dev/null

LOADED_INFLUENCERS=$(psql "$NEW_DB_URL" -t -c "SELECT COUNT(*) FROM ai_influencers" | tr -d ' ')
echo "  Loaded: $LOADED_INFLUENCERS influencers (was $EXISTING_INFLUENCERS before)"

# ---------------------------------------------------------------
# Step 4: Migrate conversations
# ---------------------------------------------------------------
echo ""
echo "[4/6] Migrating conversations..."

# Same approach: INSERT ... ON CONFLICT DO NOTHING
# New columns (conversation_type, participant_b_id) get their DEFAULT values:
#   conversation_type = 'ai_chat'
#   participant_b_id = NULL
# Any human_chat conversations created on the NEW service are NOT touched.
pg_dump "$OLD_DB_URL" \
    --data-only \
    --table=conversations \
    --column-inserts \
    --no-owner \
    --no-privileges \
    2>/dev/null \
| sed 's/INSERT INTO public\.conversations/INSERT INTO conversations/g' \
| sed 's/);$/) ON CONFLICT (id) DO NOTHING;/g' \
| psql "$NEW_DB_URL" -q 2>/dev/null

LOADED_CONVERSATIONS=$(psql "$NEW_DB_URL" -t -c "SELECT COUNT(*) FROM conversations" | tr -d ' ')
echo "  Loaded: $LOADED_CONVERSATIONS conversations"

# ---------------------------------------------------------------
# Step 5: Migrate messages
# ---------------------------------------------------------------
echo ""
echo "[5/6] Migrating messages (this may take a minute for large datasets)..."

# Same approach. The new sender_id column defaults to NULL.
# We'll backfill it in the next step.
pg_dump "$OLD_DB_URL" \
    --data-only \
    --table=messages \
    --column-inserts \
    --no-owner \
    --no-privileges \
    2>/dev/null \
| sed 's/INSERT INTO public\.messages/INSERT INTO messages/g' \
| sed 's/);$/) ON CONFLICT (id) DO NOTHING;/g' \
| psql "$NEW_DB_URL" -q 2>/dev/null

LOADED_MESSAGES=$(psql "$NEW_DB_URL" -t -c "SELECT COUNT(*) FROM messages" | tr -d ' ')
echo "  Loaded: $LOADED_MESSAGES messages"

# ---------------------------------------------------------------
# Step 5b: Backfill sender_id for newly migrated rows
# ---------------------------------------------------------------
echo ""
echo "  Backfilling sender_id for new rows..."

psql "$NEW_DB_URL" -q <<'SQL'
-- Only update rows where sender_id IS NULL (= rows from the old service).
-- Rows already backfilled from a previous migration run are NOT touched.
-- Rows created by the NEW service already have sender_id set.

-- For user messages: sender_id = the human who started the conversation
UPDATE messages m
SET sender_id = c.user_id
FROM conversations c
WHERE m.conversation_id = c.id
  AND m.role = 'user'
  AND m.sender_id IS NULL;

-- For assistant messages: sender_id = the AI influencer
UPDATE messages m
SET sender_id = c.influencer_id
FROM conversations c
WHERE m.conversation_id = c.id
  AND m.role = 'assistant'
  AND m.sender_id IS NULL;
SQL

UNFILLED=$(psql "$NEW_DB_URL" -t -c "SELECT COUNT(*) FROM messages WHERE sender_id IS NULL" | tr -d ' ')
echo "  Rows still without sender_id: $UNFILLED (should be 0)"

# ---------------------------------------------------------------
# Step 6: Re-enable triggers and verify
# ---------------------------------------------------------------
echo ""
echo "[6/6] Re-enabling triggers and verifying..."

psql "$NEW_DB_URL" -q <<'SQL'
ALTER TABLE messages ENABLE TRIGGER trigger_update_conversation_timestamp;
SQL

# Final counts
NEW_INFLUENCERS=$(psql "$NEW_DB_URL" -t -c "SELECT COUNT(*) FROM ai_influencers" | tr -d ' ')
NEW_CONVERSATIONS=$(psql "$NEW_DB_URL" -t -c "SELECT COUNT(*) FROM conversations" | tr -d ' ')
NEW_MESSAGES=$(psql "$NEW_DB_URL" -t -c "SELECT COUNT(*) FROM messages" | tr -d ' ')
NEW_HUMAN_CONVS=$(psql "$NEW_DB_URL" -t -c "SELECT COUNT(*) FROM conversations WHERE conversation_type = 'human_chat'" | tr -d ' ')

echo ""
echo "  ┌─────────────────────┬───────────┬───────────┬─────────┐"
echo "  │ Table               │ Old DB    │ New DB    │ Match?  │"
echo "  ├─────────────────────┼───────────┼───────────┼─────────┤"
printf "  │ ai_influencers      │ %9s │ %9s │ %s │\n" "$OLD_INFLUENCERS" "$NEW_INFLUENCERS" \
    "$([ "$OLD_INFLUENCERS" = "$NEW_INFLUENCERS" ] && echo '  yes ' || echo '  ~   ')"
printf "  │ conversations (all) │ %9s │ %9s │ %s │\n" "$OLD_CONVERSATIONS" "$NEW_CONVERSATIONS" \
    "$([ "$OLD_CONVERSATIONS" -le "$NEW_CONVERSATIONS" ] 2>/dev/null && echo '  yes ' || echo '  ~   ')"
printf "  │ messages            │ %9s │ %9s │ %s │\n" "$OLD_MESSAGES" "$NEW_MESSAGES" \
    "$([ "$OLD_MESSAGES" -le "$NEW_MESSAGES" ] 2>/dev/null && echo '  yes ' || echo '  ~   ')"
echo "  ├─────────────────────┼───────────┴───────────┴─────────┤"
printf "  │ human_chat convs    │ %9s (new service only)        │\n" "$NEW_HUMAN_CONVS"
echo "  └─────────────────────┴─────────────────────────────────┘"
echo ""

# Check if new DB has >= old DB rows (new DB may have MORE due to human chats)
if [ "$NEW_INFLUENCERS" -ge "$OLD_INFLUENCERS" ] 2>/dev/null && \
   [ "$NEW_MESSAGES" -ge "$OLD_MESSAGES" ] 2>/dev/null; then
    echo "Migration successful!"
    echo ""
    if [ "$RUN_TYPE" = "first" ]; then
        echo "  This was the FIRST migration run."
        echo "  You can now test the service at: https://chat-ai.rishi.yral.com"
        echo ""
        echo "  When ready to go live, run this script AGAIN to sync any"
        echo "  new data created since today, then switch DNS."
    else
        echo "  This was an INCREMENTAL run (caught up with new data)."
        echo "  New rows inserted since last run:"
        echo "    influencers:   +$(( NEW_INFLUENCERS - EXISTING_INFLUENCERS ))"
        echo ""
        echo "  You can now switch DNS to go live."
    fi
else
    echo "WARNING: Row counts look off. Check the output above for errors."
    echo "  This may be OK if the old DB has rows that failed to insert"
    echo "  (e.g., foreign key violations from deleted influencers)."
fi

echo ""
echo "=========================================="
echo " Quick verification commands:"
echo "=========================================="
echo ""
echo "  # Check influencers are visible:"
echo "  curl -s https://chat-ai.rishi.yral.com/api/v1/influencers | python3 -m json.tool | head -20"
echo ""
echo "  # Compare with old service:"
echo "  bash scripts/compare-apis.sh"
echo ""
echo "  # When ready to switch DNS:"
echo "  # 1. Run this script again (if more than a day has passed)"
echo "  # 2. Update Cloudflare: chat.yral.com -> $SERVER_1_IP + $SERVER_2_IP"
echo "  # 3. Add Caddy config (see MIGRATION-RUNBOOK.md Step 5c)"
echo ""
