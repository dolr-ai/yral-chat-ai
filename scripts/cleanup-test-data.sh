#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# cleanup-test-data.sh — Remove ALL test data before final production migration.
#
# WHAT THIS DOES:
# Deletes conversations and messages created by test users (identified by
# their user_id patterns). Does NOT touch real user data from the migration.
#
# WHEN TO RUN:
# After all testing is complete and BEFORE the final migration run.
# This ensures the final migration starts from a clean state.
#
# SAFETY:
# - Only deletes conversations where user_id matches test patterns
# - Prints what will be deleted BEFORE deleting
# - Requires confirmation before proceeding
# - Does NOT delete influencers (they were migrated from production)
#
# USAGE:
#   bash scripts/cleanup-test-data.sh           # dry run (shows what would be deleted)
#   bash scripts/cleanup-test-data.sh --apply   # actually delete
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
source "$PROJECT_ROOT/project.config"
source "$PROJECT_ROOT/servers.config"

DRY_RUN=true
if [ "${1:-}" = "--apply" ]; then
    DRY_RUN=false
fi

echo "=========================================="
echo " Test Data Cleanup"
echo " Mode: $([ "$DRY_RUN" = true ] && echo 'DRY RUN (preview only)' || echo 'APPLY (will delete!)')"
echo "=========================================="

# Find the Patroni container and current leader
PATRONI=$(ssh -o StrictHostKeyChecking=no -i "$SSH_KEY_PATH" "${DEPLOY_USER}@${SERVER_1_IP}" \
    "docker ps --format '{{.Names}}' | grep 'chat-ai-db_patroni-rishi-1' | head -1")

if [ -z "$PATRONI" ]; then
    echo "ERROR: Could not find Patroni container"
    exit 1
fi

# Get leader hostname
LEADER=$(ssh -o StrictHostKeyChecking=no -i "$SSH_KEY_PATH" "${DEPLOY_USER}@${SERVER_1_IP}" \
    "docker exec $PATRONI patronictl -c /etc/patroni.yml list -f json 2>/dev/null" \
    | python3 -c "import sys,json; [print(m['Host']) for m in json.load(sys.stdin) if m['Role']=='Leader']" 2>/dev/null)

if [ -z "$LEADER" ]; then
    echo "WARNING: Could not determine leader, defaulting to haproxy"
    LEADER="chat-ai-db_haproxy-rishi-1"
fi

echo "Using DB leader: $LEADER"

PW=$(cat "$PROJECT_ROOT/.bootstrap-secrets/postgres_password" 2>/dev/null || echo "")
if [ -z "$PW" ]; then
    echo "ERROR: Could not read postgres password"
    exit 1
fi

DB_URL="postgresql://postgres:${PW}@${LEADER}:5432/chat_ai_db"

# Test patterns that identify test data
# These are the user_id patterns used by our test scripts
TEST_PATTERNS=(
    "test-user-principal-id-for-testing"
    "stress-test-%"
    "stress-user-%"
    "edge-test-%"
    "edge-msg-user"
    "edge-inf-user"
    "different-user-xyz"
)

echo ""
echo "Test user patterns:"
for p in "${TEST_PATTERNS[@]}"; do
    echo "  - $p"
done

# Build the WHERE clause
WHERE_PARTS=()
for p in "${TEST_PATTERNS[@]}"; do
    WHERE_PARTS+=("user_id LIKE '${p}'")
done
WHERE_CLAUSE=$(IFS=" OR "; echo "${WHERE_PARTS[*]}")

echo ""
echo "--- Preview: Conversations to delete ---"

ssh -o StrictHostKeyChecking=no -i "$SSH_KEY_PATH" "${DEPLOY_USER}@${SERVER_1_IP}" \
    "docker exec -i $PATRONI psql '$DB_URL' -c \"
SELECT user_id, COUNT(*) as conversations,
       (SELECT COUNT(*) FROM messages m WHERE m.conversation_id IN
           (SELECT id FROM conversations WHERE ${WHERE_CLAUSE})) as total_messages
FROM conversations
WHERE ${WHERE_CLAUSE}
GROUP BY user_id
ORDER BY conversations DESC;
\""

echo ""
echo "--- Summary ---"
CONV_COUNT=$(ssh -o StrictHostKeyChecking=no -i "$SSH_KEY_PATH" "${DEPLOY_USER}@${SERVER_1_IP}" \
    "docker exec -i $PATRONI psql '$DB_URL' -t -c \"
SELECT COUNT(*) FROM conversations WHERE ${WHERE_CLAUSE}\"" | tr -d ' ')

MSG_COUNT=$(ssh -o StrictHostKeyChecking=no -i "$SSH_KEY_PATH" "${DEPLOY_USER}@${SERVER_1_IP}" \
    "docker exec -i $PATRONI psql '$DB_URL' -t -c \"
SELECT COUNT(*) FROM messages WHERE conversation_id IN
    (SELECT id FROM conversations WHERE ${WHERE_CLAUSE})\"" | tr -d ' ')

echo "  Conversations to delete: $CONV_COUNT"
echo "  Messages to delete:      $MSG_COUNT"

if [ "$DRY_RUN" = true ]; then
    echo ""
    echo "This was a DRY RUN. To actually delete, run:"
    echo "  bash scripts/cleanup-test-data.sh --apply"
    exit 0
fi

echo ""
echo "⚠️  About to DELETE $CONV_COUNT conversations and $MSG_COUNT messages."
echo "    This cannot be undone!"
read -p "    Type 'yes' to confirm: " CONFIRM

if [ "$CONFIRM" != "yes" ]; then
    echo "Aborted."
    exit 1
fi

echo ""
echo "Deleting test data..."

ssh -o StrictHostKeyChecking=no -i "$SSH_KEY_PATH" "${DEPLOY_USER}@${SERVER_1_IP}" \
    "docker exec -i $PATRONI psql '$DB_URL' -c \"
-- Delete messages first (FK constraint)
DELETE FROM messages WHERE conversation_id IN
    (SELECT id FROM conversations WHERE ${WHERE_CLAUSE});
-- Then delete conversations
DELETE FROM conversations WHERE ${WHERE_CLAUSE};
\""

echo ""
echo "✅ Test data deleted."
echo ""
echo "Remaining data (should be only real migrated data):"
ssh -o StrictHostKeyChecking=no -i "$SSH_KEY_PATH" "${DEPLOY_USER}@${SERVER_1_IP}" \
    "docker exec -i $PATRONI psql '$DB_URL' -c \"
SELECT 'ai_influencers' as t, COUNT(*) FROM ai_influencers
UNION ALL SELECT 'conversations', COUNT(*) FROM conversations
UNION ALL SELECT 'messages', COUNT(*) FROM messages;
\""
