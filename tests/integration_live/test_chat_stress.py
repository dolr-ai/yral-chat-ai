#!/usr/bin/env python3
"""
Phase T3: AI Chat Stress Test — 50 concurrent chat messages.

Sends messages to multiple influencers simultaneously and verifies
every single one gets a real AI response (not fallback).

Usage:
    python3 tests/test_chat_stress.py
    python3 tests/test_chat_stress.py --count 20  # fewer messages

Requires: JWT token set as environment variable TEST_JWT
    export TEST_JWT="eyJhbGci..."
"""

import argparse
import json
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

BASE = "https://chat-ai.rishi.yral.com"
FALLBACK_MSG = "I'm having trouble responding right now"


def get_token():
    token = os.environ.get("TEST_JWT")
    if not token:
        # Generate a test token
        try:
            import jwt as pyjwt
            payload = {
                "sub": f"stress-test-{uuid.uuid4().hex[:8]}",
                "iss": "https://auth.yral.com",
                "exp": int(time.time()) + 86400,
                "iat": int(time.time()),
            }
            token = pyjwt.encode(payload, "dummy", algorithm="HS256")
        except ImportError:
            print("ERROR: Need either TEST_JWT env var or pyjwt installed")
            sys.exit(1)
    return token


def send_chat_message(token, influencer_id, user_suffix, message):
    """Create a conversation and send a message. Returns (success, response_time, detail)."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    start = time.time()
    try:
        # Create conversation
        conv_r = requests.post(
            f"{BASE}/api/v1/chat/conversations",
            headers=headers,
            json={"influencer_id": influencer_id},
            timeout=15,
        )
        if conv_r.status_code not in (200, 201):
            return (False, (time.time() - start) * 1000,
                    f"Create conv failed: {conv_r.status_code}")

        conv_id = conv_r.json()["id"]

        # Send message
        msg_r = requests.post(
            f"{BASE}/api/v1/chat/conversations/{conv_id}/messages",
            headers=headers,
            json={
                "content": message,
                "message_type": "text",
                "client_message_id": f"stress-{uuid.uuid4().hex[:12]}",
            },
            timeout=30,
        )

        latency = (time.time() - start) * 1000

        if msg_r.status_code >= 500:
            return (False, latency, f"Server error: {msg_r.status_code}")

        data = msg_r.json()
        assistant = data.get("assistant_message", {})
        content = assistant.get("content", "") if assistant else ""

        if FALLBACK_MSG in content:
            return (False, latency, "Got fallback error message (Gemini failed)")

        if not content:
            return (False, latency, "Empty AI response")

        return (True, latency, content[:50])

    except requests.Timeout:
        return (False, (time.time() - start) * 1000, "TIMEOUT (>30s)")
    except Exception as e:
        return (False, (time.time() - start) * 1000, str(e)[:80])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=50, help="Number of messages")
    parser.add_argument("--concurrency", type=int, default=10, help="Concurrent messages")
    args = parser.parse_args()

    print("=" * 60)
    print(f" Chat Stress Test: {args.count} messages, {args.concurrency} concurrent")
    print("=" * 60)

    # Get influencer IDs
    print("\nFetching influencers...")
    r = requests.get(f"{BASE}/api/v1/influencers?limit=20", timeout=10)
    influencers = r.json()["influencers"]
    if not influencers:
        print("ERROR: No influencers found")
        sys.exit(1)
    print(f"  Found {len(influencers)} influencers")

    # Generate per-message tokens (each simulates a different user)
    messages = [
        "Hey! Tell me about yourself",
        "What do you think about the weather today?",
        "I need some advice about fitness",
        "Can you recommend something fun to do this weekend?",
        "Tell me a joke!",
        "What's your favorite food?",
        "How are you doing today?",
        "I'm feeling stressed, can you help?",
        "Tell me something interesting",
        "What do you like to do for fun?",
    ]

    print(f"\nSending {args.count} messages...")
    results = []
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = []
        for i in range(args.count):
            inf = influencers[i % len(influencers)]
            msg = messages[i % len(messages)]

            # Each message gets its own JWT with a unique user ID
            try:
                import jwt as pyjwt
                payload = {
                    "sub": f"stress-user-{i:04d}",
                    "iss": "https://auth.yral.com",
                    "exp": int(time.time()) + 86400,
                    "iat": int(time.time()),
                }
                token = pyjwt.encode(payload, "dummy", algorithm="HS256")
            except ImportError:
                token = get_token()

            futures.append(pool.submit(
                send_chat_message, token, inf["id"], f"user-{i}", msg
            ))

        for f in as_completed(futures):
            results.append(f.result())

    total_time = time.time() - start_time

    # Analyze
    success = sum(1 for r in results if r[0])
    failed = sum(1 for r in results if not r[0])
    latencies = sorted(r[1] for r in results if r[0])

    print(f"\n{'=' * 60}")
    print(f" Results")
    print(f"{'=' * 60}")
    print(f"  Total time:      {total_time:.1f}s")
    print(f"  Messages/sec:    {args.count / total_time:.1f}")
    print(f"  Success:         {success}/{args.count} ({success/args.count*100:.1f}%)")
    print(f"  Failed:          {failed}")

    if latencies:
        print(f"  Latency p50:     {latencies[len(latencies)//2]:.0f}ms")
        print(f"  Latency p95:     {latencies[int(len(latencies)*0.95)]:.0f}ms")
        print(f"  Latency max:     {max(latencies):.0f}ms")

    # Show failures
    failures = [(r[1], r[2]) for r in results if not r[0]]
    if failures:
        print(f"\n  Failure reasons:")
        from collections import Counter
        for reason, count in Counter(r[1] for r in failures).most_common(5):
            print(f"    {count}x: {reason}")

    # Pass/fail
    success_rate = success / args.count if args.count > 0 else 0
    if success_rate >= 0.90:
        print(f"\n  PASS (success rate {success_rate*100:.1f}% >= 90%)")
        sys.exit(0)
    else:
        print(f"\n  FAIL (success rate {success_rate*100:.1f}% < 90%)")
        sys.exit(1)


if __name__ == "__main__":
    main()
