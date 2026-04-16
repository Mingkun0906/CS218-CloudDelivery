"""NotificationLambda — SQS-triggered push notification dispatcher.

Trigger  : SQS queue clouddelivery-notifications-{env}
           (fed by SNS topic clouddelivery-match-notifications-{env})
Batch    : up to 10 records per invocation (configured in template.yaml)
Owner    : Mingkun Liu

Processing steps (INTERFACE_SPEC.md §9.5):
1. Parse the SQS record body as an SNS notification envelope.
2. Extract the inner notification JSON from envelope["Message"].
3. Route to the appropriate handler based on notificationType.
4. Stub delivery: emit a structured CloudWatch log entry.
5. Emit notification.delivery_result metric.
6. On processing error: raise so SQS retries and eventually routes to DLQ.
   Do NOT catch-and-swallow errors here — let SQS retry/DLQ handle them.
   (INTERFACE_SPEC.md §9.5: "let it fail naturally on error")

SQS/SNS message structure (INTERFACE_SPEC.md §8):
  SQS record body = SNS notification JSON (string)
    └─ body["Message"] = our NotificationMessage JSON (string)

Notification types:
  COURIER_ASSIGNED      → notify courier (accept/reject prompt) + customer (ETA)
  ORDER_STATUS_UPDATE   → notify customer and/or restaurant of status change
  NO_COURIER_FOUND      → notify customer that order could not be matched

Delivery stub note:
  Actual push delivery (APNs, FCM, or SNS mobile push) is deferred until
  Cognito device tokens are available from the deployed environment.
  TODO (Zhuoqun): Wire device token lookup once Cognito user pool is deployed.
"""
import json
import os
from typing import List

from shared import logger, metrics
from shared.kinesis import get_sequence_number
from shared.models import (
    NOTIFICATION_TYPE_COURIER_ASSIGNED,
    NOTIFICATION_TYPE_NO_COURIER_FOUND,
    NOTIFICATION_TYPE_ORDER_STATUS_UPDATE,
    NotificationMessage,
)
from shared.tracing import get_trace_id, subsegment

ENV = os.environ.get("ENV", "dev")


# ── Entry point ───────────────────────────────────────────────────────────────

def lambda_handler(event: dict, context) -> dict:
    """Lambda entry point.

    Unlike Kinesis handlers this does NOT return batchItemFailures.
    For SQS, partial batch success is signalled by raising an exception for
    failed records; the Lambda runtime retries the entire batch. With
    ReportBatchItemFailures support (configured in template.yaml by Zhuoqun)
    this can be changed to return {"batchItemFailures": [...]}.

    TODO (Zhuoqun): Enable ReportBatchItemFailures in template.yaml SQS event
    configuration and change this handler to return batchItemFailures so only
    failed messages retry (not the whole batch).
    """
    trace_id = get_trace_id()
    logger.info("invocation_start", handler="notification", trace_id=trace_id)

    for record in event.get("Records", []):
        _process_sqs_record(record, trace_id)

    logger.info(
        "invocation_complete",
        handler="notification",
        total=len(event.get("Records", [])),
        trace_id=trace_id,
    )
    return {}


# ── SQS / SNS parsing ─────────────────────────────────────────────────────────

def _process_sqs_record(record: dict, trace_id: str) -> None:
    """Parse one SQS record and dispatch the notification.

    Raises on any error so SQS retries (and eventually sends to DLQ after
    maxReceiveCount=3 attempts; see INTERFACE_SPEC.md §8).
    """
    message_id = record.get("messageId", "unknown")

    # SQS body is the SNS notification JSON string.
    raw_body = record.get("body", "")
    try:
        sns_envelope = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        logger.error(
            "invalid_sqs_body_json",
            message_id=message_id,
            error=str(exc),
            trace_id=trace_id,
        )
        raise ValueError(f"SQS body is not valid JSON: {exc}") from exc

    # SNS wraps our message in envelope["Message"] (a JSON string).
    raw_message = sns_envelope.get("Message", "")
    if not raw_message:
        logger.error(
            "missing_sns_message_field",
            message_id=message_id,
            trace_id=trace_id,
        )
        raise ValueError("SNS envelope is missing 'Message' field")

    try:
        notification_dict = json.loads(raw_message)
    except json.JSONDecodeError as exc:
        logger.error(
            "invalid_notification_json",
            message_id=message_id,
            error=str(exc),
            trace_id=trace_id,
        )
        raise ValueError(f"SNS Message field is not valid JSON: {exc}") from exc

    try:
        notification = NotificationMessage.from_dict(notification_dict)
    except (KeyError, TypeError) as exc:
        logger.error(
            "malformed_notification_fields",
            message_id=message_id,
            error=str(exc),
            trace_id=trace_id,
        )
        raise ValueError(f"Notification message is missing required fields: {exc}") from exc

    _dispatch_notification(notification, message_id, trace_id)


def _dispatch_notification(
    notification: NotificationMessage,
    message_id: str,
    trace_id: str,
) -> None:
    """Route notification to the appropriate type handler."""
    ntype = notification.notification_type

    with subsegment("sns.publish_notification") as seg:
        seg.put_metadata("notification_type", ntype)
        seg.put_metadata("order_id", notification.data.get("orderId", ""))

        try:
            if ntype == NOTIFICATION_TYPE_COURIER_ASSIGNED:
                _handle_courier_assigned(notification, trace_id)
            elif ntype == NOTIFICATION_TYPE_ORDER_STATUS_UPDATE:
                _handle_order_status_update(notification, trace_id)
            elif ntype == NOTIFICATION_TYPE_NO_COURIER_FOUND:
                _handle_no_courier_found(notification, trace_id)
            else:
                logger.warn(
                    "unknown_notification_type",
                    notification_type=ntype,
                    message_id=message_id,
                    trace_id=trace_id,
                )
                # Unknown types are acknowledged (not re-queued) to prevent DLQ flood.
                metrics.emit(
                    "notification.delivery_result",
                    1,
                    dimensions={"Result": "unknown_type"},
                )
                return

            metrics.emit(
                "notification.delivery_result", 1, dimensions={"Result": "success"}
            )

        except Exception as exc:
            metrics.emit(
                "notification.delivery_result", 1, dimensions={"Result": "failure"}
            )
            logger.error(
                "notification_dispatch_failed",
                notification_type=ntype,
                error=str(exc),
                trace_id=trace_id,
            )
            raise


# ── Notification type handlers ────────────────────────────────────────────────

def _handle_courier_assigned(notification: NotificationMessage, trace_id: str) -> None:
    """Notify the assigned courier and the customer.

    Stub: logs the delivery intent. Real push requires device tokens from Cognito.
    TODO (Zhuoqun): Replace with APNs/FCM call once device tokens are available.
    """
    order_id = notification.data.get("orderId", "")
    courier_id = notification.recipients.get("courierId", "")
    customer_id = notification.recipients.get("customerId", "")

    # Stub: log what would be pushed.
    logger.info(
        "push_stub_courier_assigned",
        notification_type=NOTIFICATION_TYPE_COURIER_ASSIGNED,
        order_id=order_id,
        courier_push_target=courier_id,
        customer_push_target=customer_id,
        message="New order assignment — tap to accept or reject.",
        trace_id=trace_id,
    )


def _handle_order_status_update(notification: NotificationMessage, trace_id: str) -> None:
    """Notify customer and/or restaurant of an order status change.

    Stub: logs the delivery intent.
    TODO (Zhuoqun): Replace with APNs/FCM call once device tokens are available.
    """
    order_id = notification.data.get("orderId", "")
    new_status = notification.data.get("status", "")
    customer_id = notification.recipients.get("customerId", "")

    logger.info(
        "push_stub_order_status_update",
        notification_type=NOTIFICATION_TYPE_ORDER_STATUS_UPDATE,
        order_id=order_id,
        new_status=new_status,
        customer_push_target=customer_id,
        message=f"Your order is now: {new_status}.",
        trace_id=trace_id,
    )


def _handle_no_courier_found(notification: NotificationMessage, trace_id: str) -> None:
    """Notify the customer that no courier could be found.

    Stub: logs the delivery intent.
    TODO (Zhuoqun): Replace with APNs/FCM call once device tokens are available.
    """
    order_id = notification.data.get("orderId", "")
    customer_id = notification.recipients.get("customerId", "")

    logger.info(
        "push_stub_no_courier_found",
        notification_type=NOTIFICATION_TYPE_NO_COURIER_FOUND,
        order_id=order_id,
        customer_push_target=customer_id,
        message="We could not find a courier for your order. Please try again.",
        trace_id=trace_id,
    )
