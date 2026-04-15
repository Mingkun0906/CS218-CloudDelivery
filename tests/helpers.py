"""Shared test helper functions for building Lambda event payloads.

These are plain functions (not pytest fixtures) so they can be imported from
any test file including tests/failure/*.
"""
import base64
import json


def make_kinesis_event(payload: dict, event_type: str = "ORDER_PLACED") -> dict:
    """Build a minimal Kinesis Lambda event wrapping an EventEnvelope."""
    envelope = {
        "eventId": "evt_TEST00000000000000000001",
        "eventType": event_type,
        "schemaVersion": "1.0",
        "timestamp": "2026-04-15T00:00:00Z",
        "traceId": "1-00000000-000000000000000000000000",
        "payload": payload,
    }
    data_b64 = base64.b64encode(json.dumps(envelope).encode()).decode()
    return {
        "Records": [
            {
                "kinesis": {
                    "kinesisSchemaVersion": "1.0",
                    "partitionKey": "test-key",
                    "sequenceNumber": "49590338271490256608559692540961693206152755161246945282",
                    "data": data_b64,
                    "approximateArrivalTimestamp": 1744675200.0,
                },
                "eventSource": "aws:kinesis",
                "eventVersion": "1.0",
                "eventID": "shardId-000000000000:49590338271490256608559692540961693206152755161246945282",
                "eventName": "aws:kinesis:record",
                "invokeIdentityArn": "arn:aws:iam::123456789012:role/test-role",
                "awsRegion": "us-east-1",
                "eventSourceARN": "arn:aws:kinesis:us-east-1:123456789012:stream/clouddelivery-orders-test",
            }
        ]
    }


def make_sqs_event(notification_message: dict) -> dict:
    """Build a minimal SQS Lambda event wrapping an SNS notification."""
    sns_envelope = {
        "Type": "Notification",
        "MessageId": "test-message-id",
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:clouddelivery-match-notifications-test",
        "Message": json.dumps(notification_message),
        "Timestamp": "2026-04-15T00:00:03Z",
    }
    return {
        "Records": [
            {
                "messageId": "test-sqs-message-id",
                "receiptHandle": "test-receipt-handle",
                "body": json.dumps(sns_envelope),
                "attributes": {"ApproximateReceiveCount": "1"},
                "messageAttributes": {},
                "md5OfBody": "test",
                "eventSource": "aws:sqs",
                "eventSourceARN": "arn:aws:sqs:us-east-1:123456789012:clouddelivery-notifications-test",
                "awsRegion": "us-east-1",
            }
        ]
    }
