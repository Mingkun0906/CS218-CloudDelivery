"""Failure injection tests for MatchingLambda.

These tests cover the failure scenarios in TEST_CASES.md (FL-01–FL-10) that
can be exercised purely with mocks. Scenarios requiring real AWS infrastructure
(FL-03 Redis failover, FL-07 Kinesis iterator expiry) are documented as TODOs.

FL-01 — Double assignment: tested here via concurrent conditional write logic.
FL-02 — Cold start surge: cannot be tested here; covered by CloudWatch metrics.
FL-04 — DynamoDB throttling: tested here by injecting ClientError.
FL-05 — Lambda crash / timeout: tested here by injecting unhandled exception.

Tests use the same conftest fixtures as unit tests.
"""
import json
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError

from tests.helpers import make_kinesis_event

import lambdas.matching.handler as handler


# ── Constants ─────────────────────────────────────────────────────────────────

ORDER_ID = "ord_FAIL_TEST_000000000000000"
COURIER_A = "cur_FAIL_A_00000000000000001"
COURIER_B = "cur_FAIL_B_00000000000000002"
CUSTOMER_ID = "cus_FAIL_TEST_0000000000001"
RESTAURANT_ID = "rst_FAIL_TEST_0000000000001"

PICKUP_LAT = 34.0522
PICKUP_LNG = -118.2437


def _seed_order(table, order_id=ORDER_ID, status="matching"):
    table.put_item(Item={
        "PK": f"ORDER#{order_id}",
        "SK": "METADATA",
        "orderId": order_id,
        "customerId": CUSTOMER_ID,
        "restaurantId": RESTAURANT_ID,
        "status": status,
        "version": 1,
        "retryCount": 0,
        "createdAt": "2026-04-15T00:00:00Z",
        "updatedAt": "2026-04-15T00:00:00Z",
    })


def _seed_courier(fake_redis, courier_id, lat=34.0505, lng=-118.2420):
    fake_redis.geoadd("couriers:locations", [lng, lat, courier_id])
    fake_redis.set(f"courier:ttl:{courier_id}", "", ex=30)


def _patch_handler(monkeypatch, fake_redis, table, topic_arn=None):
    monkeypatch.setattr(handler, "redis_client", fake_redis)
    monkeypatch.setattr(handler, "table", table)
    monkeypatch.setattr("lambdas.matching.handler.metrics.emit", MagicMock())
    if topic_arn:
        monkeypatch.setattr(handler, "MATCH_TOPIC_ARN", topic_arn)


def _make_event(order_id=ORDER_ID):
    return make_kinesis_event({
        "orderId": order_id,
        "customerId": CUSTOMER_ID,
        "restaurantId": RESTAURANT_ID,
        "pickupLat": PICKUP_LAT,
        "pickupLng": PICKUP_LNG,
        "dropoffLat": 34.0611,
        "dropoffLng": -118.24,
        "totalAmountCents": 2598,
        "createdAt": "2026-04-15T00:00:00Z",
    })


# ── FL-01: Double assignment prevention ───────────────────────────────────────

def test_fl01_two_couriers_only_one_wins(
    monkeypatch, fake_redis, dynamodb_table, sns_topic, aws_mock
):
    """FL-01: When two Lambda invocations race, exactly one courier is assigned.

    Simulates by running the handler twice sequentially (the second invocation
    sees the order already assigned and skips gracefully).
    """
    _seed_order(dynamodb_table)
    _seed_courier(fake_redis, COURIER_A)
    _seed_courier(fake_redis, COURIER_B, lat=34.0495, lng=-118.2415)
    _patch_handler(monkeypatch, fake_redis, dynamodb_table, topic_arn=sns_topic)

    event = _make_event()

    # First invocation assigns successfully
    result1 = handler.lambda_handler(event, None)
    assert result1["batchItemFailures"] == []

    item = dynamodb_table.get_item(
        Key={"PK": f"ORDER#{ORDER_ID}", "SK": "METADATA"}
    )["Item"]
    first_courier = item["courierId"]
    assert first_courier in (COURIER_A, COURIER_B)

    # Second invocation: all conditional writes fail (order already has courierId)
    result2 = handler.lambda_handler(event, None)
    # Should NOT fail the batch — conflict is handled gracefully
    assert result2["batchItemFailures"] == []

    # Courier must not have changed
    item_after = dynamodb_table.get_item(
        Key={"PK": f"ORDER#{ORDER_ID}", "SK": "METADATA"}
    )["Item"]
    assert item_after["courierId"] == first_courier


# ── FL-04: DynamoDB throttling ────────────────────────────────────────────────

def test_fl04_dynamodb_throttle_on_conditional_write_returns_batch_failure(
    monkeypatch, fake_redis, dynamodb_table, aws_mock
):
    """FL-04: ProvisionedThroughputExceededException propagates as batch failure.

    In production, Lambda will retry the record. The handler re-raises any
    ClientError that is NOT ConditionalCheckFailedException, causing
    batchItemFailures to be populated for Kinesis partial-retry.
    """
    _seed_order(dynamodb_table)
    _seed_courier(fake_redis, COURIER_A)

    # Patch the table's update_item to raise a throttle error
    def _throttle(*args, **kwargs):
        raise ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "throttled"}},
            "UpdateItem",
        )

    mock_table = MagicMock()
    mock_table.update_item.side_effect = _throttle

    monkeypatch.setattr(handler, "redis_client", fake_redis)
    monkeypatch.setattr(handler, "table", mock_table)
    monkeypatch.setattr("lambdas.matching.handler.metrics.emit", MagicMock())

    result = handler.lambda_handler(_make_event(), None)

    assert len(result["batchItemFailures"]) == 1


# ── FL-02: No couriers surge (application-side) ───────────────────────────────

def test_fl02_all_orders_unassigned_when_no_couriers(
    monkeypatch, fake_redis, dynamodb_table, aws_mock
):
    """FL-02 (app-side): Under surge with no available couriers, all orders
    go to unassigned without dropping events or failing the batch.

    The Kinesis retry behaviour (FL-02 burst absorption) is covered by
    CloudWatch metrics / integration tests — not testable here with mocks.
    """
    for i in range(5):
        oid = f"ord_SURGE_{i:04d}_0000000000000"
        _seed_order(dynamodb_table, order_id=oid)

    _patch_handler(monkeypatch, fake_redis, dynamodb_table)

    import base64

    records = []
    for i in range(5):
        oid = f"ord_SURGE_{i:04d}_0000000000000"
        payload = {
            "orderId": oid,
            "customerId": CUSTOMER_ID,
            "restaurantId": RESTAURANT_ID,
            "pickupLat": PICKUP_LAT,
            "pickupLng": PICKUP_LNG,
            "dropoffLat": 34.06,
            "dropoffLng": -118.24,
            "totalAmountCents": 1000,
            "createdAt": "2026-04-15T00:00:00Z",
        }
        envelope = {
            "eventId": f"evt_SURGE_{i:04d}",
            "eventType": "ORDER_PLACED",
            "schemaVersion": "1.0",
            "timestamp": "2026-04-15T00:00:00Z",
            "traceId": "unknown",
            "payload": payload,
        }
        data_b64 = base64.b64encode(json.dumps(envelope).encode()).decode()
        records.append({
            "kinesis": {
                "sequenceNumber": f"seq-surge-{i:04d}",
                "data": data_b64,
                "partitionKey": oid,
            },
            "eventSource": "aws:kinesis",
        })

    result = handler.lambda_handler({"Records": records}, None)

    # No batch failures (unassigned is handled gracefully)
    assert result["batchItemFailures"] == []

    for i in range(5):
        oid = f"ord_SURGE_{i:04d}_0000000000000"
        item = dynamodb_table.get_item(Key={"PK": f"ORDER#{oid}", "SK": "METADATA"})["Item"]
        assert item["status"] == "unassigned"


# ── FL-05: Lambda crash / unhandled exception ─────────────────────────────────

def test_fl05_redis_connection_error_returns_batch_failure(
    monkeypatch, fake_redis, dynamodb_table, aws_mock
):
    """FL-05: If Redis raises unexpectedly (e.g. connection dropped), the record
    is added to batchItemFailures so Kinesis retries it.
    """
    import redis

    broken_redis = MagicMock()
    broken_redis.geosearch.side_effect = redis.ConnectionError("Redis unavailable")

    monkeypatch.setattr(handler, "redis_client", broken_redis)
    monkeypatch.setattr(handler, "table", dynamodb_table)
    monkeypatch.setattr("lambdas.matching.handler.metrics.emit", MagicMock())

    result = handler.lambda_handler(_make_event(), None)

    assert len(result["batchItemFailures"]) == 1


# ── FL-10: Cascading failure ────────────────────────────────────────────────────

def test_fl10_both_redis_and_dynamodb_fail_returns_batch_failure(
    monkeypatch, fake_redis, dynamodb_table, aws_mock
):
    """FL-10 (app-side): Both Redis and DynamoDB raise → batch failure, no hang."""
    import redis

    broken_redis = MagicMock()
    broken_redis.geosearch.side_effect = redis.ConnectionError("Redis down")

    broken_table = MagicMock()
    broken_table.update_item.side_effect = ClientError(
        {"Error": {"Code": "ServiceUnavailable", "Message": "DynamoDB down"}},
        "UpdateItem",
    )

    monkeypatch.setattr(handler, "redis_client", broken_redis)
    monkeypatch.setattr(handler, "table", broken_table)
    monkeypatch.setattr("lambdas.matching.handler.metrics.emit", MagicMock())

    result = handler.lambda_handler(_make_event(), None)

    assert len(result["batchItemFailures"]) == 1
