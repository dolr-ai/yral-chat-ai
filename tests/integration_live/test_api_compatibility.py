#!/usr/bin/env python3
"""
Phase T1: API Response Compatibility — Old service vs New service.

Compares EVERY public endpoint response field-by-field to ensure the
mobile app will work identically with the new service.

Usage:
    python3 tests/test_api_compatibility.py

No authentication needed — tests public endpoints only.
"""

import json
import sys
import requests

OLD = "https://chat.yral.com"
NEW = "https://chat-ai.rishi.yral.com"

PASS = 0
FAIL = 0
WARN = 0


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


def warn(test_name, detail=""):
    global WARN
    WARN += 1
    print(f"  WARN  {test_name}")
    if detail:
        print(f"        {detail}")


def compare_fields(old_obj, new_obj, context=""):
    """Compare JSON objects field-by-field. Returns list of differences."""
    diffs = []
    all_keys = set(list(old_obj.keys()) + list(new_obj.keys()))

    for key in sorted(all_keys):
        old_val = old_obj.get(key)
        new_val = new_obj.get(key)

        if key not in old_obj:
            # New service has extra field — OK (backward compatible)
            continue
        if key not in new_obj:
            diffs.append(f"MISSING field '{context}{key}' in new service")
            continue

        # Check type match
        if type(old_val) != type(new_val) and old_val is not None and new_val is not None:
            diffs.append(
                f"TYPE mismatch for '{context}{key}': "
                f"old={type(old_val).__name__}, new={type(new_val).__name__}"
            )

    return diffs


def test_health():
    """Test health endpoint — response format is intentionally different."""
    print("\n--- Health Check ---")
    old_r = requests.get(f"{OLD}/health", timeout=30)
    new_r = requests.get(f"{NEW}/health", timeout=30)

    report("Old service reachable", old_r.status_code == 200)
    report("New service reachable", new_r.status_code == 200)

    # Note: response format is intentionally different
    # Old: {"status":"healthy","timestamp":"...","services":{...}}
    # New: {"status":"OK","database":"reachable"}
    warn("Health response format differs (by design)",
         f"Old: {list(old_r.json().keys())}, New: {list(new_r.json().keys())}")


def test_influencer_list():
    """Compare influencer list endpoint — the most critical endpoint."""
    print("\n--- Influencer List (GET /api/v1/influencers) ---")

    old_r = requests.get(f"{OLD}/api/v1/influencers?limit=5", timeout=30)
    new_r = requests.get(f"{NEW}/api/v1/influencers?limit=5", timeout=30)

    report("Old returns 200", old_r.status_code == 200)
    report("New returns 200", new_r.status_code == 200)

    old_data = old_r.json()
    new_data = new_r.json()

    # Check top-level structure
    report("Both have 'influencers' key",
           "influencers" in old_data and "influencers" in new_data)
    report("Both have 'total' key",
           "total" in old_data and "total" in new_data)
    report("Total counts match",
           old_data.get("total") == new_data.get("total"),
           f"Old: {old_data.get('total')}, New: {new_data.get('total')}")

    # Compare first influencer's fields
    if old_data.get("influencers") and new_data.get("influencers"):
        old_inf = old_data["influencers"][0]
        new_inf = new_data["influencers"][0]

        report("Same first influencer ID",
               old_inf.get("id") == new_inf.get("id"),
               f"Old: {old_inf.get('id', '')[:30]}, New: {new_inf.get('id', '')[:30]}")

        # Compare fields
        old_keys = set(old_inf.keys())
        new_keys = set(new_inf.keys())

        missing = old_keys - new_keys
        extra = new_keys - old_keys

        report("No missing fields in new service",
               len(missing) == 0,
               f"Missing: {missing}" if missing else "")
        if extra:
            warn(f"New service has extra fields: {extra} (OK — backward compatible)")

        # Check is_active is string, not bool
        report("is_active is string type",
               isinstance(new_inf.get("is_active"), str),
               f"Type: {type(new_inf.get('is_active')).__name__}")

        # Check field-by-field compatibility
        diffs = compare_fields(old_inf, new_inf, "influencer.")
        report(f"Field types match ({len(diffs)} diffs)",
               len(diffs) == 0,
               "; ".join(diffs) if diffs else "")


def test_influencer_trending():
    """Compare trending endpoint."""
    print("\n--- Trending (GET /api/v1/influencers/trending) ---")

    old_r = requests.get(f"{OLD}/api/v1/influencers/trending?limit=3", timeout=30)
    new_r = requests.get(f"{NEW}/api/v1/influencers/trending?limit=3", timeout=30)

    report("Both return 200",
           old_r.status_code == 200 and new_r.status_code == 200)

    old_data = old_r.json()
    new_data = new_r.json()

    report("Total counts match",
           old_data.get("total") == new_data.get("total"),
           f"Old: {old_data.get('total')}, New: {new_data.get('total')}")

    # Check same top influencer
    if old_data.get("influencers") and new_data.get("influencers"):
        report("Same #1 trending influencer",
               old_data["influencers"][0].get("id") == new_data["influencers"][0].get("id"),
               f"Old: {old_data['influencers'][0].get('display_name')}, "
               f"New: {new_data['influencers'][0].get('display_name')}")


def test_influencer_detail():
    """Compare influencer detail endpoint."""
    print("\n--- Influencer Detail (GET /api/v1/influencers/{id}) ---")

    # Get an influencer ID from the list
    list_r = requests.get(f"{OLD}/api/v1/influencers?limit=1", timeout=30)
    inf_id = list_r.json()["influencers"][0]["id"]

    old_r = requests.get(f"{OLD}/api/v1/influencers/{inf_id}", timeout=30)
    new_r = requests.get(f"{NEW}/api/v1/influencers/{inf_id}", timeout=30)

    report("Both return 200",
           old_r.status_code == 200 and new_r.status_code == 200)

    if old_r.status_code == 200 and new_r.status_code == 200:
        old_data = old_r.json()
        new_data = new_r.json()

        report("Same display_name",
               old_data.get("display_name") == new_data.get("display_name"))
        report("Same category",
               old_data.get("category") == new_data.get("category"))

        # Check field presence
        old_keys = set(old_data.keys())
        new_keys = set(new_data.keys())
        missing = old_keys - new_keys
        report("No critical fields missing",
               len(missing - {"system_prompt", "message_count", "conversation_count",
                             "starter_video_prompt"}) == 0,
               f"Missing: {missing}" if missing else "")


def test_pagination():
    """Verify pagination works identically."""
    print("\n--- Pagination ---")

    # Test offset
    old_r = requests.get(f"{OLD}/api/v1/influencers?limit=1&offset=50", timeout=30)
    new_r = requests.get(f"{NEW}/api/v1/influencers?limit=1&offset=50", timeout=30)

    report("Both return 200",
           old_r.status_code == 200 and new_r.status_code == 200)

    if old_r.status_code == 200 and new_r.status_code == 200:
        old_inf = old_r.json()["influencers"][0] if old_r.json()["influencers"] else None
        new_inf = new_r.json()["influencers"][0] if new_r.json()["influencers"] else None

        if old_inf and new_inf:
            report("Same influencer at offset=50",
                   old_inf["id"] == new_inf["id"],
                   f"Old: {old_inf.get('display_name')}, New: {new_inf.get('display_name')}")


def test_nonexistent():
    """Test 404 for non-existent resources."""
    print("\n--- 404 Handling ---")

    old_r = requests.get(f"{OLD}/api/v1/influencers/nonexistent-id-12345", timeout=30)
    new_r = requests.get(f"{NEW}/api/v1/influencers/nonexistent-id-12345", timeout=30)

    report("Both return 404",
           old_r.status_code == 404 and new_r.status_code == 404,
           f"Old: {old_r.status_code}, New: {new_r.status_code}")


def test_ws_docs():
    """Compare WebSocket documentation endpoint."""
    print("\n--- WebSocket Docs ---")

    old_r = requests.get(f"{OLD}/api/v1/chat/ws/docs", timeout=30)
    new_r = requests.get(f"{NEW}/api/v1/chat/ws/docs", timeout=30)

    report("Both return 200",
           old_r.status_code == 200 and new_r.status_code == 200)

    if old_r.status_code == 200 and new_r.status_code == 200:
        old_events = set(old_r.json().keys())
        new_events = set(new_r.json().keys())
        report("Same event types",
               old_events == new_events,
               f"Old: {old_events}, New: {new_events}")


if __name__ == "__main__":
    print("=" * 60)
    print(" API Compatibility Test: Old vs New Chat Service")
    print("=" * 60)

    test_health()
    test_influencer_list()
    test_influencer_trending()
    test_influencer_detail()
    test_pagination()
    test_nonexistent()
    test_ws_docs()

    print("\n" + "=" * 60)
    print(f" Results: {PASS} passed, {FAIL} failed, {WARN} warnings")
    print("=" * 60)

    sys.exit(1 if FAIL > 0 else 0)
