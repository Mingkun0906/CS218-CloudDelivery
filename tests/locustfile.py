"""CloudDelivery Locust load test.

Injects ORDER_PLACED events directly into the Kinesis orders stream,
then polls DynamoDB to measure end-to-end matching latency (time from
order injection to status = "assigned").

Bypasses API Gateway and Cognito — intended for performance and scaling
tests only.

Usage:
    # Prerequisite: seed the geo-index first
    #   python3 simulator/gps_simulator.py --couriers 50 &

    # Headless ramp: 1 → 100 users over 10s, run 30 minutes
    locust -f tests/locustfile.py \\
        --headless -u 100 -r 10 --run-time 30m --html report.html

    # Interactive UI (opens http://localhost:8089)
    locust -f tests/locustfile.py

    # Specific test scenarios (set -u to match target req/s):
    #   S-01 baseline  : -u 1   -r 1   --run-time 5m
    #   S-02 ramp      : -u 50  -r 5   --run-time 10m
    #   S-03 burst     : -u 100 -r 100 --run-time 5m

Environment variables:
    STREAM_NAME   Kinesis orders stream name      (default: clouddelivery-orders-dev)
    ORDERS_TABLE  DynamoDB table name             (default: clouddelivery-dev)
    AWS_REGION    AWS region                      (default: us-east-1)
    PICKUP_LAT    Pickup area center latitude     (default: 33.6846)
    PICKUP_LNG    Pickup area center longitude    (default: -117.8265)
    RADIUS_KM     Max offset from pickup center   (default: 3.0)
"""

import json
import math
import os
import random
import time
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.exceptions import ClientError
from locust import User, between, events, task
from locust.exception import StopUser

try:
    from ulid import ULID
    def _new_id(prefix: str) -> str:
        return f"{prefix}{ULID()}"
except ImportError:
    import uuid
    def _new_id(prefix: str) -> str:
        return f"{prefix}{uuid.uuid4().hex.upper()}"


# ── Config ────────────────────────────────────────────────────────────────────

STREAM_NAME: str = os.environ.get("STREAM_NAME", "clouddelivery-orders-dev")
ORDERS_TABLE: str = os.environ.get("ORDERS_TABLE", "clouddelivery-dev")
AWS_REGION: str = os.environ.get("AWS_REGION", "us-east-1")

PICKUP_LAT: float = float(os.environ.get("PICKUP_LAT", "33.6846"))
PICKUP_LNG: float = float(os.environ.get("PICKUP_LNG", "-117.8265"))
RADIUS_KM: float = float(os.environ.get("RADIUS_KM", "3.0"))

# DynamoDB poll settings for end-to-end latency measurement
POLL_INTERVAL_S: float = 0.5
POLL_TIMEOUT_S: float = 15.0

# Terminal statuses that indicate matching completed (success or failure)
TERMINAL_STATUSES = {"assigned", "unassigned", "failed"}


# ── Shared AWS clients (module-level, reused across users) ────────────────────

_kinesis = boto3.client("kinesis", region_name=AWS_REGION)
_dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
_table = _dynamodb.Table(ORDERS_TABLE)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _random_offset(center: float, radius_km: float, axis: str) -> float:
    """Uniform random offset within radius_km along a lat or lng axis."""
    if axis == "lat":
        delta = radius_km / 111.0
    else:  # lng
        delta = radius_km / (111.0 * math.cos(math.radians(PICKUP_LAT)))
    return center + random.uniform(-delta, delta)


def _build_order_event(order_id: str, customer_id: str) -> dict:
    pickup_lat = _random_offset(PICKUP_LAT, RADIUS_KM, "lat")
    pickup_lng = _random_offset(PICKUP_LNG, RADIUS_KM, "lng")
    # Dropoff is a few km away from pickup
    dropoff_lat = _random_offset(pickup_lat, 2.0, "lat")
    dropoff_lng = _random_offset(pickup_lng, 2.0, "lng")
    now = _now_iso()

    return {
        "eventId": _new_id("evt_"),
        "eventType": "ORDER_PLACED",
        "schemaVersion": "1.0",
        "timestamp": now,
        "traceId": f"locust-{order_id[-8:]}",
        "payload": {
            "orderId": order_id,
            "customerId": customer_id,
            "restaurantId": "rst_locust_test",
            "pickupLat": round(pickup_lat, 6),
            "pickupLng": round(pickup_lng, 6),
            "dropoffLat": round(dropoff_lat, 6),
            "dropoffLng": round(dropoff_lng, 6),
            "totalAmountCents": random.randint(800, 4500),
            "createdAt": now,
        },
    }


def _seed_dynamo_record(order_id: str, event: dict) -> None:
    """Pre-create the order record in DynamoDB with status=placed.

    The real OrderAPI does this before putting to Kinesis. The MatchingLambda's
    conditional write requires the record to exist with status in
    (placed, matching, reassigning), so we must mirror that here.
    """
    payload = event["payload"]
    _table.put_item(Item={
        "PK": f"ORDER#{order_id}",
        "SK": "METADATA",
        "orderId": order_id,
        "customerId": payload["customerId"],
        "restaurantId": payload["restaurantId"],
        "pickupLat": str(payload["pickupLat"]),
        "pickupLng": str(payload["pickupLng"]),
        "dropoffLat": str(payload["dropoffLat"]),
        "dropoffLng": str(payload["dropoffLng"]),
        "totalAmountCents": payload["totalAmountCents"],
        "status": "placed",
        "createdAt": payload["createdAt"],
    })


def _put_order(order_id: str, event: dict) -> float:
    """Put one ORDER_PLACED record to Kinesis. Returns put latency in ms."""
    payload = json.dumps(event).encode("utf-8")
    t0 = time.monotonic()
    _kinesis.put_record(
        StreamName=STREAM_NAME,
        Data=payload,
        PartitionKey=order_id,
    )
    return (time.monotonic() - t0) * 1000


def _poll_for_assignment(order_id: str) -> Optional[str]:
    """Poll DynamoDB until the order reaches a terminal status or times out.

    Returns the final status string, or None on timeout.
    """
    deadline = time.monotonic() + POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            resp = _table.get_item(
                Key={"PK": f"ORDER#{order_id}", "SK": "METADATA"},
                ProjectionExpression="#s",
                ExpressionAttributeNames={"#s": "status"},
            )
        except ClientError:
            time.sleep(POLL_INTERVAL_S)
            continue

        item = resp.get("Item", {})
        status = item.get("status")
        if status in TERMINAL_STATUSES:
            return status
        time.sleep(POLL_INTERVAL_S)

    return None  # timeout


def _fire(environment, name: str, response_time_ms: float, success: bool, exc=None) -> None:
    """Emit a Locust request event for custom (non-HTTP) operations."""
    environment.events.request.fire(
        request_type="kinesis" if "order" in name else "dynamodb",
        name=name,
        response_time=response_time_ms,
        response_length=0,
        exception=exc,
        context={},
    )


# ── Locust user classes ───────────────────────────────────────────────────────

class OrderUser(User):
    """Simulates a customer placing orders.

    Each task injects one ORDER_PLACED event into Kinesis and then polls
    DynamoDB to measure end-to-end matching latency. The two timings are
    reported as separate Locust metrics:

        kinesis / place_order        — Kinesis PutRecord latency
        dynamodb / matching_latency  — time from PutRecord to assigned/unassigned/failed
    """

    # 1–3s wait between orders per virtual user.
    # With 100 users this yields ~33–100 orders/s depending on think time.
    wait_time = between(1, 3)

    def on_start(self) -> None:
        self.customer_id = _new_id("cus_")

    @task
    def place_order(self) -> None:
        order_id = _new_id("ord_")
        event = _build_order_event(order_id, self.customer_id)

        # 1. Pre-create the DynamoDB record (mirrors what OrderAPI does in prod)
        try:
            _seed_dynamo_record(order_id, event)
        except ClientError as exc:
            _fire(self.environment, "place_order", 0, success=False, exc=exc)
            return

        # 2. Measure Kinesis put latency
        try:
            put_ms = _put_order(order_id, event)
            _fire(self.environment, "place_order", put_ms, success=True)
        except ClientError as exc:
            _fire(self.environment, "place_order", 0, success=False, exc=exc)
            return

        # 2. Measure end-to-end matching latency
        t_start = time.monotonic()
        final_status = _poll_for_assignment(order_id)
        e2e_ms = (time.monotonic() - t_start) * 1000

        if final_status == "assigned":
            _fire(self.environment, "matching_latency", e2e_ms, success=True)
        elif final_status in ("unassigned", "failed"):
            # Order processed but no courier found — record as failure for visibility
            _fire(
                self.environment,
                "matching_latency",
                e2e_ms,
                success=False,
                exc=Exception(f"order {order_id} ended with status={final_status}"),
            )
        else:
            # Timeout: matching took longer than POLL_TIMEOUT_S
            _fire(
                self.environment,
                "matching_latency",
                POLL_TIMEOUT_S * 1000,
                success=False,
                exc=Exception(f"order {order_id} not matched within {POLL_TIMEOUT_S}s"),
            )


class BurstOrderUser(OrderUser):
    """Identical to OrderUser but with shorter think time for burst tests (S-03).

    Use alongside OrderUser to model a mixed traffic profile, or exclusively
    for burst scenario: locust -f tests/locustfile.py BurstOrderUser -u 100 -r 100
    """

    wait_time = between(0.1, 0.5)
