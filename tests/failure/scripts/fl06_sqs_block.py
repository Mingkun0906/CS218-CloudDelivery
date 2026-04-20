#!/usr/bin/env python3
"""FL-06: SQS notification queue block → DLQ fill → redrive recovery.

Blocks the NotificationQueue by replacing its queue policy with a Deny-all
statement (simulating a VPC policy outage or revoked SNS permission).
SNS publish to the queue will fail; any messages already in-flight get
retried up to maxReceiveCount=3, then land in the DLQ.

TEST_CASES.md FL-06:
  Failure  : Block SQS receive (deny policy) so NotificationLambda cannot
             consume messages; messages exhaust retries → DLQ
  Expected : CloudWatch alarm fires on DLQ depth > 0; SQS redrive restores
             all messages; DLQ depth returns to 0
  Evidence : DLQ ApproximateNumberOfMessagesVisible spike then 0 after redrive

Usage:
  python fl06_sqs_block.py --inject    # apply Deny-all queue policy
  python fl06_sqs_block.py --restore   # restore Allow SNS policy
  python fl06_sqs_block.py --status    # show DLQ depth
  python fl06_sqs_block.py --redrive   # redrive all DLQ messages back to main queue

Prerequisites: aws configure
"""
import argparse
import json
import sys
import time

import boto3

REGION = "us-east-1"
ACCOUNT_ID = "043254348860"
NOTIFICATION_QUEUE_NAME = "clouddelivery-notifications-dev"
NOTIFICATION_DLQ_NAME = "clouddelivery-notifications-dlq-dev"
SNS_TOPIC_ARN = f"arn:aws:sns:{REGION}:{ACCOUNT_ID}:clouddelivery-match-notifications-dev"

NOTIFICATION_QUEUE_URL = (
    f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT_ID}/{NOTIFICATION_QUEUE_NAME}"
)
NOTIFICATION_DLQ_URL = (
    f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT_ID}/{NOTIFICATION_DLQ_NAME}"
)
NOTIFICATION_QUEUE_ARN = (
    f"arn:aws:sqs:{REGION}:{ACCOUNT_ID}:{NOTIFICATION_QUEUE_NAME}"
)

# Original policy: allow SNS to send messages.
ALLOW_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "AllowSNSPublish",
            "Effect": "Allow",
            "Principal": {"Service": "sns.amazonaws.com"},
            "Action": "sqs:SendMessage",
            "Resource": NOTIFICATION_QUEUE_ARN,
            "Condition": {
                "ArnEquals": {"aws:SourceArn": SNS_TOPIC_ARN}
            },
        }
    ],
}

# Injected policy: deny everyone (simulates VPC policy revocation).
DENY_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "DenyAll",
            "Effect": "Deny",
            "Principal": "*",
            "Action": "sqs:SendMessage",
            "Resource": NOTIFICATION_QUEUE_ARN,
        }
    ],
}


def inject_block(sqs_client):
    print(f"[FL-06] Injecting Deny-all policy on {NOTIFICATION_QUEUE_NAME}...")
    sqs_client.set_queue_attributes(
        QueueUrl=NOTIFICATION_QUEUE_URL,
        Attributes={"Policy": json.dumps(DENY_POLICY)},
    )
    print("[FL-06] ✓ Queue policy set to Deny-all.")
    print("[FL-06] Now trigger a few order matches to generate SNS→SQS publishes.")
    print("[FL-06] SNS will receive delivery failures; in-flight messages will")
    print("[FL-06] exhaust retries (maxReceiveCount=3) and land in the DLQ.")
    print("[FL-06] Monitor: aws sqs get-queue-attributes \\")
    print(f"[FL-06]   --queue-url {NOTIFICATION_DLQ_URL} \\")
    print("[FL-06]   --attribute-names ApproximateNumberOfMessagesVisible")
    print("[FL-06] Run --restore when done, then --redrive to recover messages.")


def restore(sqs_client):
    print(f"[FL-06] Restoring Allow policy on {NOTIFICATION_QUEUE_NAME}...")
    sqs_client.set_queue_attributes(
        QueueUrl=NOTIFICATION_QUEUE_URL,
        Attributes={"Policy": json.dumps(ALLOW_POLICY)},
    )
    print("[FL-06] ✓ Queue policy restored (SNS can publish again).")
    print("[FL-06] Run --redrive to move DLQ messages back to the main queue.")


def redrive(sqs_client):
    """Start a SQS message move task to redrive all DLQ messages."""
    print(f"[FL-06] Starting SQS message move task: DLQ → {NOTIFICATION_QUEUE_NAME}...")
    try:
        resp = sqs_client.start_message_move_task(
            SourceArn=f"arn:aws:sqs:{REGION}:{ACCOUNT_ID}:{NOTIFICATION_DLQ_NAME}",
            DestinationArn=NOTIFICATION_QUEUE_ARN,
        )
        task_handle = resp.get("TaskHandle", "n/a")
        print(f"[FL-06] ✓ Redrive task started: {task_handle}")
        print("[FL-06] Polling DLQ depth until empty (up to 120s)...")
        _poll_dlq_depth(sqs_client, target=0, timeout=120)
    except sqs_client.exceptions.SqsMoveTaskAlreadyExists:
        print("[FL-06] Redrive task already in progress. Check AWS console.")


def _poll_dlq_depth(sqs_client, target=0, timeout=120):
    start = time.time()
    while time.time() - start < timeout:
        attrs = sqs_client.get_queue_attributes(
            QueueUrl=NOTIFICATION_DLQ_URL,
            AttributeNames=["ApproximateNumberOfMessagesVisible"],
        )
        depth = int(attrs["Attributes"].get("ApproximateNumberOfMessagesVisible", -1))
        elapsed = int(time.time() - start)
        print(f"  [{elapsed:3d}s] DLQ depth = {depth}")
        if depth == target:
            print(f"\n[FL-06] ✓ DLQ depth reached {target} at {elapsed}s.")
            return elapsed
        time.sleep(5)
    print(f"\n[FL-06] ✗ DLQ did not reach {target} within {timeout}s.")
    return None


def status(sqs_client):
    for name, url in [
        (NOTIFICATION_QUEUE_NAME, NOTIFICATION_QUEUE_URL),
        (NOTIFICATION_DLQ_NAME, NOTIFICATION_DLQ_URL),
    ]:
        attrs = sqs_client.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=[
                "ApproximateNumberOfMessagesVisible",
                "ApproximateNumberOfMessagesNotVisible",
                "Policy",
            ],
        )["Attributes"]
        print(f"[FL-06] Queue: {name}")
        print(f"        Visible:    {attrs.get('ApproximateNumberOfMessagesVisible')}")
        print(f"        In-flight:  {attrs.get('ApproximateNumberOfMessagesNotVisible')}")
        policy = attrs.get("Policy")
        if policy:
            stmts = json.loads(policy).get("Statement", [])
            effects = [s["Effect"] for s in stmts]
            print(f"        Policy effects: {effects}")
        else:
            print("        Policy: (none)")


def main():
    parser = argparse.ArgumentParser(description="FL-06 SQS block injection")
    parser.add_argument("--inject", action="store_true", help="Apply Deny-all policy")
    parser.add_argument("--restore", action="store_true", help="Restore Allow SNS policy")
    parser.add_argument("--status", action="store_true", help="Show queue depths and policy")
    parser.add_argument("--redrive", action="store_true", help="Redrive DLQ → main queue")
    args = parser.parse_args()

    sqs = boto3.client("sqs", region_name=REGION)

    if args.inject:
        inject_block(sqs)
    elif args.restore:
        restore(sqs)
    elif args.status:
        status(sqs)
    elif args.redrive:
        redrive(sqs)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
