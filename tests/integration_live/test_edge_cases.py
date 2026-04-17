#!/usr/bin/env python3
"""
Phase T5: Edge Case & Error Handling Tests.

Tests that the service handles bad inputs gracefully (returns proper errors
instead of crashing).

Usage:
    python3 tests/test_edge_cases.py
"""

import json
import os
import sys
import time
import uuid
import requests

BASE = "https://chat-ai.rishi.yral.com"
PASS = 0
FAIL = 0


def get_test_token(user_id=None):
    """Generate a test JWT."""
    try:
        import jwt as pyjwt
        payload = {
            "sub": user_id or f"edge-test-{uuid.uuid4().hex[:8]}",
            "iss": "https://auth.yral.com",
            "exp": int(time.time()) + 86400,
            "iat": int(time.time()),
        }
        return pyjwt.encode(payload, "dummy", algorithm="HS256")
    except ImportError:
        return os.environ.get("TEST_JWT", "")


def get_expired_token():
    """Generate an expired JWT."""
    try:
        import jwt as pyjwt
        payload = {
            "sub": "expired-user",
            "iss": "https://auth.yral.com",
            "exp": int(time.time()) - 3600,  # Expired 1 hour ago
            "iat": int(time.time()) - 7200,
        }
        return pyjwt.encode(payload, "dummy", algorithm="HS256")
    except ImportError:
        return ""


def get_bad_issuer_token():
    """Generate a JWT with an untrusted issuer."""
    try:
        import jwt as pyjwt
        payload = {
            "sub": "hacker-user",
            "iss": "https://evil.com",
            "exp": int(time.time()) + 86400,
            "iat": int(time.time()),
        }
        return pyjwt.encode(payload, "dummy", algorithm="HS256")
    except ImportError:
        return ""


def report(test_name, passed, detail=""):
    global PASS, FAIL
    if passed:
        PASS += 1
        print(f"  PASS  {test_name}")
    else:
        FAIL += 1
        print(f"  FAIL  {test_name}")
        if detail:
            print(f"        {detail}")


def auth_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def test_auth_edge_cases():
    """Test authentication edge cases."""
    print("\n--- Auth Edge Cases ---")

    # No auth header
    r = requests.get(f"{BASE}/api/v1/chat/conversations", timeout=10)
    report("No auth header → 401", r.status_code == 401, f"Got: {r.status_code}")

    # Invalid format (no Bearer prefix)
    r = requests.get(f"{BASE}/api/v1/chat/conversations",
                     headers={"Authorization": "InvalidToken123"}, timeout=10)
    report("Invalid auth format → 401", r.status_code == 401, f"Got: {r.status_code}")

    # Expired JWT
    token = get_expired_token()
    if token:
        r = requests.get(f"{BASE}/api/v1/chat/conversations",
                         headers={"Authorization": f"Bearer {token}"}, timeout=10)
        report("Expired JWT → 401", r.status_code == 401, f"Got: {r.status_code}")

    # Untrusted issuer
    token = get_bad_issuer_token()
    if token:
        r = requests.get(f"{BASE}/api/v1/chat/conversations",
                         headers={"Authorization": f"Bearer {token}"}, timeout=10)
        report("Bad issuer JWT → 401", r.status_code == 401, f"Got: {r.status_code}")

    # Garbage token
    r = requests.get(f"{BASE}/api/v1/chat/conversations",
                     headers={"Authorization": "Bearer not.a.real.jwt.token"}, timeout=10)
    report("Garbage JWT → 401", r.status_code == 401, f"Got: {r.status_code}")


def test_conversation_edge_cases():
    """Test conversation edge cases."""
    print("\n--- Conversation Edge Cases ---")
    token = get_test_token()

    # Non-existent influencer
    r = requests.post(f"{BASE}/api/v1/chat/conversations",
                      headers=auth_headers(token),
                      json={"influencer_id": "nonexistent-id-xyz"}, timeout=10)
    report("Non-existent influencer → 404", r.status_code == 404, f"Got: {r.status_code}")

    # Missing influencer_id
    r = requests.post(f"{BASE}/api/v1/chat/conversations",
                      headers=auth_headers(token),
                      json={}, timeout=10)
    report("Missing influencer_id → 422", r.status_code == 422, f"Got: {r.status_code}")

    # Non-existent conversation
    r = requests.get(f"{BASE}/api/v1/chat/conversations/nonexistent-conv-id/messages",
                     headers=auth_headers(token), timeout=10)
    report("Non-existent conversation → 404", r.status_code == 404, f"Got: {r.status_code}")

    # Delete conversation you don't own (use a different user)
    token2 = get_test_token("different-user-xyz")
    # First create a conversation with token1
    inf_r = requests.get(f"{BASE}/api/v1/influencers?limit=1", timeout=10)
    if inf_r.json()["influencers"]:
        inf_id = inf_r.json()["influencers"][0]["id"]
        conv_r = requests.post(f"{BASE}/api/v1/chat/conversations",
                               headers=auth_headers(token),
                               json={"influencer_id": inf_id}, timeout=10)
        if conv_r.status_code in (200, 201):
            conv_id = conv_r.json()["id"]
            # Try to delete with different user
            del_r = requests.delete(f"{BASE}/api/v1/chat/conversations/{conv_id}",
                                    headers=auth_headers(token2), timeout=10)
            report("Delete other's conversation → 403", del_r.status_code == 403,
                   f"Got: {del_r.status_code}")


def test_message_edge_cases():
    """Test message sending edge cases."""
    print("\n--- Message Edge Cases ---")
    token = get_test_token("edge-msg-user")

    # Create a conversation first
    inf_r = requests.get(f"{BASE}/api/v1/influencers?limit=1", timeout=10)
    inf_id = inf_r.json()["influencers"][0]["id"]
    conv_r = requests.post(f"{BASE}/api/v1/chat/conversations",
                           headers=auth_headers(token),
                           json={"influencer_id": inf_id}, timeout=15)
    conv_id = conv_r.json()["id"]

    # Empty content with text type (should still work — some services allow it)
    r = requests.post(f"{BASE}/api/v1/chat/conversations/{conv_id}/messages",
                      headers=auth_headers(token),
                      json={"content": "", "message_type": "text"}, timeout=30)
    report("Empty content → should not 500",
           r.status_code < 500, f"Got: {r.status_code}")

    # Very long content (5000 chars)
    long_msg = "Hello! " * 700  # ~4900 chars
    r = requests.post(f"{BASE}/api/v1/chat/conversations/{conv_id}/messages",
                      headers=auth_headers(token),
                      json={"content": long_msg, "message_type": "text"}, timeout=30)
    report("Very long message (5000 chars) → should not 500",
           r.status_code < 500, f"Got: {r.status_code}")

    # Unicode and emoji
    r = requests.post(f"{BASE}/api/v1/chat/conversations/{conv_id}/messages",
                      headers=auth_headers(token),
                      json={"content": "Hello 🌍 नमस्ते مرحبا", "message_type": "text"},
                      timeout=30)
    report("Unicode + emoji → should not 500",
           r.status_code < 500, f"Got: {r.status_code}")

    # Send to non-existent conversation
    r = requests.post(f"{BASE}/api/v1/chat/conversations/fake-conv-id/messages",
                      headers=auth_headers(token),
                      json={"content": "hello", "message_type": "text"}, timeout=10)
    report("Message to non-existent conv → 404",
           r.status_code == 404, f"Got: {r.status_code}")


def test_influencer_edge_cases():
    """Test influencer edge cases."""
    print("\n--- Influencer Edge Cases ---")
    token = get_test_token("edge-inf-user")

    # Delete influencer you don't own
    inf_r = requests.get(f"{BASE}/api/v1/influencers?limit=1", timeout=10)
    if inf_r.json()["influencers"]:
        inf_id = inf_r.json()["influencers"][0]["id"]
        r = requests.delete(f"{BASE}/api/v1/influencers/{inf_id}",
                            headers=auth_headers(token), timeout=10)
        report("Delete others influencer → 403",
               r.status_code == 403, f"Got: {r.status_code}")

    # Admin ban without key
    r = requests.post(f"{BASE}/api/v1/admin/influencers/some-id",
                      headers=auth_headers(token), timeout=10)
    report("Admin ban without key → 403",
           r.status_code == 403, f"Got: {r.status_code}")

    # Admin ban with wrong key
    r = requests.post(f"{BASE}/api/v1/admin/influencers/some-id",
                      headers={**auth_headers(token), "X-Admin-Key": "wrong-key"},
                      timeout=10)
    report("Admin ban with wrong key → 403",
           r.status_code == 403, f"Got: {r.status_code}")


def test_response_format():
    """Verify all error responses have proper JSON format."""
    print("\n--- Error Response Format ---")

    # Every error should return {"detail": "..."}
    test_cases = [
        ("GET", f"{BASE}/api/v1/chat/conversations", None),
        ("GET", f"{BASE}/api/v1/influencers/nonexistent", None),
        ("POST", f"{BASE}/api/v1/chat/conversations", {"influencer_id": "fake"}),
    ]

    token = get_test_token()
    for method, url, body in test_cases:
        if method == "GET":
            r = requests.get(url, headers=auth_headers(token), timeout=10)
        else:
            r = requests.post(url, headers=auth_headers(token), json=body, timeout=10)

        if r.status_code >= 400:
            try:
                data = r.json()
                has_detail = "detail" in data
                report(f"{method} {url.split('/')[-1]} error has 'detail' field",
                       has_detail, f"Response: {json.dumps(data)[:80]}")
            except json.JSONDecodeError:
                report(f"{method} {url.split('/')[-1]} returns JSON on error",
                       False, f"Response: {r.text[:80]}")


if __name__ == "__main__":
    print("=" * 60)
    print(" Edge Case & Error Handling Tests")
    print("=" * 60)

    test_auth_edge_cases()
    test_conversation_edge_cases()
    test_message_edge_cases()
    test_influencer_edge_cases()
    test_response_format()

    print(f"\n{'=' * 60}")
    print(f" Results: {PASS} passed, {FAIL} failed")
    print("=" * 60)

    sys.exit(1 if FAIL > 0 else 0)
