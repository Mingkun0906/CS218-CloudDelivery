#!/usr/bin/env python3
"""FL-08: Lambda timeout injection → API returns 503.

Forces Lambda timeouts by temporarily setting the function timeout to 1s,
then submitting requests that take longer to process. The Lambda times out,
API Gateway receives a 502/504, and the handler's error path returns 503.

TEST_CASES.md FL-08:
  Failure  : Lambda timeout (set to 1s); submit POST /orders
  Expected : Lambda times out; CloudWatch LambdaErrors spike;
             API returns 503; no partial order in DynamoDB
  Evidence : CloudWatch Lambda Errors + Duration > Timeout; DynamoDB scan = 0

Usage:
  python fl08_lambda_timeout.py --inject     # set OrderAPILambda timeout to 1s
  python fl08_lambda_timeout.py --restore    # restore default 30s timeout
  python fl08_lambda_timeout.py --status     # show current timeout config
  python fl08_lambda_timeout.py --probe      # send a test POST /orders and print response

Prerequisites:
  aws configure
  export API_ENDPOINT=https://<api-id>.execute-api.us-east-1.amazonaws.com/dev
  export API_TOKEN=<cognito-jwt>   # (optional; /health probe works without auth)
"""
import argparse
import json
import os
import sys
import time

import boto3

REGION = "us-east-1"
ORDER_API_FUNCTION = "clouddelivery-order-api-dev"
DEFAULT_TIMEOUT = 30   # seconds
INJECT_TIMEOUT = 1     # seconds — forces timeout on any DynamoDB/Kinesis call


def get_current_config(lambda_client):
    resp = lambda_client.get_function_configuration(FunctionName=ORDER_API_FUNCTION)
    return {
        "Timeout": resp["Timeout"],
        "MemorySize": resp["MemorySize"],
        "LastModified": resp["LastModified"],
        "State": resp.get("State"),
    }


def inject_timeout(lambda_client):
    cfg = get_current_config(lambda_client)
    print(f"[FL-08] Current timeout: {cfg['Timeout']}s")
    print(f"[FL-08] Setting {ORDER_API_FUNCTION} timeout to {INJECT_TIMEOUT}s...")
    lambda_client.update_function_configuration(
        FunctionName=ORDER_API_FUNCTION,
        Timeout=INJECT_TIMEOUT,
    )
    _wait_for_active(lambda_client)
    print(f"[FL-08] ✓ Timeout set to {INJECT_TIMEOUT}s.")
    print("[FL-08] Submit a POST /orders now — Lambda will time out before DynamoDB write.")
    print("[FL-08] Expected: API returns 502/504; CloudWatch Lambda Errors spike.")
    print("[FL-08] Run --restore when done.")


def restore(lambda_client):
    print(f"[FL-08] Restoring {ORDER_API_FUNCTION} timeout to {DEFAULT_TIMEOUT}s...")
    lambda_client.update_function_configuration(
        FunctionName=ORDER_API_FUNCTION,
        Timeout=DEFAULT_TIMEOUT,
    )
    _wait_for_active(lambda_client)
    cfg = get_current_config(lambda_client)
    print(f"[FL-08] ✓ Restored. Current timeout: {cfg['Timeout']}s")
    print("[FL-08] EVIDENCE TO CAPTURE:")
    print("  1. CloudWatch > Lambda > Errors — spike during injection window")
    print("  2. CloudWatch > Lambda > Duration — values near/above 1000ms")
    print("  3. API response body showing 502/504 or 503 from error handler")
    print("  4. DynamoDB scan during injection: Count should be 0 (no partial writes)")
    print(f"  5. Save to docs/failure_evidence/FL-08/")


def _wait_for_active(lambda_client, timeout=30):
    start = time.time()
    while time.time() - start < timeout:
        resp = lambda_client.get_function_configuration(FunctionName=ORDER_API_FUNCTION)
        last_update = resp.get("LastUpdateStatus", "Successful")
        if last_update == "Successful":
            return
        time.sleep(2)


def status(lambda_client):
    cfg = get_current_config(lambda_client)
    print(f"[FL-08] Function: {ORDER_API_FUNCTION}")
    print(f"        Timeout:      {cfg['Timeout']}s  (default: {DEFAULT_TIMEOUT}s)")
    print(f"        MemorySize:   {cfg['MemorySize']}MB")
    print(f"        LastModified: {cfg['LastModified']}")
    if cfg["Timeout"] <= INJECT_TIMEOUT:
        print("        *** INJECTION ACTIVE — timeout is below normal ***")


def probe():
    """Send a test request to /health (no auth required) to confirm Lambda is alive."""
    api_endpoint = os.environ.get("API_ENDPOINT")
    if not api_endpoint:
        print("[FL-08] Set API_ENDPOINT env var to probe the live endpoint.")
        print("[FL-08] Example: export API_ENDPOINT=https://<id>.execute-api.us-east-1.amazonaws.com/dev")
        sys.exit(1)

    try:
        import urllib.request
        url = api_endpoint.rstrip("/") + "/health"
        print(f"[FL-08] Probing {url}...")
        start = time.time()
        with urllib.request.urlopen(url, timeout=5) as resp:
            elapsed = (time.time() - start) * 1000
            body = resp.read().decode()
            print(f"[FL-08] Status: {resp.status}  ({elapsed:.0f}ms)")
            print(f"[FL-08] Body: {body}")
    except Exception as exc:
        elapsed = (time.time() - start) * 1000
        print(f"[FL-08] ✗ Request failed after {elapsed:.0f}ms: {exc}")
        print("[FL-08] EVIDENCE: Lambda timed out or returned 502/504.")


def main():
    parser = argparse.ArgumentParser(description="FL-08 Lambda timeout injection")
    parser.add_argument("--inject", action="store_true", help="Set timeout to 1s")
    parser.add_argument("--restore", action="store_true", help="Restore 30s timeout")
    parser.add_argument("--status", action="store_true", help="Show current config")
    parser.add_argument("--probe", action="store_true", help="Send test request to /health")
    args = parser.parse_args()

    lam = boto3.client("lambda", region_name=REGION)

    if args.inject:
        inject_timeout(lam)
    elif args.restore:
        restore(lam)
    elif args.status:
        status(lam)
    elif args.probe:
        probe()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
