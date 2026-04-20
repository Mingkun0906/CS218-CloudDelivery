"""Unit tests for CourierAPILambda (lambdas/courier_api/handler.py)."""
import json
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

import lambdas.courier_api.handler as handler
from lambdas.shared.models import OrderStatus

# ── Constants ─────────────────────────────────────────────────────────────────

COURIER_ID = "cur_01HVTEST0000000000000000001"
OTHER_COURIER = "cur_01HVTEST0000000000000000099"
CUSTOMER_ID = "cus_01HVTEST0000000000000000002"
RESTAURANT_ID = "rst_01HVTEST0000000000000000003"
ORDER_ID = "ord_01HVTEST0000000000000000000"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_event(method, path, body=None, path_params=None, caller_id=COURIER_ID):
    return {
        "httpMethod": method,
        "path": path,
        "pathParameters": path_params or {"courierId": COURIER_ID},
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {
            "authorizer": {"claims": {"sub": caller_id, "cognito:groups": "couriers"}}
        },
    }


def _mock_kinesis():
    mock = MagicMock()
    mock.put_record.return_value = {"ShardId": "shardId-000000000000", "SequenceNumber": "1"}
    return mock


def _seed_order(table, order_id=ORDER_ID, status=OrderStatus.ASSIGNED,
                courier_id=COURIER_ID, version=1):
    table.put_item(Item={
        "PK": f"ORDER#{order_id}",
        "SK": "METADATA",
        "orderId": order_id,
        "customerId": CUSTOMER_ID,
        "restaurantId": RESTAURANT_ID,
        "courierId": courier_id,
        "status": status,
        "pickupLat": Decimal("34.0522"),
        "pickupLng": Decimal("-118.2437"),
        "pickupAddress": "123 Main St",
        "dropoffLat": Decimal("34.0611"),
        "dropoffLng": Decimal("-118.2400"),
        "dropoffAddress": "456 Oak Ave",
        "totalAmountCents": 2598,
        "version": version,
        "retryCount": 0,
        "createdAt": "2026-04-20T00:00:00Z",
        "updatedAt": "2026-04-20T00:00:00Z",
    })


def _seed_assignment(table, courier_id=COURIER_ID, order_id=ORDER_ID):
    table.put_item(Item={
        "PK": f"COURIER#{courier_id}",
        "SK": "ASSIGNMENT",
        "orderId": order_id,
        "assignedAt": "2026-04-20T00:00:03Z",
    })


def _get_order_status(table, order_id=ORDER_ID):
    resp = table.get_item(Key={"PK": f"ORDER#{order_id}", "SK": "METADATA"})
    return resp.get("Item", {}).get("status")


def _get_assignment_record(table, courier_id=COURIER_ID):
    resp = table.get_item(Key={"PK": f"COURIER#{courier_id}", "SK": "ASSIGNMENT"})
    return resp.get("Item")


# ── PUT /couriers/{id}/location ───────────────────────────────────────────────

def test_location_update_returns_204(dynamodb_table, monkeypatch):
    monkeypatch.setattr(handler, "table", dynamodb_table)
    mock_k = _mock_kinesis()
    monkeypatch.setattr(handler, "kinesis_client", mock_k)

    event = _make_event(
        "PUT", f"/couriers/{COURIER_ID}/location",
        body={"lat": 34.0522, "lng": -118.2437},
    )
    resp = handler.lambda_handler(event, None)
    assert resp["statusCode"] == 204


def test_location_update_publishes_to_kinesis(dynamodb_table, monkeypatch):
    monkeypatch.setattr(handler, "table", dynamodb_table)
    mock_k = _mock_kinesis()
    monkeypatch.setattr(handler, "kinesis_client", mock_k)

    event = _make_event(
        "PUT", f"/couriers/{COURIER_ID}/location",
        body={"lat": 34.0522, "lng": -118.2437},
    )
    handler.lambda_handler(event, None)

    mock_k.put_record.assert_called_once()
    envelope = json.loads(mock_k.put_record.call_args[1]["Data"].decode())
    assert envelope["eventType"] == "LOCATION_UPDATE"
    assert envelope["payload"]["courierId"] == COURIER_ID
    assert envelope["payload"]["lat"] == pytest.approx(34.0522)


def test_location_update_invalid_gps_returns_400(dynamodb_table, monkeypatch):
    monkeypatch.setattr(handler, "table", dynamodb_table)
    monkeypatch.setattr(handler, "kinesis_client", _mock_kinesis())

    event = _make_event(
        "PUT", f"/couriers/{COURIER_ID}/location",
        body={"lat": 200, "lng": 500},
    )
    resp = handler.lambda_handler(event, None)
    assert resp["statusCode"] == 400


def test_location_update_missing_coords_returns_400(dynamodb_table, monkeypatch):
    monkeypatch.setattr(handler, "table", dynamodb_table)
    monkeypatch.setattr(handler, "kinesis_client", _mock_kinesis())

    event = _make_event("PUT", f"/couriers/{COURIER_ID}/location", body={"lat": 34.0})
    resp = handler.lambda_handler(event, None)
    assert resp["statusCode"] == 400


# ── GET /couriers/{id}/assignment ─────────────────────────────────────────────

def test_get_assignment_returns_200_when_assigned(dynamodb_table, monkeypatch):
    monkeypatch.setattr(handler, "table", dynamodb_table)
    monkeypatch.setattr(handler, "kinesis_client", _mock_kinesis())

    _seed_order(dynamodb_table)
    _seed_assignment(dynamodb_table)

    event = _make_event("GET", f"/couriers/{COURIER_ID}/assignment")
    resp = handler.lambda_handler(event, None)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["orderId"] == ORDER_ID
    assert body["restaurantId"] == RESTAURANT_ID
    assert body["orderStatus"] == OrderStatus.ASSIGNED
    assert "pickupLocation" in body
    assert "dropoffLocation" in body
    assert "assignedAt" in body


def test_get_assignment_returns_204_when_no_record(dynamodb_table, monkeypatch):
    monkeypatch.setattr(handler, "table", dynamodb_table)
    monkeypatch.setattr(handler, "kinesis_client", _mock_kinesis())

    event = _make_event("GET", f"/couriers/{COURIER_ID}/assignment")
    resp = handler.lambda_handler(event, None)
    assert resp["statusCode"] == 204


def test_get_assignment_returns_204_when_order_reassigned(dynamodb_table, monkeypatch):
    """If the order's courierId no longer matches, treat as no active assignment."""
    monkeypatch.setattr(handler, "table", dynamodb_table)
    monkeypatch.setattr(handler, "kinesis_client", _mock_kinesis())

    _seed_order(dynamodb_table, courier_id=OTHER_COURIER)  # reassigned away
    _seed_assignment(dynamodb_table, courier_id=COURIER_ID)

    event = _make_event("GET", f"/couriers/{COURIER_ID}/assignment")
    resp = handler.lambda_handler(event, None)
    assert resp["statusCode"] == 204


# ── PUT /couriers/{id}/assignment/accept ──────────────────────────────────────

def test_accept_assignment_returns_200(dynamodb_table, monkeypatch):
    monkeypatch.setattr(handler, "table", dynamodb_table)
    monkeypatch.setattr(handler, "kinesis_client", _mock_kinesis())

    _seed_order(dynamodb_table)
    _seed_assignment(dynamodb_table)

    event = _make_event("PUT", f"/couriers/{COURIER_ID}/assignment/accept", body={})
    resp = handler.lambda_handler(event, None)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["orderId"] == ORDER_ID
    assert body["status"] == OrderStatus.ASSIGNED


def test_accept_with_no_assignment_returns_404(dynamodb_table, monkeypatch):
    monkeypatch.setattr(handler, "table", dynamodb_table)
    monkeypatch.setattr(handler, "kinesis_client", _mock_kinesis())

    event = _make_event("PUT", f"/couriers/{COURIER_ID}/assignment/accept", body={})
    resp = handler.lambda_handler(event, None)
    assert resp["statusCode"] == 404


# ── PUT /couriers/{id}/assignment/reject ──────────────────────────────────────

def test_reject_sets_order_to_reassigning(dynamodb_table, monkeypatch):
    monkeypatch.setattr(handler, "table", dynamodb_table)
    monkeypatch.setattr(handler, "kinesis_client", _mock_kinesis())

    _seed_order(dynamodb_table)
    _seed_assignment(dynamodb_table)

    event = _make_event(
        "PUT", f"/couriers/{COURIER_ID}/assignment/reject",
        body={"reason": "too_far"},
    )
    resp = handler.lambda_handler(event, None)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["status"] == OrderStatus.REASSIGNING
    assert _get_order_status(dynamodb_table) == OrderStatus.REASSIGNING


def test_reject_clears_courier_id_from_order(dynamodb_table, monkeypatch):
    monkeypatch.setattr(handler, "table", dynamodb_table)
    monkeypatch.setattr(handler, "kinesis_client", _mock_kinesis())

    _seed_order(dynamodb_table)
    _seed_assignment(dynamodb_table)

    event = _make_event(
        "PUT", f"/couriers/{COURIER_ID}/assignment/reject",
        body={"reason": "vehicle_issue"},
    )
    handler.lambda_handler(event, None)

    item = dynamodb_table.get_item(
        Key={"PK": f"ORDER#{ORDER_ID}", "SK": "METADATA"}
    )["Item"]
    assert "courierId" not in item


def test_reject_deletes_courier_assignment_record(dynamodb_table, monkeypatch):
    monkeypatch.setattr(handler, "table", dynamodb_table)
    monkeypatch.setattr(handler, "kinesis_client", _mock_kinesis())

    _seed_order(dynamodb_table)
    _seed_assignment(dynamodb_table)

    event = _make_event(
        "PUT", f"/couriers/{COURIER_ID}/assignment/reject",
        body={"reason": "other"},
    )
    handler.lambda_handler(event, None)

    assert _get_assignment_record(dynamodb_table) is None


def test_reject_republishes_order_placed_to_kinesis(dynamodb_table, monkeypatch):
    monkeypatch.setattr(handler, "table", dynamodb_table)
    mock_k = _mock_kinesis()
    monkeypatch.setattr(handler, "kinesis_client", mock_k)

    _seed_order(dynamodb_table)
    _seed_assignment(dynamodb_table)

    event = _make_event(
        "PUT", f"/couriers/{COURIER_ID}/assignment/reject",
        body={"reason": "too_far"},
    )
    handler.lambda_handler(event, None)

    mock_k.put_record.assert_called_once()
    envelope = json.loads(mock_k.put_record.call_args[1]["Data"].decode())
    assert envelope["eventType"] == "ORDER_PLACED"
    assert envelope["payload"]["orderId"] == ORDER_ID


def test_reject_invalid_reason_returns_400(dynamodb_table, monkeypatch):
    monkeypatch.setattr(handler, "table", dynamodb_table)
    monkeypatch.setattr(handler, "kinesis_client", _mock_kinesis())

    event = _make_event(
        "PUT", f"/couriers/{COURIER_ID}/assignment/reject",
        body={"reason": "bad_reason"},
    )
    resp = handler.lambda_handler(event, None)
    assert resp["statusCode"] == 400


def test_reject_delivered_order_returns_409(dynamodb_table, monkeypatch):
    monkeypatch.setattr(handler, "table", dynamodb_table)
    monkeypatch.setattr(handler, "kinesis_client", _mock_kinesis())

    _seed_order(dynamodb_table, status=OrderStatus.DELIVERED)
    _seed_assignment(dynamodb_table)

    event = _make_event(
        "PUT", f"/couriers/{COURIER_ID}/assignment/reject",
        body={"reason": "too_far"},
    )
    resp = handler.lambda_handler(event, None)
    assert resp["statusCode"] == 409


# ── PUT /couriers/{id}/assignment/status ──────────────────────────────────────

def test_status_update_to_picked_up_succeeds(dynamodb_table, monkeypatch):
    monkeypatch.setattr(handler, "table", dynamodb_table)
    monkeypatch.setattr(handler, "kinesis_client", _mock_kinesis())

    _seed_order(dynamodb_table, status=OrderStatus.READY_FOR_PICKUP)
    _seed_assignment(dynamodb_table)

    event = _make_event(
        "PUT", f"/couriers/{COURIER_ID}/assignment/status",
        body={"status": "picked_up"},
    )
    resp = handler.lambda_handler(event, None)

    assert resp["statusCode"] == 200
    assert _get_order_status(dynamodb_table) == OrderStatus.PICKED_UP


def test_status_update_to_delivered_succeeds(dynamodb_table, monkeypatch):
    monkeypatch.setattr(handler, "table", dynamodb_table)
    monkeypatch.setattr(handler, "kinesis_client", _mock_kinesis())

    _seed_order(dynamodb_table, status=OrderStatus.PICKED_UP)
    _seed_assignment(dynamodb_table)

    event = _make_event(
        "PUT", f"/couriers/{COURIER_ID}/assignment/status",
        body={"status": "delivered"},
    )
    resp = handler.lambda_handler(event, None)

    assert resp["statusCode"] == 200
    assert _get_order_status(dynamodb_table) == OrderStatus.DELIVERED


def test_delivered_clears_courier_assignment_record(dynamodb_table, monkeypatch):
    monkeypatch.setattr(handler, "table", dynamodb_table)
    monkeypatch.setattr(handler, "kinesis_client", _mock_kinesis())

    _seed_order(dynamodb_table, status=OrderStatus.PICKED_UP)
    _seed_assignment(dynamodb_table)

    event = _make_event(
        "PUT", f"/couriers/{COURIER_ID}/assignment/status",
        body={"status": "delivered"},
    )
    handler.lambda_handler(event, None)

    assert _get_assignment_record(dynamodb_table) is None


def test_status_update_wrong_transition_returns_409(dynamodb_table, monkeypatch):
    """Cannot go to picked_up from assigned state (must be ready_for_pickup first)."""
    monkeypatch.setattr(handler, "table", dynamodb_table)
    monkeypatch.setattr(handler, "kinesis_client", _mock_kinesis())

    _seed_order(dynamodb_table, status=OrderStatus.ASSIGNED)
    _seed_assignment(dynamodb_table)

    event = _make_event(
        "PUT", f"/couriers/{COURIER_ID}/assignment/status",
        body={"status": "picked_up"},
    )
    resp = handler.lambda_handler(event, None)
    assert resp["statusCode"] == 409


def test_status_update_invalid_value_returns_400(dynamodb_table, monkeypatch):
    monkeypatch.setattr(handler, "table", dynamodb_table)
    monkeypatch.setattr(handler, "kinesis_client", _mock_kinesis())

    _seed_order(dynamodb_table)
    _seed_assignment(dynamodb_table)

    event = _make_event(
        "PUT", f"/couriers/{COURIER_ID}/assignment/status",
        body={"status": "on_the_way"},
    )
    resp = handler.lambda_handler(event, None)
    assert resp["statusCode"] == 400


# ── SC-04: cross-courier access blocked ──────────────────────────────────────

def test_accessing_another_couriers_resource_returns_403(dynamodb_table, monkeypatch):
    monkeypatch.setattr(handler, "table", dynamodb_table)
    monkeypatch.setattr(handler, "kinesis_client", _mock_kinesis())

    # Caller is COURIER_ID but path says OTHER_COURIER.
    event = _make_event(
        "GET",
        f"/couriers/{OTHER_COURIER}/assignment",
        path_params={"courierId": OTHER_COURIER},
        caller_id=COURIER_ID,
    )
    resp = handler.lambda_handler(event, None)
    assert resp["statusCode"] == 403
