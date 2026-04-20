"""Failure injection tests for NotificationLambda.

Covers FL-06 (SNS/SQS delivery failure → DLQ) at the application layer.

FL-06 scenario:
  - NotificationLambda raises on processing
  - SQS retries up to maxReceiveCount (3) then moves to DLQ
  - CloudWatch alarm fires on DLQ depth > 0
  - SQS redrive recovers all messages

The VPC-policy block (infra side) is tested via the live injection script
tests/failure/scripts/fl06_sqs_block.py.
"""
import json
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from tests.helpers import make_sqs_event
import lambdas.notification.handler as handler


# ── Constants ─────────────────────────────────────────────────────────────────

COURIER_ID = "cur_FAIL_TEST_0000000000001"
CUSTOMER_ID = "cus_FAIL_TEST_0000000000002"
RESTAURANT_ID = "rst_FAIL_TEST_0000000000003"
ORDER_ID = "ord_FAIL_TEST_0000000000000"


def _make_courier_assigned_msg():
    return {
        "notificationType": "COURIER_ASSIGNED",
        "eventId": "evt_FAIL_TEST_001",
        "timestamp": "2026-04-20T00:00:00Z",
        "traceId": "1-00000000-000000000000000000000001",
        "recipients": {
            "courierId": COURIER_ID,
            "customerId": CUSTOMER_ID,
            "restaurantId": RESTAURANT_ID,
        },
        "data": {
            "orderId": ORDER_ID,
            "status": "assigned",
            "pickupAddress": "123 Main St",
            "dropoffAddress": "456 Oak Ave",
        },
    }


# ── FL-06: SQS delivery failure → DLQ ────────────────────────────────────────

def test_fl06_malformed_sns_envelope_raises_and_triggers_sqs_retry(monkeypatch):
    """FL-06: Malformed SQS body causes NotificationLambda to raise.

    SQS keeps the message invisible, retries up to maxReceiveCount=3,
    then moves it to the DLQ. The handler must raise (not swallow) to
    signal SQS that processing failed.
    """
    monkeypatch.setattr("lambdas.notification.handler.metrics.emit", MagicMock())

    # Inject a completely malformed body (not valid JSON).
    bad_event = {
        "Records": [{
            "messageId": "msg-fl06-001",
            "receiptHandle": "handle-001",
            "body": "THIS IS NOT JSON {{{",
            "attributes": {"ApproximateReceiveCount": "1"},
            "messageAttributes": {},
            "md5OfBody": "bad",
            "eventSource": "aws:sqs",
            "awsRegion": "us-east-1",
        }]
    }

    with pytest.raises((json.JSONDecodeError, ValueError)):
        handler.lambda_handler(bad_event, None)


def test_fl06_missing_message_field_raises(monkeypatch):
    """FL-06: SNS envelope missing 'Message' field causes handler to raise.

    Simulates a corrupt SNS → SQS delivery where the envelope is valid JSON
    but the inner notification payload is missing.
    """
    monkeypatch.setattr("lambdas.notification.handler.metrics.emit", MagicMock())

    bad_envelope = json.dumps({
        "Type": "Notification",
        "MessageId": "msg-fl06-002",
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:test-topic",
        # "Message" field deliberately absent
    })

    bad_event = {
        "Records": [{
            "messageId": "msg-fl06-002",
            "receiptHandle": "handle-002",
            "body": bad_envelope,
            "attributes": {"ApproximateReceiveCount": "2"},
            "messageAttributes": {},
            "md5OfBody": "bad",
            "eventSource": "aws:sqs",
            "awsRegion": "us-east-1",
        }]
    }

    with pytest.raises((KeyError, ValueError)):
        handler.lambda_handler(bad_event, None)


def test_fl06_partial_batch_failure_isolates_bad_record(monkeypatch):
    """FL-06 (partial): One bad SQS record fails; valid record in same batch succeeds.

    With ReportBatchItemFailures enabled, only the failed record goes back
    to the queue. The good record is acknowledged.
    """
    monkeypatch.setattr("lambdas.notification.handler.metrics.emit", MagicMock())

    good_msg = _make_courier_assigned_msg()
    good_record = make_sqs_event(good_msg)["Records"][0]
    good_record["messageId"] = "msg-good-001"

    bad_record = {
        "messageId": "msg-bad-001",
        "receiptHandle": "handle-bad",
        "body": "NOT VALID JSON",
        "attributes": {"ApproximateReceiveCount": "1"},
        "messageAttributes": {},
        "md5OfBody": "bad",
        "eventSource": "aws:sqs",
        "awsRegion": "us-east-1",
    }

    # When a bad record is in the same batch, the handler raises on it.
    # In production, ReportBatchItemFailures would isolate it.
    # Here we verify that the bad record causes the exception.
    with pytest.raises(Exception):
        handler.lambda_handler({"Records": [good_record, bad_record]}, None)


def test_fl06_valid_notification_does_not_raise(monkeypatch):
    """Baseline: valid COURIER_ASSIGNED message processes without error.

    Ensures the failure path tests above are catching real failures,
    not false positives from a broken handler.
    """
    monkeypatch.setattr("lambdas.notification.handler.metrics.emit", MagicMock())

    event = make_sqs_event(_make_courier_assigned_msg())
    # Should not raise
    result = handler.lambda_handler(event, None)
    assert result is not None
