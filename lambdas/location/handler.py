import base64
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))

import logger
import tracing
from cache import get_client

# Module-level — reused across warm invocations
redis_client = get_client()

LOCATION_TTL_SECONDS = int(os.environ.get("LOCATION_TTL_SECONDS", "30"))
GEO_KEY = "couriers:locations"


def lambda_handler(event, context):
    trace_id = tracing.get_trace_id()
    logger.info("location_invocation_start", trace_id=trace_id,
                record_count=len(event.get("Records", [])))

    failed_items = []

    for record in event.get("Records", []):
        seq = record["kinesis"]["sequenceNumber"]
        try:
            payload = json.loads(base64.b64decode(record["kinesis"]["data"]))
            _process_location_event(payload, trace_id)
        except Exception as e:
            logger.error("record_failed", sequence_number=seq,
                         error=str(e), trace_id=trace_id)
            failed_items.append({"itemIdentifier": seq})

    return {"batchItemFailures": failed_items}


def _process_location_event(envelope: dict, trace_id: str):
    event_type = envelope.get("eventType")
    if event_type != "LOCATION_UPDATE":
        return

    payload = envelope["payload"]
    courier_id = payload["courierId"]
    lat = payload["lat"]
    lng = payload["lng"]

    # TODO: implement GEOADD + TTL sentinel
    # 1. redis_client.geoadd(GEO_KEY, [lng, lat, courier_id])
    # 2. redis_client.set(f"courier:ttl:{courier_id}", "", ex=LOCATION_TTL_SECONDS)
    logger.info("location_update_received", courier_id=courier_id,
                lat=lat, lng=lng, trace_id=trace_id)
