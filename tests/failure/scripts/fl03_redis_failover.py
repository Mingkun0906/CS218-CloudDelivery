#!/usr/bin/env python3
"""FL-03: Redis primary node failure injection.

Triggers an ElastiCache primary-node failover and monitors recovery.

TEST_CASES.md FL-03:
  Failure  : Kill ElastiCache primary via AWS console (or this script)
  Expected : Replica promotes (~15–30s); geo-queries fail temporarily;
             Kinesis retries unacknowledged records
  Evidence : CloudWatch ReplicationLag drops to 0 after promotion;
             system resumes geo-queries after failover

Usage:
  # Inject failover
  python fl03_redis_failover.py --inject

  # Monitor only (no failover)
  python fl03_redis_failover.py --monitor

Prerequisites:
  aws configure  (or AWS_PROFILE env var set)
  pip install boto3
"""
import argparse
import sys
import time

import boto3

# ── Config (from config/dev.json) ────────────────────────────────────────────
REPLICATION_GROUP_ID = "clouddelivery-redis-dev"
NODE_GROUP_ID = "0001"
REGION = "us-east-1"
POLL_INTERVAL = 5   # seconds between status checks
MAX_WAIT = 120      # seconds to wait for failover completion


def inject_failover(ec_client):
    print(f"[FL-03] Triggering failover on {REPLICATION_GROUP_ID} node {NODE_GROUP_ID}...")
    try:
        ec_client.test_failover(
            ReplicationGroupId=REPLICATION_GROUP_ID,
            NodeGroupId=NODE_GROUP_ID,
        )
        print("[FL-03] Failover initiated. Monitoring recovery...")
    except ec_client.exceptions.TestFailoverNotAvailableException:
        print("[FL-03] ERROR: Failover not available (cluster may be single-node or "
              "Multi-AZ not enabled). Check ElastiCache config.")
        sys.exit(1)


def monitor_replication_lag(cw_client, duration=MAX_WAIT):
    """Poll CloudWatch ReplicationLag metric until it returns to 0."""
    print(f"\n[FL-03] Monitoring ReplicationLag for {duration}s "
          "(target: drops to 0 after promotion)...")

    import datetime
    start = time.time()
    while time.time() - start < duration:
        resp = cw_client.get_metric_statistics(
            Namespace="AWS/ElastiCache",
            MetricName="ReplicationLag",
            Dimensions=[{"Name": "ReplicationGroupId", "Value": REPLICATION_GROUP_ID}],
            StartTime=datetime.datetime.utcnow() - datetime.timedelta(minutes=2),
            EndTime=datetime.datetime.utcnow(),
            Period=60,
            Statistics=["Maximum"],
        )
        datapoints = resp.get("Datapoints", [])
        lag = datapoints[-1]["Maximum"] if datapoints else None
        elapsed = int(time.time() - start)
        print(f"  [{elapsed:3d}s] ReplicationLag = {lag}")
        if lag is not None and lag == 0:
            print(f"\n[FL-03] ✓ ReplicationLag returned to 0 — failover complete at {elapsed}s")
            return elapsed
        time.sleep(POLL_INTERVAL)

    print(f"\n[FL-03] ✗ ReplicationLag did not reach 0 within {duration}s")
    return None


def check_cluster_status(ec_client):
    resp = ec_client.describe_replication_groups(
        ReplicationGroupId=REPLICATION_GROUP_ID
    )
    rg = resp["ReplicationGroups"][0]
    status = rg["Status"]
    print(f"[FL-03] Cluster status: {status}")
    for ng in rg.get("NodeGroups", []):
        for member in ng.get("NodeGroupMembers", []):
            role = member.get("CurrentRole", "unknown")
            endpoint = member.get("ReadEndpoint", {}).get("Address", "n/a")
            print(f"  Node {member['CacheClusterId']} role={role} endpoint={endpoint}")


def main():
    parser = argparse.ArgumentParser(description="FL-03 Redis failover injection")
    parser.add_argument("--inject", action="store_true", help="Trigger failover")
    parser.add_argument("--monitor", action="store_true", help="Monitor only (no failover)")
    args = parser.parse_args()

    if not args.inject and not args.monitor:
        parser.print_help()
        sys.exit(0)

    ec = boto3.client("elasticache", region_name=REGION)
    cw = boto3.client("cloudwatch", region_name=REGION)

    print("[FL-03] === Pre-failover cluster state ===")
    check_cluster_status(ec)

    if args.inject:
        inject_failover(ec)

    recovery_time = monitor_replication_lag(cw)

    print("\n[FL-03] === Post-failover cluster state ===")
    check_cluster_status(ec)

    if recovery_time:
        print(f"\n[FL-03] RESULT: Failover completed in ~{recovery_time}s")
        print("[FL-03] EVIDENCE TO CAPTURE:")
        print("  1. CloudWatch > ElastiCache > ReplicationLag — screenshot the spike + recovery")
        print("  2. CloudWatch Logs > /aws/lambda/clouddelivery-matching-dev — filter 'redis'")
        print("  3. X-Ray trace for any order placed during the 15–30s window")
        print(f"  4. Save to docs/failure_evidence/FL-03/")
    else:
        print("\n[FL-03] WARNING: Failover may still be in progress. Check AWS console.")


if __name__ == "__main__":
    main()
