#!/usr/bin/env python3
"""FL-07: Kinesis consumer pause → iterator expiry → replay from retention.

Disables the Lambda event source mapping on the order stream to simulate
a consumer group outage. While disabled, new Kinesis records accumulate
(up to 24h retention). On restore, the event source mapping re-enables
and Lambda replays from the last checkpoint — verifying no records are lost.

TEST_CASES.md FL-07:
  Failure  : Disable Kinesis→MatchingLambda event source mapping (~1 min);
             submit orders during the pause
  Expected : MatchingLambda processes all buffered records after re-enable;
             IteratorAgeMilliseconds spikes then returns to 0; 0 data loss
  Evidence : CloudWatch > Kinesis > IteratorAge spike + recovery;
             CloudWatch Logs: all orders eventually matched

Usage:
  python fl07_kinesis_pause.py --inject    # disable event source mapping
  python fl07_kinesis_pause.py --restore   # re-enable event source mapping
  python fl07_kinesis_pause.py --status    # show ESM state + iterator age

Prerequisites: aws configure
"""
import argparse
import sys
import time

import boto3

REGION = "us-east-1"
FUNCTION_NAME = "clouddelivery-matching-dev"
ORDER_STREAM_NAME = "clouddelivery-orders-dev"
POLL_INTERVAL = 5
MAX_WAIT = 120


def _find_esm_uuid(lambda_client):
    """Find the UUID of the Kinesis event source mapping for MatchingLambda."""
    paginator = lambda_client.get_paginator("list_event_source_mappings")
    for page in paginator.paginate(FunctionName=FUNCTION_NAME):
        for esm in page["EventSourceMappings"]:
            if ORDER_STREAM_NAME in esm.get("EventSourceArn", ""):
                return esm["UUID"]
    return None


def inject_pause(lambda_client):
    uuid = _find_esm_uuid(lambda_client)
    if not uuid:
        print(f"[FL-07] ERROR: No ESM found for {FUNCTION_NAME} ← {ORDER_STREAM_NAME}")
        sys.exit(1)

    print(f"[FL-07] Disabling event source mapping {uuid}...")
    lambda_client.update_event_source_mapping(UUID=uuid, Enabled=False)

    # Wait for the ESM to reach Disabled state.
    print("[FL-07] Waiting for ESM to reach Disabled state...")
    _wait_for_esm_state(lambda_client, uuid, target="Disabled")
    print("[FL-07] ✓ ESM disabled. MatchingLambda will no longer consume new records.")
    print("[FL-07] Submit orders now (using the Locust burst test or Kinesis inject).")
    print("[FL-07] EVIDENCE: CloudWatch > Kinesis > IteratorAgeMilliseconds will climb.")
    print("[FL-07] Run --restore when done (records buffered in Kinesis for 24h).")


def restore(lambda_client):
    uuid = _find_esm_uuid(lambda_client)
    if not uuid:
        print(f"[FL-07] ERROR: No ESM found for {FUNCTION_NAME} ← {ORDER_STREAM_NAME}")
        sys.exit(1)

    print(f"[FL-07] Re-enabling event source mapping {uuid}...")
    lambda_client.update_event_source_mapping(UUID=uuid, Enabled=True)

    _wait_for_esm_state(lambda_client, uuid, target="Enabled")
    print("[FL-07] ✓ ESM re-enabled. Lambda will replay buffered records.")
    print("[FL-07] Monitor IteratorAgeMilliseconds — it should spike then drop to 0.")
    print("[FL-07] EVIDENCE TO CAPTURE:")
    print("  1. CloudWatch > Kinesis > IteratorAgeMilliseconds — spike then recovery")
    print("  2. CloudWatch Logs > MatchingLambda — order match events during catch-up")
    print("  3. DynamoDB scan count: verify all submitted orders were matched")
    print(f"  4. Save to docs/failure_evidence/FL-07/")


def _wait_for_esm_state(lambda_client, uuid, target, timeout=60):
    start = time.time()
    while time.time() - start < timeout:
        resp = lambda_client.get_event_source_mapping(UUID=uuid)
        state = resp["State"]
        elapsed = int(time.time() - start)
        print(f"  [{elapsed:3d}s] ESM state = {state}")
        if state == target:
            return
        time.sleep(POLL_INTERVAL)
    print(f"[FL-07] WARNING: ESM did not reach '{target}' within {timeout}s")


def status(lambda_client, cw_client):
    uuid = _find_esm_uuid(lambda_client)
    if not uuid:
        print(f"[FL-07] No ESM found for {FUNCTION_NAME} ← {ORDER_STREAM_NAME}")
        return

    resp = lambda_client.get_event_source_mapping(UUID=uuid)
    print(f"[FL-07] ESM UUID:  {uuid}")
    print(f"        State:     {resp['State']}")
    print(f"        Batch:     {resp.get('BatchSize')}")
    print(f"        Position:  {resp.get('StartingPosition')}")

    import datetime
    age_resp = cw_client.get_metric_statistics(
        Namespace="AWS/Kinesis",
        MetricName="GetRecords.IteratorAgeMilliseconds",
        Dimensions=[{"Name": "StreamName", "Value": ORDER_STREAM_NAME}],
        StartTime=datetime.datetime.utcnow() - datetime.timedelta(minutes=5),
        EndTime=datetime.datetime.utcnow(),
        Period=60,
        Statistics=["Maximum"],
    )
    datapoints = age_resp.get("Datapoints", [])
    if datapoints:
        latest = sorted(datapoints, key=lambda d: d["Timestamp"])[-1]
        print(f"        IteratorAge (last 5m max): {latest['Maximum']}ms")
    else:
        print("        IteratorAge: no data")


def main():
    parser = argparse.ArgumentParser(description="FL-07 Kinesis consumer pause injection")
    parser.add_argument("--inject", action="store_true", help="Disable ESM (pause consumer)")
    parser.add_argument("--restore", action="store_true", help="Re-enable ESM")
    parser.add_argument("--status", action="store_true", help="Show ESM state + iterator age")
    args = parser.parse_args()

    lam = boto3.client("lambda", region_name=REGION)
    cw = boto3.client("cloudwatch", region_name=REGION)

    if args.inject:
        inject_pause(lam)
    elif args.restore:
        restore(lam)
    elif args.status:
        status(lam, cw)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
