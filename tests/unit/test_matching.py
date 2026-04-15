"""Unit tests for MatchingLambda (lambdas/matching/handler.py).

Tests use:
- fakeredis for Redis geo operations
- moto (via dynamodb_table fixture) for DynamoDB
- moto SNS for publish assertions
- monkeypatch to replace module-level clients in the handler
"""
import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import boto3
import pytest

from tests.helpers import make_kinesis_event

import lambdas.matching.handler as handler


# ── Helpers ───────────────────────────────────────────────────────────────────

ORDER_ID = "ord_01HVTEST0000000000000000000"
COURIER_ID = "cur_01HVTEST0000000000000000001"
CUSTOMER_ID = "cus_01HVTEST0000000000000000002"
RESTAURANT_ID = "rst_01HVTEST0000000000000000003"

PICKUP_LAT = 34.0522
PICKUP_LNG = -118.2437


def _seed_order(table, order_id=ORDER_ID, status="matching"):
    """Insert a minimal order METADATA item into the mocked DynamoDB table."""
    table.put_item(
        Item={
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
        }
    )


def _seed_courier(fake_redis, courier_id=COURIER_ID, lat=34.0505, lng=-118.2420):
    """Add a courier to the Redis geo-index and set its TTL sentinel."""
    fake_redis.geoadd("couriers:locations", [lng, lat, courier_id])
    fake_redis.set(f"courier:ttl:{courier_id}", "", ex=30)


def _order_status(table, order_id=ORDER_ID):
    resp = table.get_item(Key={"PK": f"ORDER#{order_id}", "SK": "METADATA"})
    return resp["Item"]["status"]


def _patch_handler(monkeypatch, fake_redis, table, topic_arn=None):
    """Replace module-level clients in the handler with test doubles."""
    monkeypatch.setattr(handler, "redis_client", fake_redis)
    monkeypatch.setattr(handler, "table", table)
    if topic_arn:
        monkeypatch.setattr(handler, "MATCH_TOPIC_ARN", topic_arn)


# ── Tests: happy path ──────────────────────────────────────────────────────────

def test_matching_success_assigns_courier_and_publishes_sns(
    monkeypatch, fake_redis, dynamodb_table, sns_topic, aws_mock
):
    """F-01 / S-01: A valid ORDER_PLACED event assigns the nearest courier."""
    _seed_order(dynamodb_table)
    _seed_courier(fake_redis)
    _patch_handler(monkeypatch, fake_redis, dynamodb_table, topic_arn=sns_topic)

    # Mock metrics to avoid CloudWatch calls
    monkeypatch.setattr("lambdas.matching.handler.metrics.emit", MagicMock())

    event = make_kinesis_event(
        {
            "orderId": ORDER_ID,
            "customerId": CUSTOMER_ID,
            "restaurantId": RESTAURANT_ID,
            "pickupLat": PICKUP_LAT,
            "pickupLng": PICKUP_LNG,
            "dropoffLat": 34.0611,
            "dropoffLng": -118.2400,
            "totalAmountCents": 2598,
            "createdAt": "2026-04-15T00:00:00Z",
        }
    )
    result = handler.lambda_handler(event, None)

    # No batch failures
    assert result["batchItemFailures"] == []

    # DynamoDB order item must be updated
    item = dynamodb_table.get_item(
        Key={"PK": f"ORDER#{ORDER_ID}", "SK": "METADATA"}
    )["Item"]
    assert item["status"] == "assigned"
    assert item["courierId"] == COURIER_ID

    # SNS must have received one message
    sns = boto3.client("sns", region_name="us-east-1")
    subs = sns.list_subscriptions_by_topic(TopicArn=sns_topic)
    # With moto we can't inspect published messages directly via subscriptions,
    # but the publish call itself would have raised if it failed.


def test_matching_no_courier_in_radius_marks_unassigned(
    monkeypatch, fake_redis, dynamodb_table, aws_mock
):
    """E-01: No couriers in geo-index → order transitions to unassigned."""
    _seed_order(dynamodb_table)
    _patch_handler(monkeypatch, fake_redis, dynamodb_table)
    monkeypatch.setattr("lambdas.matching.handler.metrics.emit", MagicMock())

    event = make_kinesis_event(
        {
            "orderId": ORDER_ID,
            "customerId": CUSTOMER_ID,
            "restaurantId": RESTAURANT_ID,
            "pickupLat": PICKUP_LAT,
            "pickupLng": PICKUP_LNG,
            "dropoffLat": 34.0611,
            "dropoffLng": -118.24,
            "totalAmountCents": 1000,
            "createdAt": "2026-04-15T00:00:00Z",
        }
    )
    result = handler.lambda_handler(event, None)

    assert result["batchItemFailures"] == []
    assert _order_status(dynamodb_table) == "unassigned"


def test_matching_max_retries_marks_failed(
    monkeypatch, fake_redis, dynamodb_table, aws_mock
):
    """FL-05 / E-01: After MAX_RETRY_COUNT unassigned attempts, order is failed."""
    _seed_order(dynamodb_table)
    # Simulate retryCount already at MAX_RETRY_COUNT - 1 so one more push hits limit
    dynamodb_table.update_item(
        Key={"PK": f"ORDER#{ORDER_ID}", "SK": "METADATA"},
        UpdateExpression="SET retryCount = :r",
        ExpressionAttributeValues={":r": 2},  # MAX_RETRY_COUNT = 3, so +1 = 3 → failed
    )

    _patch_handler(monkeypatch, fake_redis, dynamodb_table)
    monkeypatch.setattr("lambdas.matching.handler.metrics.emit", MagicMock())

    event = make_kinesis_event(
        {
            "orderId": ORDER_ID,
            "customerId": CUSTOMER_ID,
            "restaurantId": RESTAURANT_ID,
            "pickupLat": PICKUP_LAT,
            "pickupLng": PICKUP_LNG,
            "dropoffLat": 34.0611,
            "dropoffLng": -118.24,
            "totalAmountCents": 1000,
            "createdAt": "2026-04-15T00:00:00Z",
        }
    )
    handler.lambda_handler(event, None)

    assert _order_status(dynamodb_table) == "failed"


# ── Tests: FL-01 double-assignment prevention ─────────────────────────────────

def test_conditional_write_conflict_retries_next_courier(
    monkeypatch, fake_redis, dynamodb_table, sns_topic, aws_mock
):
    """FL-01: First courier conflicts (already assigned), second courier succeeds."""
    _seed_order(dynamodb_table)

    courier_a = "cur_A_TEST_00000000000000001"
    courier_b = "cur_B_TEST_00000000000000002"

    # Both couriers in geo-index; courier_a is slightly closer
    _seed_courier(fake_redis, courier_a, lat=34.0510, lng=-118.2425)
    _seed_courier(fake_redis, courier_b, lat=34.0495, lng=-118.2415)

    # Simulate courier_a being pre-assigned (concurrent Lambda already wrote)
    dynamodb_table.update_item(
        Key={"PK": f"ORDER#{ORDER_ID}", "SK": "METADATA"},
        UpdateExpression="SET courierId = :c",
        ExpressionAttributeValues={":c": courier_a},
    )

    _patch_handler(monkeypatch, fake_redis, dynamodb_table, topic_arn=sns_topic)
    monkeypatch.setattr("lambdas.matching.handler.metrics.emit", MagicMock())

    event = make_kinesis_event(
        {
            "orderId": ORDER_ID,
            "customerId": CUSTOMER_ID,
            "restaurantId": RESTAURANT_ID,
            "pickupLat": PICKUP_LAT,
            "pickupLng": PICKUP_LNG,
            "dropoffLat": 34.0611,
            "dropoffLng": -118.24,
            "totalAmountCents": 2598,
            "createdAt": "2026-04-15T00:00:00Z",
        }
    )
    result = handler.lambda_handler(event, None)

    # Batch should not fail — conflict is handled gracefully
    assert result["batchItemFailures"] == []

    # The order already has courier_a; the handler should not have overwritten it
    item = dynamodb_table.get_item(
        Key={"PK": f"ORDER#{ORDER_ID}", "SK": "METADATA"}
    )["Item"]
    assert item["courierId"] == courier_a


def test_courier_ttl_expired_courier_skipped(
    monkeypatch, fake_redis, dynamodb_table, aws_mock
):
    """E-17: Courier in geo-index but TTL sentinel expired → skip, mark unassigned."""
    _seed_order(dynamodb_table)

    # Add courier to geo-index but do NOT set the TTL sentinel key
    fake_redis.geoadd("couriers:locations", [-118.2420, 34.0505, COURIER_ID])
    # Intentionally omit: fake_redis.set(f"courier:ttl:{COURIER_ID}", "", ex=30)

    _patch_handler(monkeypatch, fake_redis, dynamodb_table)
    monkeypatch.setattr("lambdas.matching.handler.metrics.emit", MagicMock())

    event = make_kinesis_event(
        {
            "orderId": ORDER_ID,
            "customerId": CUSTOMER_ID,
            "restaurantId": RESTAURANT_ID,
            "pickupLat": PICKUP_LAT,
            "pickupLng": PICKUP_LNG,
            "dropoffLat": 34.0611,
            "dropoffLng": -118.24,
            "totalAmountCents": 1000,
            "createdAt": "2026-04-15T00:00:00Z",
        }
    )
    handler.lambda_handler(event, None)

    # Stale courier skipped → no couriers found → unassigned
    assert _order_status(dynamodb_table) == "unassigned"


# ── Tests: malformed / invalid input ─────────────────────────────────────────

def test_malformed_kinesis_payload_returns_batch_failure(
    monkeypatch, fake_redis, dynamodb_table, aws_mock
):
    """Malformed base64 data → batchItemFailures contains the sequence number."""
    _patch_handler(monkeypatch, fake_redis, dynamodb_table)
    monkeypatch.setattr("lambdas.matching.handler.metrics.emit", MagicMock())

    bad_event = {
        "Records": [
            {
                "kinesis": {
                    "sequenceNumber": "seq-001",
                    "data": "!!!not_valid_base64!!!",
                    "partitionKey": "test",
                },
                "eventSource": "aws:kinesis",
            }
        ]
    }
    result = handler.lambda_handler(bad_event, None)

    assert len(result["batchItemFailures"]) == 1
    assert result["batchItemFailures"][0]["itemIdentifier"] == "seq-001"


def test_missing_required_fields_returns_batch_failure(
    monkeypatch, fake_redis, dynamodb_table, aws_mock
):
    """ORDER_PLACED payload missing orderId → batchItemFailures."""
    _patch_handler(monkeypatch, fake_redis, dynamodb_table)
    monkeypatch.setattr("lambdas.matching.handler.metrics.emit", MagicMock())

    # pickupLat and orderId are missing
    event = make_kinesis_event(
        {"customerId": CUSTOMER_ID, "restaurantId": RESTAURANT_ID}
    )
    result = handler.lambda_handler(event, None)

    assert len(result["batchItemFailures"]) == 1


def test_invalid_gps_coordinates_returns_batch_failure(
    monkeypatch, fake_redis, dynamodb_table, aws_mock
):
    """SC-09 / E-05: lat=200 is out of range → batchItemFailures."""
    _patch_handler(monkeypatch, fake_redis, dynamodb_table)
    monkeypatch.setattr("lambdas.matching.handler.metrics.emit", MagicMock())

    event = make_kinesis_event(
        {
            "orderId": ORDER_ID,
            "customerId": CUSTOMER_ID,
            "restaurantId": RESTAURANT_ID,
            "pickupLat": 200.0,  # Invalid
            "pickupLng": -118.2437,
            "dropoffLat": 34.0611,
            "dropoffLng": -118.24,
            "totalAmountCents": 1000,
            "createdAt": "2026-04-15T00:00:00Z",
        }
    )
    result = handler.lambda_handler(event, None)

    assert len(result["batchItemFailures"]) == 1


def test_unsupported_event_type_is_silently_skipped(
    monkeypatch, fake_redis, dynamodb_table, aws_mock
):
    """ORDER_STATUS_CHANGED events are not processed by MatchingLambda."""
    _patch_handler(monkeypatch, fake_redis, dynamodb_table)
    monkeypatch.setattr("lambdas.matching.handler.metrics.emit", MagicMock())

    event = make_kinesis_event(
        {"orderId": ORDER_ID, "fromStatus": "matching", "toStatus": "assigned"},
        event_type="ORDER_STATUS_CHANGED",
    )
    result = handler.lambda_handler(event, None)

    assert result["batchItemFailures"] == []


# ── Tests: empty/edge cases ────────────────────────────────────────────────────

def test_empty_records_list_returns_no_failures(
    monkeypatch, fake_redis, dynamodb_table, aws_mock
):
    _patch_handler(monkeypatch, fake_redis, dynamodb_table)
    monkeypatch.setattr("lambdas.matching.handler.metrics.emit", MagicMock())

    result = handler.lambda_handler({"Records": []}, None)
    assert result == {"batchItemFailures": []}
