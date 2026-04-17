"""RetryLambda — Re-queues unassigned orders for a second matching attempt.

Trigger  : SQS RetryQueue (messages arrive with 30s delay set by MatchingLambda)
Owner    : Zhuoqun Wei (infra)

Flow:
  1. MatchingLambda exhausts all courier candidates → marks order "unassigned"
  2. MatchingLambda sends a message to RetryQueue with DelaySeconds=30
  3. After 30s this Lambda fires, reconstructs the ORDER_PLACED EventEnvelope,
     and puts it back into the Kinesis orders stream
  4. MatchingLambda picks it up and tries again (up to MAX_RETRY_COUNT=3 total)

The SQS message body is a JSON object with the minimum fields needed to
reconstruct an ORDER_PLACED event (orderId, customerId, restaurantId,
pickupLat, pickupLng). Dropoff coordinates default to the pickup location
if not present — MatchingLambda does not use dropoff, so this is fine.
"""
import json
import os
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from shared import logger
from shared.tracing import get_trace_id

try:
    from ulid import ULID
    def _new_id(prefix: str) -> str:
        return f"{prefix}{ULID()}"
except ImportError:
    import uuid
    def _new_id(prefix: str) -> str:
        return f"{prefix}{uuid.uuid4().hex.upper()}"

# ── Module-level init ─────────────────────────────────────────────────────────

ORDER_STREAM_NAME: str = os.environ.get("ORDER_STREAM_NAME", "")

_kinesis = boto3.client("kinesis", region_name="us-east-1")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_envelope(body: dict) -> dict:
    order_id = body["orderId"]
    pickup_lat = body["pickupLat"]
    pickup_lng = body["pickupLng"]
    return {
        "eventId": _new_id("evt_"),
        "eventType": "ORDER_PLACED",
        "schemaVersion": "1.0",
        "timestamp": _now_iso(),
        "traceId": f"retry-{order_id[-8:]}",
        "payload": {
            "orderId": order_id,
            "customerId": body["customerId"],
            "restaurantId": body["restaurantId"],
            "pickupLat": pickup_lat,
            "pickupLng": pickup_lng,
            # Dropoff is not used by MatchingLambda; default to pickup if missing.
            "dropoffLat": body.get("dropoffLat", pickup_lat),
            "dropoffLng": body.get("dropoffLng", pickup_lng),
            "totalAmountCents": body.get("totalAmountCents", 0),
            "createdAt": body.get("createdAt", _now_iso()),
        },
    }


# ── Entry point ───────────────────────────────────────────────────────────────

def lambda_handler(event: dict, context) -> dict:
    trace_id = get_trace_id()
    logger.info("invocation_start", handler="retry", trace_id=trace_id)

    failures = []

    for record in event.get("Records", []):
        msg_id = record.get("messageId", "unknown")
        try:
            body = json.loads(record["body"])
            order_id = body["orderId"]

            envelope = _build_envelope(body)

            _kinesis.put_record(
                StreamName=ORDER_STREAM_NAME,
                Data=json.dumps(envelope).encode("utf-8"),
                PartitionKey=order_id,
            )

            logger.info(
                "retry_injected",
                order_id=order_id,
                trace_id=trace_id,
            )

        except (KeyError, json.JSONDecodeError) as exc:
            logger.error(
                "retry_invalid_message",
                msg_id=msg_id,
                error=str(exc),
                trace_id=trace_id,
            )
            failures.append({"itemIdentifier": msg_id})

        except ClientError as exc:
            logger.error(
                "retry_kinesis_error",
                msg_id=msg_id,
                error=str(exc),
                trace_id=trace_id,
            )
            failures.append({"itemIdentifier": msg_id})

    logger.info(
        "invocation_complete",
        handler="retry",
        total=len(event.get("Records", [])),
        failed=len(failures),
        trace_id=trace_id,
    )
    return {"batchItemFailures": failures}
