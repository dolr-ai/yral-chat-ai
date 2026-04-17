# Pre-Production Testing Plan

**Goal:** Prove that `chat-ai.rishi.yral.com` can replace `chat.yral.com` with
zero problems. Every test here must PASS before we switch DNS.

**Important:** All test data created during testing will be CLEANED UP before
the final migration. The final source of truth is Ravi's Postgres database.

---

## Phase T1: API Response Compatibility (Old vs New)

**What:** Compare EVERY public endpoint response between old and new services
field-by-field to ensure mobile app compatibility.

**Why:** If any field name, type, or structure is different, the mobile app crashes.

**Script:** `tests/test_api_compatibility.py`

**Endpoints tested:**
- GET /api/v1/influencers (list, pagination, field names)
- GET /api/v1/influencers/trending (ordering, field names)
- GET /api/v1/influencers/{id} (detail response)
- GET /api/v1/chat/ws/docs (WebSocket schema)
- GET /health (response format — note: intentionally different)

**Pass criteria:**
- Same field names in JSON responses
- Same data types (is_active = string, not bool)
- Same pagination structure (total, limit, offset)
- Same influencer ordering

---

## Phase T2: Load Test — Simulate 200 Concurrent Users

**What:** Hit the service with 200 concurrent requests across different endpoints
to verify it handles production-level traffic without errors or slowdowns.

**Why:** The old Rust service handles ~4,000 downloads/day. With active chat users,
we need to handle hundreds of concurrent requests.

**Script:** `tests/test_load.py`

**Tests:**
1. **Influencer listing under load** — 200 concurrent GET /api/v1/influencers
2. **Trending under load** — 200 concurrent GET /api/v1/influencers/trending
3. **Health check under load** — 200 concurrent GET /health
4. **Mixed workload** — 200 concurrent requests split across all public endpoints

**Pass criteria:**
- Zero 5xx errors
- 95th percentile response time < 2 seconds
- Zero connection refused errors
- All responses return valid JSON

---

## Phase T3: AI Chat Stress Test

**What:** Send 50 concurrent chat messages to different influencers and verify
every single one gets a real AI response (not fallback).

**Why:** The Gemini API has rate limits. We need to confirm our service handles
concurrent AI requests gracefully.

**Script:** `tests/test_chat_stress.py`

**Pass criteria:**
- Zero "I'm having trouble responding" fallback messages
- All responses contain personality-appropriate content
- All messages saved to database correctly
- No duplicate messages

---

## Phase T4: Data Integrity Verification

**What:** Compare specific records between old and new databases to verify
migration accuracy at the row level (not just counts).

**Script:** `tests/test_data_integrity.py`

**Tests:**
1. Pick 10 random influencers — compare every field
2. Pick 10 random conversations — compare message counts
3. Pick 10 random messages — compare content, role, timestamps
4. Verify sender_id is populated for ALL messages
5. Verify conversation_type is 'ai_chat' for ALL migrated conversations

**Pass criteria:**
- 100% field-level match for sampled records
- Zero NULL sender_id values
- Zero unexpected conversation_type values

---

## Phase T5: Error Handling & Edge Cases

**What:** Test that the service handles bad inputs gracefully instead of crashing.

**Script:** `tests/test_edge_cases.py`

**Tests:**
1. Send message with empty content → should return error, not crash
2. Send message to non-existent conversation → 404
3. Send message with invalid JWT → 401
4. Send message with expired JWT → 401
5. Create conversation with non-existent influencer → 404
6. Create duplicate conversation → return existing
7. Delete conversation you don't own → 403
8. Send message with very long content (10,000 chars) → should work
9. Send message with special characters (emoji, unicode, RTL text) → should work
10. Hit rate limit → 429 (not 500)

**Pass criteria:**
- All error responses return proper HTTP status codes (4xx, not 5xx)
- All error responses return JSON with "detail" field
- No server crashes or restarts

---

## Phase T6: WebSocket Stability Test

**What:** Open 50 concurrent WebSocket connections and verify they all receive
events when messages are sent.

**Script:** Manual test with wscat or `tests/test_websocket.py`

**Pass criteria:**
- All 50 connections establish successfully
- Events are delivered to all connections for the same user
- Connections survive for 5+ minutes without dropping

---

## Phase T7: Android Device Testing

**What:** Install the YRAL app on Rishi's Android device and configure it to
point to the new service. Test the full user flow.

**How:**
1. Build a debug version of the app that points to `chat-ai.rishi.yral.com`
   (OR use a proxy like Charles/mitmproxy to redirect traffic)
2. Test:
   - Open app → home feed loads
   - Go to Discover → influencer list loads
   - Tap an influencer → chat screen opens with greeting
   - Send a message → AI responds
   - Check inbox → conversation appears with last message preview
   - Back to home → inbox icon shows unread count
   - Create a new AI influencer → creation flow works
   - View profile → videos and info display correctly

**Pass criteria:**
- No crashes
- No empty screens
- AI responses appear within 5 seconds
- All UI elements populated correctly

---

## Phase T8: Cleanup & Final Migration

**What:** After all tests pass, clean up test data and do the final migration.

**Steps:**
1. Delete ALL test conversations and messages created during testing
2. Run the migration script one final time (catches data from the gap period)
3. Verify row counts match between old and new databases
4. Switch DNS
5. Monitor for 1 week

---

## Test Execution Order

```
T1 (API compatibility)  → if FAIL, fix response format first
T2 (Load test)          → if FAIL, optimize or scale
T3 (Chat stress)        → if FAIL, check Gemini rate limits
T4 (Data integrity)     → if FAIL, investigate migration gaps
T5 (Edge cases)         → if FAIL, fix error handling
T6 (WebSocket)          → if FAIL, fix WebSocket manager
T7 (Android device)     → if FAIL, fix mobile app compatibility
T8 (Cleanup + migrate)  → final step, only after T1-T7 all pass
```
