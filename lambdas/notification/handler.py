import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))

import logger
import tracing

ENV = os.environ.get("ENV", "dev")


def lambda_handler(event, context):
    trace_id = tracing.get_trace_id()
    logger.info("notification_invocation_start", trace_id=trace_id,
                record_count=len(event.get("Records", [])))

    failed_items = []

    for record in event.get("Records", []):
        msg_id = record["messageId"]
        try:
            # SQS wraps SNS message — unwrap both layers
            outer = json.loads(record["body"])
            message = json.loads(outer.get("Message", outer.get("body", "{}")))
            _process_notification(message, trace_id)
        except Exception as e:
            logger.error("notification_failed", message_id=msg_id,
                         error=str(e), trace_id=trace_id)
            failed_items.append({"itemIdentifier": msg_id})

    return {"batchItemFailures": failed_items}


def _process_notification(message: dict, trace_id: str):
    notification_type = message.get("notificationType")
    logger.info("processing_notification", notification_type=notification_type,
                trace_id=trace_id)
    # TODO: route to courier / customer / restaurant based on notificationType
