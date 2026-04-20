#!/usr/bin/env python3
"""FL-04: DynamoDB throttling injection.

Switches the orders table to provisioned mode with 1 WCU to force
ThrottledRequests, then restores on-demand billing.

TEST_CASES.md FL-04:
  Failure  : Artificially limit table capacity; submit burst of writes
  Expected : Throttled requests caught by retry logic with exponential backoff
  Evidence : No data loss; all orders eventually written; DLQ depth stays 0

Usage:
  python fl04_dynamodb_throttle.py --inject    # switch to 1 WCU provisioned
  python fl04_dynamodb_throttle.py --restore   # restore PAY_PER_REQUEST
  python fl04_dynamodb_throttle.py --status    # show current billing mode

Prerequisites: aws configure
"""
import argparse
import sys
import time

import boto3

TABLE_NAME = "clouddelivery-dev"
REGION = "us-east-1"


def get_billing_mode(ddb_client):
    resp = ddb_client.describe_table(TableName=TABLE_NAME)
    mode = resp["Table"].get("BillingModeSummary", {}).get("BillingMode", "PROVISIONED")
    throughput = resp["Table"].get("ProvisionedThroughput", {})
    return mode, throughput


def inject_throttle(ddb_client):
    mode, _ = get_billing_mode(ddb_client)
    print(f"[FL-04] Current billing mode: {mode}")
    print(f"[FL-04] Switching {TABLE_NAME} to PROVISIONED with 1 WCU / 1 RCU...")
    ddb_client.update_table(
        TableName=TABLE_NAME,
        BillingMode="PROVISIONED",
        ProvisionedThroughput={"ReadCapacityUnits": 1, "WriteCapacityUnits": 1},
    )
    # Wait for the table to become ACTIVE again after the update.
    print("[FL-04] Waiting for table to become ACTIVE...")
    waiter = ddb_client.get_waiter("table_exists")
    waiter.wait(TableName=TABLE_NAME)
    print("[FL-04] ✓ Table is ACTIVE with 1 WCU. Submit burst writes now to trigger throttling.")
    print("[FL-04] Run a Locust burst test or use the Kinesis inject script.")
    print("[FL-04] EVIDENCE: CloudWatch > DynamoDB > ThrottledRequests — watch spike.")
    print("[FL-04] Run --restore when done.")


def restore(ddb_client):
    print(f"[FL-04] Restoring {TABLE_NAME} to PAY_PER_REQUEST (on-demand)...")
    ddb_client.update_table(
        TableName=TABLE_NAME,
        BillingMode="PAY_PER_REQUEST",
    )
    waiter = ddb_client.get_waiter("table_exists")
    waiter.wait(TableName=TABLE_NAME)
    mode, _ = get_billing_mode(ddb_client)
    print(f"[FL-04] ✓ Restored. Current mode: {mode}")
    print("[FL-04] EVIDENCE TO CAPTURE:")
    print("  1. CloudWatch > DynamoDB > ThrottledRequests — spike then return to 0")
    print("  2. CloudWatch Logs — filter 'ProvisionedThroughputExceededException'")
    print("  3. Check DLQ depth stayed at 0 (no data loss)")
    print(f"  4. Save to docs/failure_evidence/FL-04/")


def status(ddb_client):
    mode, throughput = get_billing_mode(ddb_client)
    print(f"[FL-04] Table: {TABLE_NAME}")
    print(f"        BillingMode: {mode}")
    if throughput:
        print(f"        ReadCapacityUnits:  {throughput.get('ReadCapacityUnits')}")
        print(f"        WriteCapacityUnits: {throughput.get('WriteCapacityUnits')}")


def main():
    parser = argparse.ArgumentParser(description="FL-04 DynamoDB throttle injection")
    parser.add_argument("--inject", action="store_true")
    parser.add_argument("--restore", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    ddb = boto3.client("dynamodb", region_name=REGION)

    if args.inject:
        inject_throttle(ddb)
    elif args.restore:
        restore(ddb)
    elif args.status:
        status(ddb)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
