"""MatchingLambda — Kinesis-triggered courier-to-order matching engine.

Trigger  : Kinesis stream clouddelivery-orders-{env}
Batch    : 1 record per invocation (configured in template.yaml)
Owner    : Mingkun Liu

Processing steps (INTERFACE_SPEC.md §9.3):
1. Decode Kinesis record → EventEnvelope
2. Validate ORDER_PLACED payload
3. GEOSEARCH Redis for nearest available couriers within SEARCH_RADIUS_KM
4. For each candidate (nearest-first):
   a. Verify courier TTL sentinel still alive (guards E-17 stale-courier race)
   b. DynamoDB conditional write: set courierId + status=assigned
      Condition: attribute_not_exists(courierId) AND status IN (matching, placed)
      This is the primary FL-01 double-assignment guard.
   c. On success: publish COURIER_ASSIGNED to SNS, emit metrics, return.
   d. On ConditionalCheckFailedException: try next candidate.
5. If no candidate succeeds: increment retryCount; mark order unassigned or failed.
6. Return {"batchItemFailures": [...]} for Kinesis partial-batch retry support.

TODO (Zhuoqun): Retry scheduling for unassigned orders.
  When an order reaches "unassigned" with retryCount < 3, it must be re-enqueued
  for matching after 30s. This requires an infra-level mechanism:
  Option A — DynamoDB Streams + EventBridge Pipes with delay
  Option B — EventBridge Scheduler rule created per order
  The application-level retry counter and status transitions are implemented here;
  only the 30s re-trigger is missing.
"""
import json
import os
import time
from datetime import datetime, timezone
from typing import List, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

_boto_cfg = Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 1, "mode": "standard"})
from ulid import ULID

from shared import logger, metrics
from shared.cache import get_client as _get_redis_client
from shared.db import get_table as _get_table
from shared.kinesis import decode_kinesis_record, get_sequence_number
from shared.models import (
    EVENT_PREFIX,
    EVENT_TYPE_ORDER_PLACED,
    NOTIFICATION_TYPE_COURIER_ASSIGNED,
    NOTIFICATION_TYPE_NO_COURIER_FOUND,
    OrderStatus,
)
from shared.tracing import get_trace_id, subsegment
from shared.validation import validate_order_placed_payload

# ── Module-level init (reused across warm invocations) ────────────────────────

redis_client = _get_redis_client()
table = _get_table()
sns_client = boto3.client("sns", region_name="us-east-1", config=_boto_cfg)
sqs_client = boto3.client("sqs", region_name="us-east-1", config=_boto_cfg)

ENV = os.environ.get("ENV", "dev")
SEARCH_RADIUS_KM = float(os.environ.get("SEARCH_RADIUS_KM", "5"))
MAX_COURIER_CANDIDATES = int(os.environ.get("MAX_COURIER_CANDIDATES", "10"))
MATCH_TOPIC_ARN = os.environ.get("MATCH_TOPIC_ARN", "")
RETRY_QUEUE_URL = os.environ.get("RETRY_QUEUE_URL", "")
MAX_RETRY_COUNT = 3

GEO_KEY = "couriers:locations"
COURIER_TTL_KEY_PREFIX = "courier:ttl:"


# ── Entry point ───────────────────────────────────────────────────────────────

def lambda_handler(event: dict, context) -> dict:
    """Lambda entry point. Returns partial batch failure list."""
    trace_id = get_trace_id()
    logger.info("invocation_start", handler="matching", trace_id=trace_id)

    failed_items: List[dict] = []

    for record in event.get("Records", []):
        seq = get_sequence_number(record)
        try:
            _process_record(record, trace_id)
        except Exception as exc:
            logger.error(
                "record_processing_failed",
                error=str(exc),
                error_type=type(exc).__name__,
                sequence_number=seq,
                trace_id=trace_id,
            )
            failed_items.append({"itemIdentifier": seq})

    logger.info(
        "invocation_complete",
        handler="matching",
        total=len(event.get("Records", [])),
        failed=len(failed_items),
        trace_id=trace_id,
    )
    return {"batchItemFailures": failed_items}


# ── Record processing ──────────────────────────────────────────────────────────

def _process_record(record: dict, trace_id: str) -> None:
    envelope = decode_kinesis_record(record)

    event_type = envelope.get("eventType")
    if event_type != EVENT_TYPE_ORDER_PLACED:
        logger.info(
            "skipping_unsupported_event_type",
            event_type=event_type,
            trace_id=trace_id,
        )
        return

    payload = envelope.get("payload", {})
    validate_order_placed_payload(payload)

    order_id = payload["orderId"]
    customer_id = payload["customerId"]
    restaurant_id = payload["restaurantId"]
    pickup_lat = float(payload["pickupLat"])
    pickup_lng = float(payload["pickupLng"])

    start_ms = time.monotonic() * 1000

    try:
        _match_order(
            order_id=order_id,
            customer_id=customer_id,
            restaurant_id=restaurant_id,
            pickup_lat=pickup_lat,
            pickup_lng=pickup_lng,
            trace_id=trace_id,
        )
    finally:
        duration_ms = time.monotonic() * 1000 - start_ms
        metrics.emit(
            "matching.duration_ms",
            duration_ms,
            unit="Milliseconds",
            dimensions={"Result": "completed"},
        )


def _match_order(
    order_id: str,
    customer_id: str,
    restaurant_id: str,
    pickup_lat: float,
    pickup_lng: float,
    trace_id: str,
) -> None:
    # 1. Query Redis geo-index for nearby couriers.
    #    Use GEOSEARCH (Redis 6.2+); GEORADIUS is deprecated in redis-py 4.1+.
    #    This is semantically equivalent to the GEORADIUS command in INTERFACE_SPEC.md §7.
    with subsegment("redis.georadius") as seg:
        seg.put_metadata("radius_km", SEARCH_RADIUS_KM)
        seg.put_metadata("order_id", order_id)

        candidates: List[str] = redis_client.geosearch(
            GEO_KEY,
            longitude=pickup_lng,
            latitude=pickup_lat,
            radius=SEARCH_RADIUS_KM,
            unit="km",
            sort="ASC",
            count=MAX_COURIER_CANDIDATES,
        )

    metrics.emit("matching.couriers_in_radius", len(candidates))
    logger.info(
        "geo_search_complete",
        order_id=order_id,
        courier_count=len(candidates),
        trace_id=trace_id,
    )

    if not candidates:
        logger.info("no_couriers_in_radius", order_id=order_id, trace_id=trace_id)
        _mark_unassigned_or_failed(
            order_id, customer_id, restaurant_id, pickup_lat, pickup_lng, trace_id
        )
        metrics.emit(
            "matching.assignment_result", 1, dimensions={"Result": "no_courier"}
        )
        return

    # 2. Try each courier nearest-first.
    for courier_id in candidates:
        # Guard against E-17: courier TTL expired between GEOSEARCH and now.
        if not redis_client.exists(f"{COURIER_TTL_KEY_PREFIX}{courier_id}"):
            logger.info(
                "courier_ttl_expired_skipping",
                order_id=order_id,
                courier_id=courier_id,
                trace_id=trace_id,
            )
            continue

        assigned = _try_assign_courier(order_id, courier_id, trace_id)
        if assigned:
            _publish_assignment(
                order_id=order_id,
                courier_id=courier_id,
                customer_id=customer_id,
                restaurant_id=restaurant_id,
                trace_id=trace_id,
            )
            metrics.emit(
                "matching.assignment_result", 1, dimensions={"Result": "success"}
            )
            return

    # All candidates exhausted (conflicts or TTL expiry).
    logger.info(
        "all_candidates_exhausted",
        order_id=order_id,
        candidate_count=len(candidates),
        trace_id=trace_id,
    )
    _mark_unassigned_or_failed(
        order_id, customer_id, restaurant_id, pickup_lat, pickup_lng, trace_id
    )
    metrics.emit(
        "matching.assignment_result",
        1,
        dimensions={"Result": "conflict"},
    )


def _try_assign_courier(order_id: str, courier_id: str, trace_id: str) -> bool:
    """Atomically assign *courier_id* to *order_id*.

    Uses attribute_not_exists(courierId) as the primary FL-01 guard.
    Returns True on success, False on ConditionalCheckFailedException.
    Raises on any other ClientError (network, throttle, etc.).
    """
    now = _now_iso()

    with subsegment("dynamo.conditional_write") as seg:
        seg.put_metadata("order_id", order_id)
        seg.put_metadata("courier_id", courier_id)

        try:
            table.update_item(
                Key={"PK": f"ORDER#{order_id}", "SK": "METADATA"},
                UpdateExpression=(
                    "SET courierId = :cid, #s = :assigned, "
                    "version = if_not_exists(version, :zero) + :one, "
                    "updatedAt = :now"
                ),
                ConditionExpression=(
                    "attribute_not_exists(courierId) AND "
                    "(#s = :matching OR #s = :placed OR #s = :reassigning)"
                ),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":cid": courier_id,
                    ":assigned": OrderStatus.ASSIGNED,
                    ":matching": OrderStatus.MATCHING,
                    ":placed": OrderStatus.PLACED,
                    ":reassigning": OrderStatus.REASSIGNING,
                    ":zero": 0,
                    ":one": 1,
                    ":now": now,
                },
            )
            logger.info(
                "courier_assigned",
                order_id=order_id,
                courier_id=courier_id,
                trace_id=trace_id,
            )
            return True

        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code == "ConditionalCheckFailedException":
                logger.info(
                    "conditional_write_conflict",
                    order_id=order_id,
                    courier_id=courier_id,
                    trace_id=trace_id,
                )
                return False
            raise


def _publish_assignment(
    order_id: str,
    courier_id: str,
    customer_id: str,
    restaurant_id: str,
    trace_id: str,
) -> None:
    """Publish COURIER_ASSIGNED notification to the SNS match topic."""
    message = {
        "notificationType": NOTIFICATION_TYPE_COURIER_ASSIGNED,
        "eventId": f"{EVENT_PREFIX}{ULID()}",
        "timestamp": _now_iso(),
        "traceId": trace_id,
        "recipients": {
            "courierId": courier_id,
            "customerId": customer_id,
            "restaurantId": restaurant_id,
        },
        "data": {
            "orderId": order_id,
            "status": OrderStatus.ASSIGNED,
        },
    }

    with subsegment("sns.publish") as seg:
        seg.put_metadata("order_id", order_id)
        seg.put_metadata("courier_id", courier_id)

        sns_client.publish(
            TopicArn=MATCH_TOPIC_ARN,
            Message=json.dumps(message),
            MessageAttributes={
                "notificationType": {
                    "DataType": "String",
                    "StringValue": NOTIFICATION_TYPE_COURIER_ASSIGNED,
                },
                "courierId": {
                    "DataType": "String",
                    "StringValue": courier_id,
                },
            },
        )

    logger.info(
        "assignment_published_to_sns",
        order_id=order_id,
        courier_id=courier_id,
        trace_id=trace_id,
    )

    # Write COURIER assignment record so CourierAPILambda can look up the
    # current assignment by courierId (GET /couriers/{id}/assignment).
    try:
        table.put_item(Item={
            "PK": f"COURIER#{courier_id}",
            "SK": "ASSIGNMENT",
            "orderId": order_id,
            "assignedAt": _now_iso(),
        })
    except ClientError as exc:
        # Non-critical: the order is already locked; log and continue.
        logger.warn(
            "courier_assignment_record_write_failed",
            courier_id=courier_id,
            error=str(exc),
            trace_id=trace_id,
        )


def _mark_unassigned_or_failed(
    order_id: str,
    customer_id: str,
    restaurant_id: str,
    pickup_lat: float,
    pickup_lng: float,
    trace_id: str,
) -> None:
    """Increment retryCount and set order to unassigned or failed.

    If retryCount reaches MAX_RETRY_COUNT the order is permanently failed.
    When the order is unassigned (retryCount < MAX_RETRY_COUNT), sends a
    message to RetryQueue with a 30s delay so RetryLambda re-injects the
    ORDER_PLACED event into Kinesis for another matching attempt.
    Skips silently if the order was already assigned by a concurrent invocation.
    """
    now = _now_iso()

    try:
        with subsegment("dynamo.increment_retry") as seg:
            seg.put_metadata("order_id", order_id)
            # Atomic increment; guard ensures we don't overwrite an assigned order.
            response = table.update_item(
                Key={"PK": f"ORDER#{order_id}", "SK": "METADATA"},
                UpdateExpression=(
                    "SET retryCount = if_not_exists(retryCount, :zero) + :one, "
                    "updatedAt = :now, "
                    "version = if_not_exists(version, :zero) + :one"
                ),
                ConditionExpression="attribute_not_exists(courierId)",
                ExpressionAttributeValues={":zero": 0, ":one": 1, ":now": now},
                ReturnValues="UPDATED_NEW",
            )

        new_retry_count = int(response["Attributes"].get("retryCount", 1))
        metrics.emit("matching.retry_count", 1)

        new_status = (
            OrderStatus.FAILED
            if new_retry_count >= MAX_RETRY_COUNT
            else OrderStatus.UNASSIGNED
        )

        with subsegment("dynamo.update_status") as seg:
            seg.put_metadata("order_id", order_id)
            seg.put_metadata("new_status", new_status)
            table.update_item(
                Key={"PK": f"ORDER#{order_id}", "SK": "METADATA"},
                UpdateExpression=(
                    "SET #s = :status, updatedAt = :now, "
                    "version = if_not_exists(version, :zero) + :one"
                ),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":status": new_status,
                    ":zero": 0,
                    ":one": 1,
                    ":now": now,
                },
            )

        logger.info(
            "order_marked",
            order_id=order_id,
            new_status=new_status,
            retry_count=new_retry_count,
            trace_id=trace_id,
        )

        if new_status == OrderStatus.UNASSIGNED and RETRY_QUEUE_URL:
            # Schedule a re-matching attempt in 30s via RetryLambda.
            sqs_client.send_message(
                QueueUrl=RETRY_QUEUE_URL,
                MessageBody=json.dumps({
                    "orderId": order_id,
                    "customerId": customer_id,
                    "restaurantId": restaurant_id,
                    "pickupLat": pickup_lat,
                    "pickupLng": pickup_lng,
                }),
                DelaySeconds=30,
            )
            logger.info(
                "retry_scheduled",
                order_id=order_id,
                delay_seconds=30,
                trace_id=trace_id,
            )

        if new_status == OrderStatus.FAILED:
            metrics.emit(
                "matching.assignment_result",
                1,
                dimensions={"Result": "failed_max_retries"},
            )

    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # Order was assigned by a concurrent invocation between our GEOSEARCH
            # and now; this is normal and not an error.
            logger.info(
                "order_already_assigned_skipping_unassigned_mark",
                order_id=order_id,
                trace_id=trace_id,
            )
        else:
            raise


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
