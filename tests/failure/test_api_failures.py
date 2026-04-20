"""Failure injection tests for API-layer failures.

Covers:
  FL-08 — Lambda timeout / DynamoDB down → API returns 503
  FL-09 — Cognito outage → missing/invalid claims → API returns 400/401

The live-infrastructure variants (real timeout injection, real Cognito disable)
are in tests/failure/scripts/.
"""
import json
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

import lambdas.order_api.handler as order_handler
import lambdas.courier_api.handler as courier_handler


# ── Helpers ───────────────────────────────────────────────────────────────────

CUSTOMER_ID = "cus_FAIL_TEST_0000000000001"
COURIER_ID = "cur_FAIL_TEST_0000000000002"
RESTAURANT_ID = "rst_FAIL_TEST_0000000000003"
ORDER_ID = "ord_FAIL_TEST_0000000000000"

VALID_ORDER_BODY = {
    "restaurantId": RESTAURANT_ID,
    "pickupLocation": {"lat": 34.0522, "lng": -118.2437, "address": "123 Main St"},
    "dropoffLocation": {"lat": 34.0611, "lng": -118.2400, "address": "456 Oak Ave"},
    "items": [{"itemId": "item_1", "name": "Burger", "quantity": 1, "unitPriceCents": 1299}],
}


def _order_event(method="POST", path="/orders", body=None, path_params=None,
                 customer_id=CUSTOMER_ID):
    return {
        "httpMethod": method, "path": path,
        "pathParameters": path_params,
        "body": json.dumps(body) if body else None,
        "requestContext": {"authorizer": {"claims": {"sub": customer_id}}},
    }


def _courier_event(method, path, body=None, path_params=None, caller_id=COURIER_ID):
    return {
        "httpMethod": method, "path": path,
        "pathParameters": path_params or {"courierId": COURIER_ID},
        "body": json.dumps(body) if body else None,
        "requestContext": {"authorizer": {"claims": {"sub": caller_id}}},
    }


def _broken_table(error_code="ServiceUnavailable"):
    """Return a mock DynamoDB table that raises ClientError on any write."""
    mock = MagicMock()
    exc = ClientError(
        {"Error": {"Code": error_code, "Message": "Simulated AWS failure"}},
        "Operation",
    )
    mock.put_item.side_effect = exc
    mock.update_item.side_effect = exc
    mock.get_item.side_effect = exc
    mock.query.side_effect = exc
    mock.delete_item.side_effect = exc
    return mock


# ── FL-08: Lambda timeout / DynamoDB down → 503 ───────────────────────────────

def test_fl08_dynamodb_down_on_post_order_returns_503(monkeypatch):
    """FL-08: DynamoDB ServiceUnavailable on POST /orders → 503.

    Simulates what happens when the Lambda is processing but the downstream
    DynamoDB service is unreachable (e.g. during a VPC endpoint outage or
    Lambda timeout scenario where DynamoDB calls are failing).
    """
    monkeypatch.setattr(order_handler, "table", _broken_table())
    monkeypatch.setattr(order_handler, "kinesis_client", MagicMock())

    event = _order_event("POST", "/orders", body=VALID_ORDER_BODY)
    resp = order_handler.lambda_handler(event, None)

    assert resp["statusCode"] == 503
    body = json.loads(resp["body"])
    assert body["type"] == "/errors/service-unavailable"


def test_fl08_dynamodb_down_on_get_order_returns_503(dynamodb_table, monkeypatch):
    """FL-08: DynamoDB ServiceUnavailable on GET /orders/{id} → 503."""
    monkeypatch.setattr(order_handler, "table", _broken_table())
    monkeypatch.setattr(order_handler, "kinesis_client", MagicMock())

    event = _order_event("GET", "/orders/ord_test", path_params={"orderId": "ord_test"})
    resp = order_handler.lambda_handler(event, None)

    assert resp["statusCode"] == 503


def test_fl08_dynamodb_down_on_location_update_returns_503(monkeypatch):
    """FL-08: Kinesis unavailable on PUT /couriers/{id}/location → 503."""
    mock_k = MagicMock()
    mock_k.put_record.side_effect = ClientError(
        {"Error": {"Code": "ServiceUnavailable", "Message": "Kinesis down"}},
        "PutRecord",
    )
    monkeypatch.setattr(courier_handler, "table", MagicMock())
    monkeypatch.setattr(courier_handler, "kinesis_client", mock_k)

    event = _courier_event(
        "PUT", f"/couriers/{COURIER_ID}/location",
        body={"lat": 34.0522, "lng": -118.2437},
    )
    resp = courier_handler.lambda_handler(event, None)

    assert resp["statusCode"] == 503


def test_fl08_dynamodb_down_on_cancel_returns_503(monkeypatch):
    """FL-08: DynamoDB down on DELETE /orders/{id} → 503."""
    monkeypatch.setattr(order_handler, "table", _broken_table())
    monkeypatch.setattr(order_handler, "kinesis_client", MagicMock())

    event = _order_event(
        "DELETE", f"/orders/{ORDER_ID}",
        path_params={"orderId": ORDER_ID},
    )
    resp = order_handler.lambda_handler(event, None)

    assert resp["statusCode"] == 503


# ── FL-09: Cognito outage → missing/invalid claims ────────────────────────────

def test_fl09_missing_cognito_claims_on_post_order_returns_400(
        dynamodb_table, monkeypatch):
    """FL-09: When Cognito is down, API Gateway passes through with no claims.

    The Lambda sees an empty sub claim and returns 400 — no partial order
    is created in DynamoDB.
    """
    monkeypatch.setattr(order_handler, "table", dynamodb_table)
    monkeypatch.setattr(order_handler, "kinesis_client", MagicMock())

    # Simulate Cognito outage: requestContext has no authorizer claims.
    event = {
        "httpMethod": "POST",
        "path": "/orders",
        "pathParameters": None,
        "body": json.dumps(VALID_ORDER_BODY),
        "requestContext": {},  # no authorizer at all
    }
    resp = order_handler.lambda_handler(event, None)

    assert resp["statusCode"] == 400
    # Verify no order was written to DynamoDB.
    result = dynamodb_table.scan()
    assert result["Count"] == 0


def test_fl09_empty_sub_claim_returns_400(dynamodb_table, monkeypatch):
    """FL-09: Empty sub claim (malformed Cognito token) → 400, no DB write."""
    monkeypatch.setattr(order_handler, "table", dynamodb_table)
    monkeypatch.setattr(order_handler, "kinesis_client", MagicMock())

    event = _order_event("POST", "/orders", body=VALID_ORDER_BODY, customer_id="")
    resp = order_handler.lambda_handler(event, None)

    assert resp["statusCode"] == 400
    assert dynamodb_table.scan()["Count"] == 0


def test_fl09_cognito_outage_health_endpoint_still_responds(monkeypatch):
    """FL-09: /health endpoint requires no auth — stays reachable during outage."""
    monkeypatch.setattr(order_handler, "table", MagicMock())
    monkeypatch.setattr(order_handler, "kinesis_client", MagicMock())

    event = {
        "httpMethod": "GET",
        "path": "/health",
        "pathParameters": None,
        "body": None,
        "requestContext": {},  # no auth
    }
    resp = order_handler.lambda_handler(event, None)

    # Health check must return 200 even without Cognito claims.
    assert resp["statusCode"] == 200


def test_fl09_sc04_cross_courier_access_blocked(dynamodb_table, monkeypatch):
    """SC-04 / FL-09: Valid token but wrong identity → 403 Forbidden.

    Even during a partial Cognito degradation where tokens are still issued,
    the application-layer role check must block cross-resource access.
    """
    monkeypatch.setattr(courier_handler, "table", dynamodb_table)
    monkeypatch.setattr(courier_handler, "kinesis_client", MagicMock())

    attacker_id = "cur_ATTACKER_00000000000000"
    victim_id = "cur_VICTIM_000000000000000"

    event = _courier_event(
        "GET",
        f"/couriers/{victim_id}/assignment",
        path_params={"courierId": victim_id},
        caller_id=attacker_id,
    )
    resp = courier_handler.lambda_handler(event, None)

    assert resp["statusCode"] == 403
    body = json.loads(resp["body"])
    assert body["type"] == "/errors/forbidden"
