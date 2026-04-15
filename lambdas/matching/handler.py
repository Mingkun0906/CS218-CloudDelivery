import base64
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))

import logger
import tracing
from db import get_table
from cache import get_client

# Module-level — reused across warm invocations
table = get_table()
redis_client = get_client()

SEARCH_RADIUS_KM = float(os.environ.get("SEARCH_RADIUS_KM", "5"))
MAX_COURIER_CANDIDATES = int(os.environ.get("MAX_COURIER_CANDIDATES", "10"))
MATCH_TOPIC_ARN = os.environ.get("MATCH_TOPIC_ARN", "")


def lambda_handler(event, context):
    trace_id = tracing.get_trace_id()
    logger.info("matching_invocation_start", trace_id=trace_id,
                record_count=len(event.get("Records", [])))

    failed_items = []

    for record in event.get("Records", []):
        seq = record["kinesis"]["sequenceNumber"]
        try:
            payload = json.loads(base64.b64decode(record["kinesis"]["data"]))
            _process_order_event(payload, trace_id)
        except Exception as e:
            logger.error("record_failed", sequence_number=seq,
                         error=str(e), trace_id=trace_id)
            failed_items.append({"itemIdentifier": seq})

    return {"batchItemFailures": failed_items}


def _process_order_event(envelope: dict, trace_id: str):
    event_type = envelope.get("eventType")
    if event_type != "ORDER_PLACED":
        logger.info("skipping_event", event_type=event_type, trace_id=trace_id)
        return

    payload = envelope["payload"]
    order_id = payload["orderId"]
    pickup_lng = payload["pickupLng"]
    pickup_lat = payload["pickupLat"]

    logger.info("matching_start", order_id=order_id, trace_id=trace_id)

    # TODO: implement geo-matching logic
    # 1. GEORADIUS query on Redis
    # 2. Conditional DynamoDB write for each candidate
    # 3. SNS publish on success
