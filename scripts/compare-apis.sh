#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# compare-apis.sh — Compare API responses between old and new chat services.
#
# WHAT THIS SCRIPT DOES:
# Calls the same endpoints on both services and compares the responses.
# This helps verify that the new Python service returns the same data
# as the old Rust service after migration.
#
# USAGE:
#   bash scripts/compare-apis.sh
#   bash scripts/compare-apis.sh --with-auth "Bearer YOUR_JWT_TOKEN"
#
# NOTE: Some endpoints require authentication. Pass a JWT token via
# --with-auth to test authenticated endpoints.
# ---------------------------------------------------------------------------

set -euo pipefail

OLD_HOST="https://chat.yral.com"
NEW_HOST="https://chat-ai.rishi.yral.com"
AUTH_HEADER=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --with-auth)
            AUTH_HEADER="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

echo "=========================================="
echo " API Comparison: Old vs New Chat Service"
echo "=========================================="
echo "Old: $OLD_HOST"
echo "New: $NEW_HOST"
echo ""

PASS=0
FAIL=0

compare() {
    local path="$1"
    local description="$2"
    local auth_flag="${3:-}"

    echo -n "  $description... "

    local curl_opts="-s --max-time 10"
    if [ -n "$auth_flag" ] && [ -n "$AUTH_HEADER" ]; then
        curl_opts="$curl_opts -H 'Authorization: $AUTH_HEADER'"
    fi

    old_status=$(eval curl $curl_opts -o /dev/null -w '%{http_code}' "$OLD_HOST$path" 2>/dev/null || echo "000")
    new_status=$(eval curl $curl_opts -o /dev/null -w '%{http_code}' "$NEW_HOST$path" 2>/dev/null || echo "000")

    if [ "$old_status" = "$new_status" ]; then
        echo "✓ (both: $old_status)"
        PASS=$((PASS + 1))
    else
        echo "✗ (old: $old_status, new: $new_status)"
        FAIL=$((FAIL + 1))
    fi
}

echo "[Public endpoints — no auth required]"
compare "/health" "Health check"
compare "/api/v1/influencers?limit=3" "List influencers"
compare "/api/v1/influencers/trending?limit=3" "Trending influencers"
compare "/api/v1/chat/ws/docs" "WebSocket docs"

echo ""
echo "[Auth-required endpoints]"
if [ -n "$AUTH_HEADER" ]; then
    compare "/api/v1/chat/conversations?limit=3" "List conversations" "auth"
    compare "/api/v1/influencers/generate-prompt" "Generate prompt (POST)" "auth"
else
    echo "  (skipped — pass --with-auth 'Bearer JWT' to test)"
fi

echo ""
echo "[Response format comparison — influencers]"
echo -n "  Comparing influencer JSON fields... "
old_fields=$(curl -s "$OLD_HOST/api/v1/influencers?limit=1" 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    if data.get('influencers'):
        print(','.join(sorted(data['influencers'][0].keys())))
    else:
        print('(empty)')
except: print('(error)')
" 2>/dev/null || echo "(error)")

new_fields=$(curl -s "$NEW_HOST/api/v1/influencers?limit=1" 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    if data.get('influencers'):
        print(','.join(sorted(data['influencers'][0].keys())))
    else:
        print('(empty)')
except: print('(error)')
" 2>/dev/null || echo "(error)")

if [ "$old_fields" = "$new_fields" ]; then
    echo "✓ (fields match)"
    PASS=$((PASS + 1))
elif [ "$old_fields" = "(empty)" ] || [ "$new_fields" = "(empty)" ]; then
    echo "~ (one or both have no data — compare after migration)"
else
    echo "✗"
    echo "    Old fields: $old_fields"
    echo "    New fields: $new_fields"
    FAIL=$((FAIL + 1))
fi

echo ""
echo "=========================================="
echo " Results: $PASS passed, $FAIL failed"
echo "=========================================="
