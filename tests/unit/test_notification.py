"""Unit tests for NotificationLambda (lambdas/notification/handler.py).

Tests use:
- monkeypatch to suppress metrics.emit CloudWatch calls
- No Redis or DynamoDB needed (notification handler is stateless stubs)
"""
import json
from unittest.mock import MagicMock

import pytest

from tests.helpers import make_sqs_event

import lambdas.notification.handler as handler


# ── Helpers ───────────────────────────────────────────────────────────────────

def _courier_assigned_msg(order_id="ord_TEST", courier_id="cur_TEST", customer_id="cus_TEST"):
    return {
        "notificationType": "COURIER_ASSIGNED",
        "eventId": "evt_TEST_NOTIF_0001",
        "timestamp": "2026-04-15T00:00:03Z",
        "traceId": "1-00000000-000000000000000000000000",
        "recipients": {
            "courierId": courier_id,
            "customerId": customer_id,
            "restaurantId": "rst_TEST",
        },
        "data": {"orderId": order_id, "status": "assigned"},
    }


def _patch_metrics(monkeypatch):
    monkeypatch.setattr("lambdas.notification.handler.metrics.emit", MagicMock())


# ── Tests: happy path ──────────────────────────────────────────────────────────

def test_courier_assigned_dispatches_without_error(monkeypatch):
    """COURIER_ASSIGNED notification is routed and logged without raising."""
    _patch_metrics(monkeypatch)

    event = make_sqs_event(_courier_assigned_msg())
    result = handler.lambda_handler(event, None)

    assert result == {}


def test_order_status_update_dispatches_without_error(monkeypatch):
    """ORDER_STATUS_UPDATE notification is routed and logged without raising."""
    _patch_metrics(monkeypatch)

    msg = {
        "notificationType": "ORDER_STATUS_UPDATE",
        "eventId": "evt_TEST_STATUS_0001",
        "timestamp": "2026-04-15T00:05:00Z",
        "traceId": "unknown",
        "recipients": {"customerId": "cus_TEST", "courierId": "cur_TEST", "restaurantId": None},
        "data": {"orderId": "ord_TEST", "status": "preparing"},
    }
    event = make_sqs_event(msg)
    result = handler.lambda_handler(event, None)

    assert result == {}


def test_no_courier_found_dispatches_without_error(monkeypatch):
    """NO_COURIER_FOUND notification is routed and logged without raising."""
    _patch_metrics(monkeypatch)

    msg = {
        "notificationType": "NO_COURIER_FOUND",
        "eventId": "evt_TEST_NOCOURIER_0001",
        "timestamp": "2026-04-15T00:01:00Z",
        "traceId": "unknown",
        "recipients": {"customerId": "cus_TEST", "courierId": None, "restaurantId": None},
        "data": {"orderId": "ord_TEST"},
    }
    event = make_sqs_event(msg)
    result = handler.lambda_handler(event, None)

    assert result == {}


def test_unknown_notification_type_is_acknowledged_not_retried(monkeypatch):
    """Unknown notificationType is logged and NOT raised (avoids DLQ flood)."""
    _patch_metrics(monkeypatch)

    msg = {
        "notificationType": "FUTURE_UNKNOWN_TYPE",
        "eventId": "evt_UNKNOWN",
        "timestamp": "2026-04-15T00:00:00Z",
        "traceId": "unknown",
        "recipients": {},
        "data": {},
    }
    event = make_sqs_event(msg)
    # Should not raise
    result = handler.lambda_handler(event, None)
    assert result == {}


def test_batch_of_multiple_sqs_records(monkeypatch):
    """Batch of 3 valid notifications all succeed."""
    _patch_metrics(monkeypatch)

    import json

    msgs = [
        _courier_assigned_msg(order_id=f"ord_TEST_{i}") for i in range(3)
    ]

    records = []
    for i, msg in enumerate(msgs):
        sns_envelope = {
            "Type": "Notification",
            "MessageId": f"msg-{i}",
            "TopicArn": "arn:aws:sns:us-east-1:123456789012:test-topic",
            "Message": json.dumps(msg),
        }
        records.append(
            {
                "messageId": f"sqs-{i}",
                "receiptHandle": f"handle-{i}",
                "body": json.dumps(sns_envelope),
                "attributes": {},
                "messageAttributes": {},
                "md5OfBody": "test",
                "eventSource": "aws:sqs",
                "eventSourceARN": "arn:aws:sqs:us-east-1:123456789012:test-queue",
                "awsRegion": "us-east-1",
            }
        )

    result = handler.lambda_handler({"Records": records}, None)
    assert result == {}


# ── Tests: malformed / error paths ────────────────────────────────────────────

def test_malformed_sqs_body_json_raises(monkeypatch):
    """Non-JSON SQS body raises ValueError (triggers SQS retry → DLQ)."""
    _patch_metrics(monkeypatch)

    bad_event = {
        "Records": [
            {
                "messageId": "bad-msg",
                "receiptHandle": "handle",
                "body": "this is not json {{{{",
                "attributes": {},
                "messageAttributes": {},
                "md5OfBody": "test",
                "eventSource": "aws:sqs",
                "eventSourceARN": "arn:aws:sqs:us-east-1:123456789012:test-queue",
                "awsRegion": "us-east-1",
            }
        ]
    }
    with pytest.raises(ValueError, match="not valid JSON"):
        handler.lambda_handler(bad_event, None)


def test_missing_sns_message_field_raises(monkeypatch):
    """SNS envelope without 'Message' field raises ValueError."""
    _patch_metrics(monkeypatch)

    import json

    bad_envelope = {"Type": "Notification", "MessageId": "x"}  # no Message key
    bad_event = {
        "Records": [
            {
                "messageId": "bad-msg",
                "receiptHandle": "handle",
                "body": json.dumps(bad_envelope),
                "attributes": {},
                "messageAttributes": {},
                "md5OfBody": "test",
                "eventSource": "aws:sqs",
                "eventSourceARN": "arn:aws:sqs:us-east-1:123456789012:test-queue",
                "awsRegion": "us-east-1",
            }
        ]
    }
    with pytest.raises(ValueError, match="missing 'Message' field"):
        handler.lambda_handler(bad_event, None)


def test_malformed_notification_json_inside_message_raises(monkeypatch):
    """SNS Message field containing invalid JSON raises ValueError."""
    _patch_metrics(monkeypatch)

    import json

    bad_envelope = {
        "Type": "Notification",
        "MessageId": "x",
        "Message": "not json {{{",
    }
    bad_event = {
        "Records": [
            {
                "messageId": "bad-msg",
                "receiptHandle": "handle",
                "body": json.dumps(bad_envelope),
                "attributes": {},
                "messageAttributes": {},
                "md5OfBody": "test",
                "eventSource": "aws:sqs",
                "eventSourceARN": "arn:aws:sqs:us-east-1:123456789012:test-queue",
                "awsRegion": "us-east-1",
            }
        ]
    }
    with pytest.raises(ValueError, match="not valid JSON"):
        handler.lambda_handler(bad_event, None)


def test_missing_notification_type_field_raises(monkeypatch):
    """Notification dict without notificationType raises ValueError."""
    _patch_metrics(monkeypatch)

    msg = {
        # "notificationType" intentionally omitted
        "eventId": "evt_TEST",
        "timestamp": "2026-04-15T00:00:00Z",
        "traceId": "unknown",
        "recipients": {},
        "data": {},
    }
    event = make_sqs_event(msg)

    with pytest.raises(ValueError, match="missing required fields"):
        handler.lambda_handler(event, None)
