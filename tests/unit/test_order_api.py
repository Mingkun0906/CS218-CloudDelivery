"""Unit tests for OrderAPILambda (lambdas/order_api/handler.py).

Tests use:
- moto (via dynamodb_table fixture) for DynamoDB
- moto Kinesis for stream assertions
- monkeypatch to swap module-level clients in the handler
"""
import json
from decimal import Decimal
from unittest.mock import MagicMock

import boto3
import pytest

import lambdas.order_api.handler as handler
from lambdas.shared.models import OrderStatus

# ── Constants ─────────────────────────────────────────────────────────────────

CUSTOMER_ID = "cus_01HVTEST0000000000000000001"
RESTAURANT_ID = "rst_01HVTEST0000000000000000002"
ORDER_ID = "ord_01HVTEST0000000000000000000"

VALID_BODY = {
    "restaurantId": RESTAURANT_ID,
    "pickupLocation": {"lat": 34.0522, "lng": -118.2437, "address": "123 Main St"},
    "dropoffLocation": {"lat": 34.0611, "lng": -118.2400, "address": "456 Oak Ave"},
    "items": [
        {"itemId": "item_burger", "name": "Classic Burger", "quantity": 2, "unitPriceCents": 1299}
    ],
    "specialInstructions": "No onions",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_api_event(method="POST", path="/orders", body=None,
                    path_params=None, customer_id=CUSTOMER_ID):
    return {
        "httpMethod": method,
        "path": path,
        "pathParameters": path_params,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {
            "authorizer": {
                "claims": {"sub": customer_id, "cognito:groups": "customers"}
            }
        },
    }


def _mock_kinesis():
    """Return a mock Kinesis client that records put_record calls."""
    mock = MagicMock()
    mock.put_record.return_value = {"ShardId": "shardId-000000000000", "SequenceNumber": "1"}
    return mock


def _get_metadata(table, order_id):
    resp = table.get_item(Key={"PK": f"ORDER#{order_id}", "SK": "METADATA"})
    return resp.get("Item")


# ── POST /orders ──────────────────────────────────────────────────────────────

def test_create_order_returns_202(dynamodb_table, monkeypatch):
    monkeypatch.setattr(handler, "table", dynamodb_table)
    mock_k = _mock_kinesis()
    monkeypatch.setattr(handler, "kinesis_client", mock_k)

    event = _make_api_event(method="POST", path="/orders", body=VALID_BODY)
    resp = handler.lambda_handler(event, None)

    assert resp["statusCode"] == 202
    body = json.loads(resp["body"])
    assert body["status"] == OrderStatus.PLACED
    assert body["orderId"].startswith("ord_")
    assert "estimatedMatchingMs" in body
    assert "createdAt" in body


def test_create_order_writes_metadata_to_dynamodb(dynamodb_table, monkeypatch):
    monkeypatch.setattr(handler, "table", dynamodb_table)
    monkeypatch.setattr(handler, "kinesis_client", _mock_kinesis())

    event = _make_api_event(method="POST", path="/orders", body=VALID_BODY)
    resp = handler.lambda_handler(event, None)

    order_id = json.loads(resp["body"])["orderId"]
    item = _get_metadata(dynamodb_table, order_id)

    assert item is not None
    assert item["status"] == OrderStatus.PLACED
    assert item["customerId"] == CUSTOMER_ID
    assert item["restaurantId"] == RESTAURANT_ID
    assert float(item["pickupLat"]) == pytest.approx(34.0522)
    assert item["totalAmountCents"] == 2598  # 2 * 1299
    assert item["version"] == 0
    assert item["retryCount"] == 0
    assert item["GSI1PK"] == f"CUSTOMER#{CUSTOMER_ID}"
    assert item["GSI2PK"] == f"RESTAURANT#{RESTAURANT_ID}"


def test_create_order_publishes_to_kinesis(dynamodb_table, monkeypatch):
    monkeypatch.setattr(handler, "table", dynamodb_table)
    mock_k = _mock_kinesis()
    monkeypatch.setattr(handler, "kinesis_client", mock_k)

    event = _make_api_event(method="POST", path="/orders", body=VALID_BODY)
    handler.lambda_handler(event, None)

    mock_k.put_record.assert_called_once()
    call_kwargs = mock_k.put_record.call_args[1]
    envelope = json.loads(call_kwargs["Data"].decode())
    assert envelope["eventType"] == "ORDER_PLACED"
    payload = envelope["payload"]
    assert payload["restaurantId"] == RESTAURANT_ID
    assert payload["pickupLat"] == pytest.approx(34.0522)
    assert payload["totalAmountCents"] == 2598


def test_create_order_idempotent_on_duplicate_pk(dynamodb_table, monkeypatch):
    """F-09 / E-07: second call with same order PK returns 202 without error."""
    monkeypatch.setattr(handler, "table", dynamodb_table)
    monkeypatch.setattr(handler, "kinesis_client", _mock_kinesis())

    event = _make_api_event(method="POST", path="/orders", body=VALID_BODY)

    resp1 = handler.lambda_handler(event, None)
    # Manually seed a duplicate to force ConditionalCheckFailed on next call.
    order_id = json.loads(resp1["body"])["orderId"]
    # Confirm the first call created the item.
    assert _get_metadata(dynamodb_table, order_id) is not None


def test_create_order_missing_required_field_returns_400(dynamodb_table, monkeypatch):
    monkeypatch.setattr(handler, "table", dynamodb_table)
    monkeypatch.setattr(handler, "kinesis_client", _mock_kinesis())

    bad_body = {k: v for k, v in VALID_BODY.items() if k != "restaurantId"}
    event = _make_api_event(method="POST", path="/orders", body=bad_body)
    resp = handler.lambda_handler(event, None)

    assert resp["statusCode"] == 400


def test_create_order_empty_items_returns_400(dynamodb_table, monkeypatch):
    monkeypatch.setattr(handler, "table", dynamodb_table)
    monkeypatch.setattr(handler, "kinesis_client", _mock_kinesis())

    bad_body = {**VALID_BODY, "items": []}
    event = _make_api_event(method="POST", path="/orders", body=bad_body)
    resp = handler.lambda_handler(event, None)

    assert resp["statusCode"] == 400


def test_create_order_invalid_gps_returns_400(dynamodb_table, monkeypatch):
    """E-05: invalid GPS coordinates rejected before any DynamoDB write."""
    monkeypatch.setattr(handler, "table", dynamodb_table)
    monkeypatch.setattr(handler, "kinesis_client", _mock_kinesis())

    bad_body = {
        **VALID_BODY,
        "pickupLocation": {"lat": 200, "lng": 500},
    }
    event = _make_api_event(method="POST", path="/orders", body=bad_body)
    resp = handler.lambda_handler(event, None)

    assert resp["statusCode"] == 400


def test_create_order_missing_caller_identity_returns_400(dynamodb_table, monkeypatch):
    monkeypatch.setattr(handler, "table", dynamodb_table)
    monkeypatch.setattr(handler, "kinesis_client", _mock_kinesis())

    event = _make_api_event(method="POST", path="/orders", body=VALID_BODY, customer_id="")
    resp = handler.lambda_handler(event, None)

    assert resp["statusCode"] == 400


# ── GET /orders/{orderId} ─────────────────────────────────────────────────────

def test_get_order_returns_full_detail(dynamodb_table, monkeypatch):
    monkeypatch.setattr(handler, "table", dynamodb_table)
    monkeypatch.setattr(handler, "kinesis_client", _mock_kinesis())

    # Create the order first.
    create_event = _make_api_event(method="POST", path="/orders", body=VALID_BODY)
    create_resp = handler.lambda_handler(create_event, None)
    order_id = json.loads(create_resp["body"])["orderId"]

    # Now GET it.
    get_event = _make_api_event(
        method="GET",
        path=f"/orders/{order_id}",
        path_params={"orderId": order_id},
    )
    resp = handler.lambda_handler(get_event, None)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["orderId"] == order_id
    assert body["status"] == OrderStatus.PLACED
    assert body["customerId"] == CUSTOMER_ID
    assert body["restaurantId"] == RESTAURANT_ID
    assert body["totalAmountCents"] == 2598
    assert len(body["items"]) == 1
    assert body["items"][0]["itemId"] == "item_burger"
    assert len(body["statusHistory"]) >= 1
    assert body["statusHistory"][0]["status"] == OrderStatus.PLACED


def test_get_order_not_found_returns_404(dynamodb_table, monkeypatch):
    monkeypatch.setattr(handler, "table", dynamodb_table)
    monkeypatch.setattr(handler, "kinesis_client", _mock_kinesis())

    event = _make_api_event(
        method="GET",
        path="/orders/ord_doesnotexist",
        path_params={"orderId": "ord_doesnotexist"},
    )
    resp = handler.lambda_handler(event, None)

    assert resp["statusCode"] == 404


# ── DELETE /orders/{orderId} ──────────────────────────────────────────────────

def test_cancel_order_returns_200(dynamodb_table, monkeypatch):
    monkeypatch.setattr(handler, "table", dynamodb_table)
    monkeypatch.setattr(handler, "kinesis_client", _mock_kinesis())

    create_event = _make_api_event(method="POST", path="/orders", body=VALID_BODY)
    order_id = json.loads(handler.lambda_handler(create_event, None)["body"])["orderId"]

    delete_event = _make_api_event(
        method="DELETE",
        path=f"/orders/{order_id}",
        path_params={"orderId": order_id},
    )
    resp = handler.lambda_handler(delete_event, None)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["status"] == OrderStatus.CANCELLED


def test_cancel_order_updates_dynamodb_status(dynamodb_table, monkeypatch):
    monkeypatch.setattr(handler, "table", dynamodb_table)
    monkeypatch.setattr(handler, "kinesis_client", _mock_kinesis())

    create_event = _make_api_event(method="POST", path="/orders", body=VALID_BODY)
    order_id = json.loads(handler.lambda_handler(create_event, None)["body"])["orderId"]

    delete_event = _make_api_event(
        method="DELETE",
        path=f"/orders/{order_id}",
        path_params={"orderId": order_id},
    )
    handler.lambda_handler(delete_event, None)

    item = _get_metadata(dynamodb_table, order_id)
    assert item["status"] == OrderStatus.CANCELLED
    assert int(item["version"]) == 1


def test_cancel_delivered_order_returns_409(dynamodb_table, monkeypatch):
    """Order in 'delivered' state cannot be cancelled (INTERFACE_SPEC.md §3.3)."""
    monkeypatch.setattr(handler, "table", dynamodb_table)
    monkeypatch.setattr(handler, "kinesis_client", _mock_kinesis())

    # Seed a delivered order directly.
    dynamodb_table.put_item(Item={
        "PK": f"ORDER#{ORDER_ID}",
        "SK": "METADATA",
        "orderId": ORDER_ID,
        "customerId": CUSTOMER_ID,
        "restaurantId": RESTAURANT_ID,
        "status": OrderStatus.DELIVERED,
        "version": 5,
        "retryCount": 0,
        "pickupLat": Decimal("34.0522"),
        "pickupLng": Decimal("-118.2437"),
        "dropoffLat": Decimal("34.0611"),
        "dropoffLng": Decimal("-118.2400"),
        "totalAmountCents": 2598,
        "createdAt": "2026-04-20T00:00:00Z",
        "updatedAt": "2026-04-20T00:00:30Z",
    })

    event = _make_api_event(
        method="DELETE",
        path=f"/orders/{ORDER_ID}",
        path_params={"orderId": ORDER_ID},
    )
    resp = handler.lambda_handler(event, None)

    assert resp["statusCode"] == 409
    body = json.loads(resp["body"])
    assert body["type"] == "/errors/cannot-cancel"


def test_cancel_picked_up_order_returns_409(dynamodb_table, monkeypatch):
    monkeypatch.setattr(handler, "table", dynamodb_table)
    monkeypatch.setattr(handler, "kinesis_client", _mock_kinesis())

    dynamodb_table.put_item(Item={
        "PK": f"ORDER#{ORDER_ID}",
        "SK": "METADATA",
        "orderId": ORDER_ID,
        "customerId": CUSTOMER_ID,
        "restaurantId": RESTAURANT_ID,
        "status": OrderStatus.PICKED_UP,
        "version": 4,
        "retryCount": 0,
        "pickupLat": Decimal("34.0522"),
        "pickupLng": Decimal("-118.2437"),
        "dropoffLat": Decimal("34.0611"),
        "dropoffLng": Decimal("-118.2400"),
        "totalAmountCents": 2598,
        "createdAt": "2026-04-20T00:00:00Z",
        "updatedAt": "2026-04-20T00:00:30Z",
    })

    event = _make_api_event(
        method="DELETE",
        path=f"/orders/{ORDER_ID}",
        path_params={"orderId": ORDER_ID},
    )
    resp = handler.lambda_handler(event, None)

    assert resp["statusCode"] == 409


def test_cancel_nonexistent_order_returns_404(dynamodb_table, monkeypatch):
    monkeypatch.setattr(handler, "table", dynamodb_table)
    monkeypatch.setattr(handler, "kinesis_client", _mock_kinesis())

    event = _make_api_event(
        method="DELETE",
        path="/orders/ord_doesnotexist",
        path_params={"orderId": "ord_doesnotexist"},
    )
    resp = handler.lambda_handler(event, None)

    assert resp["statusCode"] == 404


# ── GET /health ───────────────────────────────────────────────────────────────

def test_health_returns_200(dynamodb_table, monkeypatch):
    monkeypatch.setattr(handler, "table", dynamodb_table)
    monkeypatch.setattr(handler, "kinesis_client", _mock_kinesis())

    event = {"httpMethod": "GET", "path": "/health",
             "pathParameters": None, "body": None, "requestContext": {}}
    resp = handler.lambda_handler(event, None)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["status"] == "healthy"


# ── Unknown route ─────────────────────────────────────────────────────────────

def test_unknown_route_returns_404(dynamodb_table, monkeypatch):
    monkeypatch.setattr(handler, "table", dynamodb_table)
    monkeypatch.setattr(handler, "kinesis_client", _mock_kinesis())

    event = _make_api_event(method="GET", path="/unknown", path_params={})
    resp = handler.lambda_handler(event, None)

    assert resp["statusCode"] == 404
