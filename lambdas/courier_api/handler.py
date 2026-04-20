"""CourierAPILambda — API Gateway handler for /couriers endpoints.

Trigger  : API Gateway REST
Owner    : Mingkun Liu

Routes handled (template.yaml §CourierAPILambda):
  PUT  /couriers/{courierId}/location            — Push GPS update (§3.4)
  GET  /couriers/{courierId}/assignment          — Current assignment (§3.5)
  PUT  /couriers/{courierId}/assignment/accept   — Acknowledge assignment (§3.6)
  PUT  /couriers/{courierId}/assignment/reject   — Reject, trigger reassign (§3.7)
  PUT  /couriers/{courierId}/assignment/status   — Delivery progress (§3.8)

Design notes:
- SC-04 guard: caller's Cognito sub must equal the courierId in the path.
- Assignment lookup: MatchingLambda writes COURIER#{id}/ASSIGNMENT when it assigns
  an order; this record is the single source of truth for the courier's active order.
- Reject re-triggers matching: clears courierId on the order (REMOVE), sets status
  to "reassigning", then puts ORDER_PLACED back to Kinesis. MatchingLambda handles
  "reassigning" in its conditional write (attribute_not_exists(courierId) guard).
- Status transitions from courier: ready_for_pickup→picked_up, picked_up→delivered.
- Optimistic locking (version check) on all order mutations.
"""
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import ClientError
from ulid import ULID

from shared import logger, metrics
from shared.db import get_table as _get_table
from shared.errors import conflict, forbidden, invalid_request, not_found, problem, service_unavailable
from shared.models import EVENT_PREFIX, OrderStatus
from shared.tracing import get_trace_id, subsegment
from shared.validation import require_fields, validate_gps_coords

# ── Module-level init ─────────────────────────────────────────────────────────

table = _get_table()
kinesis_client = boto3.client("kinesis", region_name="us-east-1")

ENV = os.environ.get("ENV", "dev")
ORDER_STREAM_NAME = os.environ.get("ORDER_STREAM_NAME", "")
LOCATION_STREAM_NAME = os.environ.get("LOCATION_STREAM_NAME", "")

VALID_REJECT_REASONS = {"too_far", "vehicle_issue", "other"}
VALID_STATUS_UPDATES = {"picked_up", "delivered"}

# Allowed current status before a courier delivery-progress update.
_REQUIRED_STATUS_FOR = {
    "picked_up": OrderStatus.READY_FOR_PICKUP,
    "delivered": OrderStatus.PICKED_UP,
}


# ── Entry point ───────────────────────────────────────────────────────────────

def lambda_handler(event: dict, context) -> dict:
    trace_id = get_trace_id()
    method = event.get("httpMethod", "")
    path = event.get("path", "")
    params = event.get("pathParameters") or {}
    courier_id = params.get("courierId", "")

    logger.info("api_request", method=method, path=path, trace_id=trace_id)

    # SC-04: caller identity must match the courierId in the URL.
    claims = _get_claims(event)
    caller_id = claims.get("sub", "")
    if caller_id and courier_id and caller_id != courier_id:
        return forbidden(
            "You may only access your own courier resources.",
            instance=path, trace_id=trace_id,
        )

    try:
        if method == "PUT" and path.endswith("/location"):
            return _update_location(courier_id, event, trace_id)
        elif method == "GET" and path.endswith("/assignment"):
            return _get_assignment(courier_id, trace_id)
        elif method == "PUT" and path.endswith("/assignment/accept"):
            return _accept_assignment(courier_id, trace_id)
        elif method == "PUT" and path.endswith("/assignment/reject"):
            return _reject_assignment(courier_id, event, trace_id)
        elif method == "PUT" and path.endswith("/assignment/status"):
            return _update_delivery_status(courier_id, event, trace_id)
        else:
            return not_found("Endpoint not found.", trace_id=trace_id)

    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        logger.error("aws_client_error", error=code, trace_id=trace_id)
        return service_unavailable("Service temporarily unavailable.", trace_id=trace_id)
    except Exception as exc:
        logger.error("unexpected_error", error=str(exc), trace_id=trace_id)
        return service_unavailable("Unexpected server error.", trace_id=trace_id)


# ── Route handlers ────────────────────────────────────────────────────────────

def _update_location(courier_id: str, event: dict, trace_id: str) -> dict:
    """PUT /couriers/{id}/location — validate GPS, publish LOCATION_UPDATE to Kinesis."""
    raw_body = event.get("body") or "{}"
    try:
        body = json.loads(raw_body)
        require_fields(body, ["lat", "lng"])
        validate_gps_coords(body["lat"], body["lng"])
    except (ValueError, json.JSONDecodeError) as exc:
        return invalid_request(str(exc), trace_id=trace_id)

    lat = float(body["lat"])
    lng = float(body["lng"])
    now = _now_iso()

    envelope = {
        "eventId": f"{EVENT_PREFIX}{ULID()}",
        "eventType": "LOCATION_UPDATE",
        "schemaVersion": "1.0",
        "timestamp": now,
        "traceId": trace_id,
        "payload": {
            "courierId": courier_id,
            "lat": lat,
            "lng": lng,
            "timestamp": now,
        },
    }

    with subsegment("kinesis.put_location"):
        kinesis_client.put_record(
            StreamName=LOCATION_STREAM_NAME,
            Data=json.dumps(envelope).encode("utf-8"),
            PartitionKey=courier_id,
        )

    logger.info("location_update_published", courier_id=courier_id, trace_id=trace_id)
    metrics.emit("courier_api.location_update", 1)
    return {"statusCode": 204, "headers": {}, "body": ""}


def _get_assignment(courier_id: str, trace_id: str) -> dict:
    """GET /couriers/{id}/assignment — return active assignment or 204."""
    with subsegment("dynamodb.get_courier_assignment"):
        resp = table.get_item(Key={"PK": f"COURIER#{courier_id}", "SK": "ASSIGNMENT"})
    assignment = resp.get("Item")
    if not assignment:
        return {"statusCode": 204, "headers": {}, "body": ""}

    order_id = assignment["orderId"]

    with subsegment("dynamodb.get_order"):
        order_resp = table.get_item(Key={"PK": f"ORDER#{order_id}", "SK": "METADATA"})
    order = order_resp.get("Item")

    # If the order is gone or reassigned to someone else, report no active assignment.
    if not order or order.get("courierId") != courier_id:
        return {"statusCode": 204, "headers": {}, "body": ""}

    return _ok({
        "orderId": order["orderId"],
        "restaurantId": order["restaurantId"],
        "pickupLocation": {
            "lat": float(order["pickupLat"]),
            "lng": float(order["pickupLng"]),
            "address": order.get("pickupAddress", ""),
        },
        "dropoffLocation": {
            "lat": float(order["dropoffLat"]),
            "lng": float(order["dropoffLng"]),
            "address": order.get("dropoffAddress", ""),
        },
        "orderStatus": order["status"],
        "assignedAt": assignment["assignedAt"],
    })


def _accept_assignment(courier_id: str, trace_id: str) -> dict:
    """PUT /couriers/{id}/assignment/accept — acknowledge assignment (no status change)."""
    assignment, order = _load_active_assignment(courier_id, trace_id)
    if isinstance(assignment, dict) and "statusCode" in assignment:
        return assignment  # error response

    order_id = order["orderId"]
    now = _now_iso()

    # Record the acknowledgement timestamp without changing the order status.
    with subsegment("dynamodb.accept_assignment"):
        table.update_item(
            Key={"PK": f"ORDER#{order_id}", "SK": "METADATA"},
            UpdateExpression="SET updatedAt = :now",
            ExpressionAttributeValues={":now": now},
        )

    logger.info("assignment_accepted", courier_id=courier_id,
                order_id=order_id, trace_id=trace_id)
    metrics.emit("courier_api.assignment_accepted", 1)

    return _ok({"orderId": order_id, "status": order["status"], "updatedAt": now})


def _reject_assignment(courier_id: str, event: dict, trace_id: str) -> dict:
    """PUT /couriers/{id}/assignment/reject — reject order, trigger reassignment."""
    raw_body = event.get("body") or "{}"
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        body = {}

    reason = body.get("reason", "other")
    if reason not in VALID_REJECT_REASONS:
        return invalid_request(
            f"Invalid reason '{reason}'. Allowed: {sorted(VALID_REJECT_REASONS)}",
            trace_id=trace_id,
        )

    assignment, order = _load_active_assignment(courier_id, trace_id)
    if isinstance(assignment, dict) and "statusCode" in assignment:
        return assignment

    order_id = order["orderId"]
    current_status = order["status"]
    current_version = int(order.get("version", 0))

    # Only reject when still in an assignable state.
    if current_status not in {OrderStatus.ASSIGNED, OrderStatus.CONFIRMED}:
        return problem(
            409, "cannot-reject", "Cannot Reject Order",
            f"Order is in '{current_status}' state and cannot be rejected.",
            instance=f"/orders/{order_id}", trace_id=trace_id,
        )

    now = _now_iso()

    # 1. Clear courierId + set status to reassigning (optimistic lock).
    with subsegment("dynamodb.reject_order"):
        try:
            table.update_item(
                Key={"PK": f"ORDER#{order_id}", "SK": "METADATA"},
                UpdateExpression=(
                    "SET #s = :reassigning, version = :new_v, updatedAt = :now "
                    "REMOVE courierId"
                ),
                ConditionExpression="version = :cur_v",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":reassigning": OrderStatus.REASSIGNING,
                    ":new_v": current_version + 1,
                    ":cur_v": current_version,
                    ":now": now,
                },
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return conflict("Order was modified concurrently. Please retry.",
                                instance=f"/orders/{order_id}", trace_id=trace_id)
            raise

    # 2. Delete courier assignment record.
    try:
        table.delete_item(Key={"PK": f"COURIER#{courier_id}", "SK": "ASSIGNMENT"})
    except ClientError as exc:
        logger.warn("courier_record_delete_failed", courier_id=courier_id,
                    error=str(exc), trace_id=trace_id)

    # 3. Re-trigger matching: put ORDER_PLACED back to Kinesis.
    #    MatchingLambda handles reassigning state via attribute_not_exists(courierId).
    envelope = {
        "eventId": f"{EVENT_PREFIX}{ULID()}",
        "eventType": "ORDER_PLACED",
        "schemaVersion": "1.0",
        "timestamp": now,
        "traceId": trace_id,
        "payload": {
            "orderId": order_id,
            "customerId": order["customerId"],
            "restaurantId": order["restaurantId"],
            "pickupLat": float(order["pickupLat"]),
            "pickupLng": float(order["pickupLng"]),
            "dropoffLat": float(order["dropoffLat"]),
            "dropoffLng": float(order["dropoffLng"]),
            "totalAmountCents": int(order["totalAmountCents"]),
            "createdAt": order["createdAt"],
        },
    }
    with subsegment("kinesis.put_reassign"):
        kinesis_client.put_record(
            StreamName=ORDER_STREAM_NAME,
            Data=json.dumps(envelope).encode("utf-8"),
            PartitionKey=order_id,
        )

    logger.info("assignment_rejected", courier_id=courier_id, order_id=order_id,
                reason=reason, trace_id=trace_id)
    metrics.emit("courier_api.assignment_rejected", 1)

    return _ok({"orderId": order_id, "status": OrderStatus.REASSIGNING, "updatedAt": now})


def _update_delivery_status(courier_id: str, event: dict, trace_id: str) -> dict:
    """PUT /couriers/{id}/assignment/status — picked_up or delivered."""
    raw_body = event.get("body") or "{}"
    try:
        body = json.loads(raw_body)
        new_status = body.get("status", "")
    except json.JSONDecodeError:
        return invalid_request("Invalid JSON body.", trace_id=trace_id)

    if new_status not in VALID_STATUS_UPDATES:
        return invalid_request(
            f"Invalid status '{new_status}'. Allowed from courier: {sorted(VALID_STATUS_UPDATES)}",
            trace_id=trace_id,
        )

    assignment, order = _load_active_assignment(courier_id, trace_id)
    if isinstance(assignment, dict) and "statusCode" in assignment:
        return assignment

    order_id = order["orderId"]
    current_status = order["status"]
    current_version = int(order.get("version", 0))
    required_current = _REQUIRED_STATUS_FOR[new_status]

    if current_status != required_current:
        return problem(
            409, "conflict", "Invalid Status Transition",
            f"Cannot transition to '{new_status}' from '{current_status}'. "
            f"Expected current status: '{required_current}'.",
            instance=f"/orders/{order_id}", trace_id=trace_id,
        )

    now = _now_iso()

    with subsegment("dynamodb.update_delivery_status"):
        try:
            table.update_item(
                Key={"PK": f"ORDER#{order_id}", "SK": "METADATA"},
                UpdateExpression="SET #s = :new_s, version = :new_v, updatedAt = :now",
                ConditionExpression="version = :cur_v",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":new_s": new_status,
                    ":new_v": current_version + 1,
                    ":cur_v": current_version,
                    ":now": now,
                },
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return conflict("Order was modified concurrently. Please retry.",
                                instance=f"/orders/{order_id}", trace_id=trace_id)
            raise

    # Clear the courier assignment record on final delivery.
    if new_status == OrderStatus.DELIVERED:
        try:
            table.delete_item(Key={"PK": f"COURIER#{courier_id}", "SK": "ASSIGNMENT"})
        except ClientError as exc:
            logger.warn("courier_record_delete_failed", courier_id=courier_id,
                        error=str(exc), trace_id=trace_id)

    logger.info("delivery_status_updated", courier_id=courier_id, order_id=order_id,
                new_status=new_status, trace_id=trace_id)
    metrics.emit("courier_api.delivery_status_update", 1,
                 dimensions={"Status": new_status})

    return _ok({"orderId": order_id, "status": new_status, "updatedAt": now})


# ── Shared helpers ────────────────────────────────────────────────────────────

def _load_active_assignment(courier_id: str, trace_id: str):
    """Read and validate the courier's current ASSIGNMENT record + the linked order.

    Returns (assignment_item, order_item) on success.
    Returns (error_response_dict, None) if not found or mismatched.
    """
    resp = table.get_item(Key={"PK": f"COURIER#{courier_id}", "SK": "ASSIGNMENT"})
    assignment = resp.get("Item")
    if not assignment:
        return not_found("No active assignment found.", trace_id=trace_id), None

    order_id = assignment["orderId"]
    order_resp = table.get_item(Key={"PK": f"ORDER#{order_id}", "SK": "METADATA"})
    order = order_resp.get("Item")

    if not order or order.get("courierId") != courier_id:
        return not_found("Assignment no longer active.", trace_id=trace_id), None

    return assignment, order


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_claims(event: dict) -> dict:
    return event.get("requestContext", {}).get("authorizer", {}).get("claims", {})


def _ok(body: Any, status_code: int = 200) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": _json_dumps(body),
    }


def _json_dumps(obj: Any) -> str:
    def default(o: Any) -> Any:
        if isinstance(o, Decimal):
            f = float(o)
            return int(f) if f == int(f) else f
        raise TypeError(f"Object of type {type(o)} is not JSON serialisable")
    return json.dumps(obj, default=default)
