"""OrderAPILambda — API Gateway handler for /orders endpoints.

Trigger  : API Gateway REST
Owner    : Mingkun Liu

Routes handled (template.yaml §OrderAPILambda):
  POST   /orders                 — Place a new order (§3.1)
  GET    /orders/{orderId}       — Order status and detail (§3.2)
  DELETE /orders/{orderId}       — Cancel an order (§3.3)
  GET    /health                 — Unauthenticated liveness check (§3.11)

Restaurant routes (§3.9, §3.10) are TODO — not yet wired in template.yaml.

Design notes:
- customerId is taken from the Cognito JWT claim `sub` injected by API Gateway.
- Order creation uses attribute_not_exists(PK) to be idempotent (E-07, F-09).
- Cancellation uses optimistic locking (version check) to prevent races.
- All errors follow RFC 7807 (shared.errors); traceId is always included.
- DynamoDB Decimal is converted to int/float for JSON serialisation.
"""
import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from ulid import ULID

from shared import logger, metrics
from shared.db import get_table as _get_table
from shared.errors import conflict, invalid_request, not_found, problem, service_unavailable
from shared.models import EVENT_PREFIX, ORDER_PREFIX, OrderStatus
from shared.tracing import get_trace_id, subsegment
from shared.validation import require_fields, validate_gps_coords, validate_payload_size

# ── Module-level init (reused across warm invocations) ────────────────────────

table = _get_table()
kinesis_client = boto3.client("kinesis", region_name="us-east-1")

ENV = os.environ.get("ENV", "dev")
ORDER_STREAM_NAME = os.environ.get("ORDER_STREAM_NAME", "")
MAX_PAYLOAD_BYTES = 10_240  # 10 KB
ORDER_TTL_SECONDS = 30 * 24 * 3600  # 30 days


# ── Entry point ───────────────────────────────────────────────────────────────

def lambda_handler(event: dict, context) -> dict:
    trace_id = get_trace_id()
    method = event.get("httpMethod", "")
    path = event.get("path", "")
    params = event.get("pathParameters") or {}

    logger.info("api_request", method=method, path=path, trace_id=trace_id)

    try:
        if path == "/health":
            return _health()
        elif method == "POST" and path == "/orders":
            return _create_order(event, trace_id)
        elif method == "GET" and "orderId" in params:
            return _get_order(params["orderId"], trace_id)
        elif method == "DELETE" and "orderId" in params:
            return _cancel_order(params["orderId"], event, trace_id)
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

def _health() -> dict:
    return _ok({"status": "healthy", "timestamp": _now_iso(), "version": "1.0.0"})


def _create_order(event: dict, trace_id: str) -> dict:
    # 1. Extract caller identity from Cognito JWT claims.
    claims = _get_claims(event)
    customer_id = claims.get("sub", "")
    if not customer_id:
        return invalid_request("Missing caller identity.", trace_id=trace_id)

    # 2. Parse and validate body.
    raw_body = event.get("body") or "{}"
    try:
        validate_payload_size(raw_body)
        body = json.loads(raw_body)
    except (ValueError, json.JSONDecodeError) as exc:
        return invalid_request(str(exc), trace_id=trace_id)

    try:
        require_fields(body, ["restaurantId", "pickupLocation", "dropoffLocation", "items"])
        pickup = body["pickupLocation"]
        dropoff = body["dropoffLocation"]
        require_fields(pickup, ["lat", "lng"])
        require_fields(dropoff, ["lat", "lng"])
        validate_gps_coords(pickup["lat"], pickup["lng"])
        validate_gps_coords(dropoff["lat"], dropoff["lng"])
        items: List[dict] = body["items"]
        if not isinstance(items, list) or len(items) == 0:
            raise ValueError("'items' must be a non-empty list.")
        for item in items:
            require_fields(item, ["itemId", "name", "quantity", "unitPriceCents"])
    except ValueError as exc:
        return invalid_request(str(exc), trace_id=trace_id)

    # 3. Compute derived fields.
    order_id = f"{ORDER_PREFIX}{ULID()}"
    now = _now_iso()
    ttl = int(time.time()) + ORDER_TTL_SECONDS
    total_cents = sum(int(i["quantity"]) * int(i["unitPriceCents"]) for i in items)
    pickup_lat = float(pickup["lat"])
    pickup_lng = float(pickup["lng"])
    dropoff_lat = float(dropoff["lat"])
    dropoff_lng = float(dropoff["lng"])
    restaurant_id: str = body["restaurantId"]
    special = body.get("specialInstructions")

    # 4. Write order metadata to DynamoDB (idempotency: attribute_not_exists(PK)).
    metadata_item: Dict[str, Any] = {
        "PK": f"ORDER#{order_id}",
        "SK": "METADATA",
        "orderId": order_id,
        "customerId": customer_id,
        "restaurantId": restaurant_id,
        "status": OrderStatus.PLACED,
        "pickupLat": Decimal(str(pickup_lat)),
        "pickupLng": Decimal(str(pickup_lng)),
        "pickupAddress": pickup.get("address", ""),
        "dropoffLat": Decimal(str(dropoff_lat)),
        "dropoffLng": Decimal(str(dropoff_lng)),
        "dropoffAddress": dropoff.get("address", ""),
        "totalAmountCents": total_cents,
        "retryCount": 0,
        "version": 0,
        "createdAt": now,
        "updatedAt": now,
        "ttl": ttl,
        # GSIs for customer and restaurant queries (INTERFACE_SPEC.md §5).
        "GSI1PK": f"CUSTOMER#{customer_id}",
        "GSI1SK": now,
        "GSI2PK": f"RESTAURANT#{restaurant_id}",
        "GSI2SK": now,
    }
    if special:
        metadata_item["specialInstructions"] = special

    with subsegment("dynamodb.put_order"):
        try:
            table.put_item(
                Item=metadata_item,
                ConditionExpression="attribute_not_exists(PK)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                # Duplicate submission — return the existing order ID as-is.
                logger.info("duplicate_order_submission", order_id=order_id, trace_id=trace_id)
                return _ok(
                    {"orderId": order_id, "status": OrderStatus.PLACED,
                     "estimatedMatchingMs": 5000, "createdAt": now},
                    status_code=202,
                )
            raise

    # 5. Write first status history entry and order items (best-effort; non-critical).
    try:
        with table.batch_writer() as batch:
            batch.put_item(Item={
                "PK": f"ORDER#{order_id}",
                "SK": f"STATUS#{now}",
                "status": OrderStatus.PLACED,
                "timestamp": now,
                "actorId": customer_id,
                "actorRole": "customer",
            })
            for item in items:
                batch.put_item(Item={
                    "PK": f"ORDER#{order_id}",
                    "SK": f"ITEM#{item['itemId']}",
                    "itemId": item["itemId"],
                    "name": item["name"],
                    "quantity": int(item["quantity"]),
                    "unitPriceCents": int(item["unitPriceCents"]),
                })
    except ClientError as exc:
        # Non-critical: log and continue. Order metadata is already written.
        logger.warn("order_items_write_failed", order_id=order_id,
                    error=str(exc), trace_id=trace_id)

    # 6. Publish ORDER_PLACED to Kinesis order stream.
    envelope = {
        "eventId": f"{EVENT_PREFIX}{ULID()}",
        "eventType": "ORDER_PLACED",
        "schemaVersion": "1.0",
        "timestamp": now,
        "traceId": trace_id,
        "payload": {
            "orderId": order_id,
            "customerId": customer_id,
            "restaurantId": restaurant_id,
            "pickupLat": pickup_lat,
            "pickupLng": pickup_lng,
            "dropoffLat": dropoff_lat,
            "dropoffLng": dropoff_lng,
            "totalAmountCents": total_cents,
            "createdAt": now,
        },
    }
    with subsegment("kinesis.put_order"):
        kinesis_client.put_record(
            StreamName=ORDER_STREAM_NAME,
            Data=json.dumps(envelope).encode("utf-8"),
            PartitionKey=order_id,
        )

    logger.info("order_created", order_id=order_id, customer_id=customer_id,
                restaurant_id=restaurant_id, trace_id=trace_id)
    metrics.emit("order_api.order_created", 1)

    return _ok(
        {"orderId": order_id, "status": OrderStatus.PLACED,
         "estimatedMatchingMs": 5000, "createdAt": now},
        status_code=202,
    )


def _get_order(order_id: str, trace_id: str) -> dict:
    pk = f"ORDER#{order_id}"

    # 1. Fetch order metadata.
    with subsegment("dynamodb.get_order"):
        resp = table.get_item(Key={"PK": pk, "SK": "METADATA"})
    item = resp.get("Item")
    if not item:
        return not_found(f"Order '{order_id}' not found.",
                         instance=f"/orders/{order_id}", trace_id=trace_id)

    # 2. Fetch status history (all SK beginning with STATUS#, oldest-first).
    with subsegment("dynamodb.query_status_history"):
        history_resp = table.query(
            KeyConditionExpression=Key("PK").eq(pk) & Key("SK").begins_with("STATUS#"),
            ScanIndexForward=True,
        )
    status_history = [
        {"status": h["status"], "timestamp": h["timestamp"]}
        for h in history_resp.get("Items", [])
    ]

    # 3. Fetch order items (all SK beginning with ITEM#).
    with subsegment("dynamodb.query_items"):
        items_resp = table.query(
            KeyConditionExpression=Key("PK").eq(pk) & Key("SK").begins_with("ITEM#"),
        )
    order_items = [
        {
            "itemId": i["itemId"],
            "name": i["name"],
            "quantity": int(i["quantity"]),
            "unitPriceCents": int(i["unitPriceCents"]),
        }
        for i in items_resp.get("Items", [])
    ]

    # 4. Build response (INTERFACE_SPEC.md §3.2).
    response_body: Dict[str, Any] = {
        "orderId": item["orderId"],
        "customerId": item["customerId"],
        "restaurantId": item["restaurantId"],
        "status": item["status"],
        "pickupLocation": {
            "lat": float(item["pickupLat"]),
            "lng": float(item["pickupLng"]),
            "address": item.get("pickupAddress", ""),
        },
        "dropoffLocation": {
            "lat": float(item["dropoffLat"]),
            "lng": float(item["dropoffLng"]),
            "address": item.get("dropoffAddress", ""),
        },
        "totalAmountCents": int(item["totalAmountCents"]),
        "items": order_items,
        "statusHistory": status_history,
        "version": int(item.get("version", 0)),
        "retryCount": int(item.get("retryCount", 0)),
        "createdAt": item["createdAt"],
        "updatedAt": item["updatedAt"],
    }
    if item.get("courierId"):
        response_body["courierId"] = item["courierId"]
    if item.get("specialInstructions"):
        response_body["specialInstructions"] = item["specialInstructions"]

    return _ok(response_body)


def _cancel_order(order_id: str, event: dict, trace_id: str) -> dict:
    claims = _get_claims(event)
    customer_id = claims.get("sub", "")
    pk = f"ORDER#{order_id}"
    instance = f"/orders/{order_id}"

    # 1. Read current order.
    with subsegment("dynamodb.get_order_for_cancel"):
        resp = table.get_item(Key={"PK": pk, "SK": "METADATA"})
    item = resp.get("Item")
    if not item:
        return not_found(f"Order '{order_id}' not found.",
                         instance=instance, trace_id=trace_id)

    current_status = item["status"]
    current_version = int(item.get("version", 0))

    # 2. Check cancellability.
    if current_status not in OrderStatus.CANCELLABLE:
        return problem(
            409, "cannot-cancel", "Order Cannot Be Cancelled",
            f"Orders in '{current_status}' state cannot be cancelled.",
            instance=instance, trace_id=trace_id,
        )

    now = _now_iso()

    # 3. Conditional update to 'cancelled' with optimistic lock (version check).
    with subsegment("dynamodb.cancel_order"):
        try:
            table.update_item(
                Key={"PK": pk, "SK": "METADATA"},
                UpdateExpression=(
                    "SET #s = :cancelled, version = :new_version, updatedAt = :now"
                ),
                ConditionExpression="version = :current_version",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":cancelled": OrderStatus.CANCELLED,
                    ":new_version": current_version + 1,
                    ":current_version": current_version,
                    ":now": now,
                },
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                # Concurrent modification — re-read and check.
                return conflict(
                    "Order was modified concurrently. Please retry.",
                    instance=instance, trace_id=trace_id,
                )
            raise

    # 4. Append cancelled status history.
    try:
        table.put_item(Item={
            "PK": pk,
            "SK": f"STATUS#{now}",
            "status": OrderStatus.CANCELLED,
            "timestamp": now,
            "actorId": customer_id,
            "actorRole": "customer",
        })
    except ClientError as exc:
        logger.warn("cancel_history_write_failed", order_id=order_id,
                    error=str(exc), trace_id=trace_id)

    logger.info("order_cancelled", order_id=order_id,
                customer_id=customer_id, trace_id=trace_id)
    metrics.emit("order_api.order_cancelled", 1)

    return _ok({"orderId": order_id, "status": OrderStatus.CANCELLED, "updatedAt": now})


# ── Helpers ───────────────────────────────────────────────────────────────────

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
    """JSON serialiser that handles DynamoDB Decimal values."""
    def default(o: Any) -> Any:
        if isinstance(o, Decimal):
            f = float(o)
            return int(f) if f == int(f) else f
        raise TypeError(f"Object of type {type(o)} is not JSON serialisable")
    return json.dumps(obj, default=default)
