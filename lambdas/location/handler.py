"""LocationLambda — Kinesis-triggered courier GPS ingestion.

Trigger  : Kinesis stream clouddelivery-locations-{env}
Batch    : up to 10 records per invocation (configured in template.yaml)
Owner    : Mingkun Liu

Processing steps (INTERFACE_SPEC.md §9.4):
1. Decode Kinesis record → EventEnvelope
2. Validate LOCATION_UPDATE payload (GPS range check)
3. Redis GEOADD couriers:locations <lng> <lat> <courierId>
4. Redis SET courier:ttl:<courierId> "" EX LOCATION_TTL_SECONDS
5. Emit metrics and structured logs
6. Return {"batchItemFailures": [...]} for Kinesis partial-batch retry support.

Redis schema (INTERFACE_SPEC.md §7):
  GEO set key : couriers:locations
  TTL sentinel: courier:ttl:<courierId>  (value="", TTL=30s)

TTL / ZREM note:
  When a courier:ttl key expires, the courier should be removed from
  couriers:locations via ZREM. This requires Redis keyspace expiry notifications
  delivered to a subscriber. Implementing a subscriber inside this Lambda is
  not feasible (Lambda has no persistent connection). Options:
  TODO (Zhuoqun): Enable notify-keyspace-events = "Ex" on the ElastiCache
  parameter group and wire the expiry event to a cleanup Lambda (or to this
  Lambda via an additional SQS/SNS trigger). Until then, MatchingLambda
  defensively checks the TTL sentinel before assigning a courier.
"""
import os
import time
from datetime import datetime, timezone
from typing import List

from shared import logger, metrics
from shared.cache import get_client as _get_redis_client
from shared.kinesis import decode_kinesis_record, get_sequence_number
from shared.models import EVENT_TYPE_LOCATION_UPDATE
from shared.tracing import get_trace_id, subsegment
from shared.validation import validate_location_update_payload

# ── Module-level init ─────────────────────────────────────────────────────────

redis_client = _get_redis_client()

ENV = os.environ.get("ENV", "dev")
LOCATION_TTL_SECONDS = int(os.environ.get("LOCATION_TTL_SECONDS", "30"))

GEO_KEY = "couriers:locations"
COURIER_TTL_KEY_PREFIX = "courier:ttl:"

# Sample 1-in-10 for the active_couriers gauge to avoid per-record overhead.
_ACTIVE_COURIER_SAMPLE_RATE = 10


# ── Entry point ───────────────────────────────────────────────────────────────

def lambda_handler(event: dict, context) -> dict:
    """Lambda entry point. Returns partial batch failure list."""
    trace_id = get_trace_id()
    logger.info("invocation_start", handler="location", trace_id=trace_id)

    failed_items: List[dict] = []
    success_count = 0

    for i, record in enumerate(event.get("Records", [])):
        seq = get_sequence_number(record)
        try:
            _process_record(record, trace_id)
            success_count += 1
            # Emit active courier count on a sampled basis to reduce metric noise.
            if i % _ACTIVE_COURIER_SAMPLE_RATE == 0:
                _emit_active_courier_count()
        except Exception as exc:
            logger.error(
                "record_processing_failed",
                error=str(exc),
                error_type=type(exc).__name__,
                sequence_number=seq,
                trace_id=trace_id,
            )
            failed_items.append({"itemIdentifier": seq})

    if success_count:
        metrics.emit("location.update_count", success_count)

    logger.info(
        "invocation_complete",
        handler="location",
        total=len(event.get("Records", [])),
        succeeded=success_count,
        failed=len(failed_items),
        trace_id=trace_id,
    )
    return {"batchItemFailures": failed_items}


# ── Record processing ──────────────────────────────────────────────────────────

def _process_record(record: dict, trace_id: str) -> None:
    start_ms = time.monotonic() * 1000

    envelope = decode_kinesis_record(record)

    event_type = envelope.get("eventType")
    if event_type != EVENT_TYPE_LOCATION_UPDATE:
        logger.info(
            "skipping_unsupported_event_type",
            event_type=event_type,
            trace_id=trace_id,
        )
        return

    payload = envelope.get("payload", {})
    validate_location_update_payload(payload)

    courier_id: str = payload["courierId"]
    lat: float = float(payload["lat"])
    lng: float = float(payload["lng"])

    _ingest_location(courier_id, lat, lng, trace_id)

    duration_ms = time.monotonic() * 1000 - start_ms
    metrics.emit("location.update_duration_ms", duration_ms, unit="Milliseconds")


def _ingest_location(courier_id: str, lat: float, lng: float, trace_id: str) -> None:
    """Write courier position to Redis geo-index and reset TTL sentinel."""
    # GEOADD — overwrites any previous position for this courier.
    with subsegment("redis.geoadd") as seg:
        seg.put_metadata("courier_id", courier_id)
        seg.put_metadata("lat", lat)
        seg.put_metadata("lng", lng)

        redis_client.geoadd(GEO_KEY, [lng, lat, courier_id])

    # SET TTL sentinel — reset 30s window on every update.
    ttl_key = f"{COURIER_TTL_KEY_PREFIX}{courier_id}"
    with subsegment("redis.set_ttl") as seg:
        seg.put_metadata("ttl_key", ttl_key)
        seg.put_metadata("ttl_seconds", LOCATION_TTL_SECONDS)

        redis_client.set(ttl_key, "", ex=LOCATION_TTL_SECONDS)

    logger.info(
        "location_ingested",
        courier_id=courier_id,
        lat=lat,
        lng=lng,
        ttl=LOCATION_TTL_SECONDS,
        trace_id=trace_id,
    )


def _emit_active_courier_count() -> None:
    """Emit an approximate count of couriers currently in the geo-index."""
    try:
        count = redis_client.zcard(GEO_KEY)
        metrics.emit("location.active_couriers", count)
    except Exception:
        pass  # Never fail a batch due to a metrics side-effect.


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
