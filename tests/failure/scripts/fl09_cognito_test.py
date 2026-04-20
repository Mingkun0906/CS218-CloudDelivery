#!/usr/bin/env python3
"""FL-09: Cognito outage simulation → missing/invalid JWT → 400/401.

Tests API behavior when Cognito is unavailable or tokens are malformed:
  1. No Authorization header → API Gateway returns 401 (Unauthorized)
  2. Malformed JWT (not a real token) → API Gateway returns 401
  3. Expired token (if provided) → API Gateway returns 401
  4. /health endpoint → always 200 (no auth required)

The application-layer guard (empty sub claim → 400) is exercised by the
mock tests in tests/failure/test_api_failures.py. This script tests the
live API Gateway + Cognito integration layer.

TEST_CASES.md FL-09:
  Failure  : Cognito outage → API Gateway cannot validate JWT
  Expected : Requests with missing/invalid token → 401; /health stays 200;
             No partial orders created in DynamoDB
  Evidence : API Gateway 401 responses in CloudWatch Access Logs;
             DynamoDB scan = 0 for any order IDs attempted during outage

Usage:
  python fl09_cognito_test.py --run   # execute all probe cases

Prerequisites:
  export API_ENDPOINT=https://<api-id>.execute-api.us-east-1.amazonaws.com/dev
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error

REGION = "us-east-1"

VALID_ORDER_BODY = json.dumps({
    "restaurantId": "rst_FL09_TEST_000000000001",
    "pickupLocation": {"lat": 34.0522, "lng": -118.2437, "address": "123 Main St"},
    "dropoffLocation": {"lat": 34.0611, "lng": -118.2400, "address": "456 Oak Ave"},
    "items": [{"itemId": "item_1", "name": "Burger", "quantity": 1, "unitPriceCents": 1299}],
}).encode()


def _do_request(url, method="GET", headers=None, body=None):
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=body)
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            elapsed = (time.time() - start) * 1000
            return resp.status, resp.read().decode(), elapsed
    except urllib.error.HTTPError as exc:
        elapsed = (time.time() - start) * 1000
        return exc.code, exc.read().decode(), elapsed
    except Exception as exc:
        elapsed = (time.time() - start) * 1000
        return None, str(exc), elapsed


def run_probes(api_endpoint):
    base = api_endpoint.rstrip("/")
    results = []

    # Case 1: /health — no auth required, must return 200.
    print("[FL-09] Case 1: GET /health (no auth) — expect 200")
    status, body, ms = _do_request(f"{base}/health")
    _report(1, status, body, ms, expect=200)
    results.append(("health_no_auth", status, status == 200))

    # Case 2: POST /orders — no Authorization header → 401.
    print("[FL-09] Case 2: POST /orders (no Authorization header) — expect 401")
    status, body, ms = _do_request(
        f"{base}/orders",
        method="POST",
        headers={"Content-Type": "application/json"},
        body=VALID_ORDER_BODY,
    )
    _report(2, status, body, ms, expect=401)
    results.append(("post_no_auth", status, status == 401))

    # Case 3: POST /orders — malformed JWT → 401.
    print("[FL-09] Case 3: POST /orders (malformed JWT) — expect 401")
    status, body, ms = _do_request(
        f"{base}/orders",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer this.is.not.a.real.jwt",
        },
        body=VALID_ORDER_BODY,
    )
    _report(3, status, body, ms, expect=401)
    results.append(("post_malformed_jwt", status, status == 401))

    # Case 4: GET /orders/ord_test — no auth → 401.
    print("[FL-09] Case 4: GET /orders/ord_fl09_test (no auth) — expect 401")
    status, body, ms = _do_request(f"{base}/orders/ord_fl09_test")
    _report(4, status, body, ms, expect=401)
    results.append(("get_no_auth", status, status == 401))

    # Case 5: PUT /couriers/{id}/location — no auth → 401.
    print("[FL-09] Case 5: PUT /couriers/cur_fl09_test/location (no auth) — expect 401")
    loc_body = json.dumps({"lat": 34.0522, "lng": -118.2437}).encode()
    status, body, ms = _do_request(
        f"{base}/couriers/cur_fl09_test/location",
        method="PUT",
        headers={"Content-Type": "application/json"},
        body=loc_body,
    )
    _report(5, status, body, ms, expect=401)
    results.append(("put_location_no_auth", status, status == 401))

    # Summary
    print("\n[FL-09] === Summary ===")
    passed = sum(1 for _, _, ok in results if ok)
    for name, actual, ok in results:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}: HTTP {actual}")

    print(f"\n[FL-09] {passed}/{len(results)} cases passed.")
    print("[FL-09] EVIDENCE TO CAPTURE:")
    print("  1. Terminal output above (screenshot or copy)")
    print("  2. CloudWatch > API Gateway > 4XXError — spike of 401s")
    print("  3. CloudWatch Access Logs — filter '\"status\":401'")
    print("  4. DynamoDB scan: no orders with restaurantId=rst_FL09_TEST_000000000001")
    print(f"  5. Save to docs/failure_evidence/FL-09/")

    return passed == len(results)


def _report(case_num, status, body, ms, expect):
    ok = status == expect
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] HTTP {status} ({ms:.0f}ms)")
    if not ok:
        preview = body[:200].replace("\n", " ")
        print(f"         Expected {expect}, got {status}: {preview}")


def main():
    parser = argparse.ArgumentParser(description="FL-09 Cognito outage simulation")
    parser.add_argument("--run", action="store_true", help="Run all probe cases")
    args = parser.parse_args()

    if not args.run:
        parser.print_help()
        sys.exit(0)

    api_endpoint = os.environ.get("API_ENDPOINT")
    if not api_endpoint:
        print("[FL-09] ERROR: Set API_ENDPOINT environment variable.")
        print("[FL-09]   export API_ENDPOINT=https://<id>.execute-api.us-east-1.amazonaws.com/dev")
        sys.exit(1)

    print(f"[FL-09] Running Cognito outage probes against: {api_endpoint}")
    success = run_probes(api_endpoint)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
