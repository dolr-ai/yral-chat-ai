#!/usr/bin/env python3
"""
Phase T2: Load Test — Simulate 200 concurrent users.

Sends concurrent requests to verify the service handles production traffic
without errors, timeouts, or crashes.

Usage:
    python3 tests/test_load.py
    python3 tests/test_load.py --concurrency 100  # custom concurrency

No authentication needed — tests public endpoints only.
"""

import argparse
import json
import time
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
import requests

BASE = "https://chat-ai.rishi.yral.com"


def make_request(url, timeout=15):
    """Make a single HTTP request and return (status_code, latency_ms, error)."""
    start = time.time()
    try:
        r = requests.get(url, timeout=timeout)
        latency = (time.time() - start) * 1000
        return (r.status_code, latency, None)
    except requests.Timeout:
        return (0, (time.time() - start) * 1000, "TIMEOUT")
    except requests.ConnectionError:
        return (0, (time.time() - start) * 1000, "CONNECTION_REFUSED")
    except Exception as e:
        return (0, (time.time() - start) * 1000, str(type(e).__name__))


def run_load_test(name, url, concurrency, total_requests):
    """Run a load test against a single endpoint."""
    print(f"\n--- {name} ({total_requests} requests, {concurrency} concurrent) ---")

    results = []
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(make_request, url) for _ in range(total_requests)]
        for f in as_completed(futures):
            results.append(f.result())

    total_time = time.time() - start_time

    # Analyze results
    statuses = Counter(r[0] for r in results)
    errors = Counter(r[2] for r in results if r[2])
    latencies = [r[1] for r in results if r[2] is None]

    if latencies:
        latencies.sort()
        p50 = latencies[len(latencies) // 2]
        p95 = latencies[int(len(latencies) * 0.95)]
        p99 = latencies[int(len(latencies) * 0.99)]
        max_lat = max(latencies)
    else:
        p50 = p95 = p99 = max_lat = 0

    rps = total_requests / total_time if total_time > 0 else 0
    success_count = statuses.get(200, 0)
    error_5xx = sum(v for k, v in statuses.items() if 500 <= k < 600)
    conn_errors = sum(errors.values())

    print(f"  Total time:    {total_time:.1f}s")
    print(f"  Requests/sec:  {rps:.1f}")
    print(f"  Success (200): {success_count}/{total_requests}")
    print(f"  5xx errors:    {error_5xx}")
    print(f"  Conn errors:   {conn_errors}")
    print(f"  Latency p50:   {p50:.0f}ms")
    print(f"  Latency p95:   {p95:.0f}ms")
    print(f"  Latency p99:   {p99:.0f}ms")
    print(f"  Latency max:   {max_lat:.0f}ms")

    if statuses:
        print(f"  Status codes:  {dict(statuses)}")
    if errors:
        print(f"  Error types:   {dict(errors)}")

    # Pass/fail
    passed = True
    issues = []

    if error_5xx > 0:
        passed = False
        issues.append(f"{error_5xx} server errors (5xx)")
    if conn_errors > 0:
        passed = False
        issues.append(f"{conn_errors} connection errors")
    if p95 > 2000:
        passed = False
        issues.append(f"p95 latency {p95:.0f}ms > 2000ms")
    if success_count < total_requests * 0.95:
        passed = False
        issues.append(f"Success rate {success_count/total_requests*100:.1f}% < 95%")

    if passed:
        print(f"  Result: PASS")
    else:
        print(f"  Result: FAIL — {', '.join(issues)}")

    return passed


def main():
    parser = argparse.ArgumentParser(description="Load test for YRAL chat service")
    parser.add_argument("--concurrency", type=int, default=200, help="Concurrent users")
    parser.add_argument("--requests", type=int, default=500, help="Total requests per test")
    args = parser.parse_args()

    C = args.concurrency
    N = args.requests

    print("=" * 60)
    print(f" Load Test: {C} concurrent users, {N} requests per test")
    print(f" Target: {BASE}")
    print("=" * 60)

    all_pass = True

    # Test 1: Health endpoint (lightest — DB health check)
    if not run_load_test("Health Check", f"{BASE}/health", C, N):
        all_pass = False

    # Test 2: Influencer list (moderate — DB query + JSON serialization)
    if not run_load_test("Influencer List", f"{BASE}/api/v1/influencers?limit=20", C, N):
        all_pass = False

    # Test 3: Trending (heavier — subqueries for message counts)
    if not run_load_test("Trending", f"{BASE}/api/v1/influencers/trending?limit=20", C, N):
        all_pass = False

    # Test 4: Mixed workload
    print(f"\n--- Mixed Workload ({N} total requests, {C} concurrent) ---")
    urls = [
        f"{BASE}/health",
        f"{BASE}/api/v1/influencers?limit=10",
        f"{BASE}/api/v1/influencers/trending?limit=10",
        f"{BASE}/status",
    ]

    results = []
    start_time = time.time()
    with ThreadPoolExecutor(max_workers=C) as pool:
        futures = [pool.submit(make_request, urls[i % len(urls)])
                   for i in range(N)]
        for f in as_completed(futures):
            results.append(f.result())

    total_time = time.time() - start_time
    success = sum(1 for r in results if r[0] == 200)
    errors_5xx = sum(1 for r in results if 500 <= r[0] < 600)
    latencies = sorted(r[1] for r in results if r[2] is None)

    print(f"  Total time:    {total_time:.1f}s")
    print(f"  Requests/sec:  {N / total_time:.1f}")
    print(f"  Success:       {success}/{N}")
    print(f"  5xx errors:    {errors_5xx}")
    if latencies:
        print(f"  Latency p95:   {latencies[int(len(latencies) * 0.95)]:.0f}ms")

    if errors_5xx > 0 or success < N * 0.95:
        all_pass = False
        print(f"  Result: FAIL")
    else:
        print(f"  Result: PASS")

    print("\n" + "=" * 60)
    print(f" Overall: {'ALL PASS' if all_pass else 'SOME FAILED'}")
    print("=" * 60)

    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
