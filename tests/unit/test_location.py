"""Unit tests for LocationLambda (lambdas/location/handler.py).

Tests use:
- fakeredis for Redis geo operations (no real Redis needed)
- monkeypatch to replace module-level clients in the handler
"""
from unittest.mock import MagicMock

import pytest

from tests.helpers import make_kinesis_event

import lambdas.location.handler as handler


# ── Constants ─────────────────────────────────────────────────────────────────

COURIER_ID = "cur_01HVTEST0000000000000000010"
LAT = 34.0505
LNG = -118.2420


def _patch_handler(monkeypatch, fake_redis):
    monkeypatch.setattr(handler, "redis_client", fake_redis)
    monkeypatch.setattr("lambdas.location.handler.metrics.emit", MagicMock())


# ── Tests: happy path ──────────────────────────────────────────────────────────

def test_location_update_writes_geo_and_ttl(monkeypatch, fake_redis):
    """F-02: LOCATION_UPDATE causes GEOADD + TTL sentinel set."""
    _patch_handler(monkeypatch, fake_redis)

    event = make_kinesis_event(
        {
            "courierId": COURIER_ID,
            "lat": LAT,
            "lng": LNG,
            "timestamp": "2026-04-15T00:00:00Z",
        },
        event_type="LOCATION_UPDATE",
    )
    result = handler.lambda_handler(event, None)

    assert result["batchItemFailures"] == []

    # Courier must be in the geo-index
    positions = fake_redis.geopos("couriers:locations", COURIER_ID)
    assert positions[0] is not None
    stored_lng, stored_lat = positions[0]
    assert abs(stored_lat - LAT) < 0.001
    assert abs(stored_lng - LNG) < 0.001

    # TTL sentinel must exist
    assert fake_redis.exists(f"courier:ttl:{COURIER_ID}") == 1

    # TTL should be close to LOCATION_TTL_SECONDS (30s)
    ttl = fake_redis.ttl(f"courier:ttl:{COURIER_ID}")
    assert 0 < ttl <= 30


def test_location_update_overwrites_previous_position(monkeypatch, fake_redis):
    """F-02: Second location update overwrites the first in the geo-index."""
    _patch_handler(monkeypatch, fake_redis)

    first_lat, first_lng = 34.0000, -118.0000
    second_lat, second_lng = 34.0505, -118.2420

    for lat, lng in [(first_lat, first_lng), (second_lat, second_lng)]:
        event = make_kinesis_event(
            {"courierId": COURIER_ID, "lat": lat, "lng": lng, "timestamp": "2026-04-15T00:00:00Z"},
            event_type="LOCATION_UPDATE",
        )
        handler.lambda_handler(event, None)

    positions = fake_redis.geopos("couriers:locations", COURIER_ID)
    stored_lng, stored_lat = positions[0]
    assert abs(stored_lat - second_lat) < 0.001


def test_location_batch_of_multiple_records(monkeypatch, fake_redis):
    """S-06 / E-11: Batch of 10 location updates all succeed."""
    _patch_handler(monkeypatch, fake_redis)

    import base64, json

    records = []
    for i in range(10):
        cid = f"cur_TEST_{i:04d}"
        payload = {
            "courierId": cid,
            "lat": 34.0 + i * 0.001,
            "lng": -118.2 + i * 0.001,
            "timestamp": "2026-04-15T00:00:00Z",
        }
        envelope = {
            "eventId": f"evt_TEST_{i:04d}",
            "eventType": "LOCATION_UPDATE",
            "schemaVersion": "1.0",
            "timestamp": "2026-04-15T00:00:00Z",
            "traceId": "unknown",
            "payload": payload,
        }
        data_b64 = base64.b64encode(json.dumps(envelope).encode()).decode()
        records.append(
            {
                "kinesis": {
                    "sequenceNumber": f"seq-{i:04d}",
                    "data": data_b64,
                    "partitionKey": cid,
                },
                "eventSource": "aws:kinesis",
            }
        )

    result = handler.lambda_handler({"Records": records}, None)

    assert result["batchItemFailures"] == []
    assert fake_redis.zcard("couriers:locations") == 10


# ── Tests: invalid input ───────────────────────────────────────────────────────

def test_invalid_gps_lat_returns_batch_failure(monkeypatch, fake_redis):
    """E-05: lat=200 is out of range → batchItemFailures."""
    _patch_handler(monkeypatch, fake_redis)

    event = make_kinesis_event(
        {"courierId": COURIER_ID, "lat": 200.0, "lng": LNG, "timestamp": "2026-04-15T00:00:00Z"},
        event_type="LOCATION_UPDATE",
    )
    result = handler.lambda_handler(event, None)

    assert len(result["batchItemFailures"]) == 1
    # Nothing should be written to Redis
    assert fake_redis.zcard("couriers:locations") == 0


def test_missing_courier_id_returns_batch_failure(monkeypatch, fake_redis):
    """Missing courierId → batchItemFailures."""
    _patch_handler(monkeypatch, fake_redis)

    event = make_kinesis_event(
        {"lat": LAT, "lng": LNG, "timestamp": "2026-04-15T00:00:00Z"},
        event_type="LOCATION_UPDATE",
    )
    result = handler.lambda_handler(event, None)

    assert len(result["batchItemFailures"]) == 1


def test_malformed_base64_returns_batch_failure(monkeypatch, fake_redis):
    """Corrupt Kinesis data → batchItemFailures."""
    _patch_handler(monkeypatch, fake_redis)

    bad_event = {
        "Records": [
            {
                "kinesis": {
                    "sequenceNumber": "bad-seq-001",
                    "data": "@@not_base64@@",
                    "partitionKey": "test",
                },
                "eventSource": "aws:kinesis",
            }
        ]
    }
    result = handler.lambda_handler(bad_event, None)

    assert len(result["batchItemFailures"]) == 1


def test_unsupported_event_type_silently_skipped(monkeypatch, fake_redis):
    """Non-LOCATION_UPDATE events are ignored without failures."""
    _patch_handler(monkeypatch, fake_redis)

    event = make_kinesis_event(
        {"orderId": "ord_TEST", "status": "placed"},
        event_type="ORDER_PLACED",
    )
    result = handler.lambda_handler(event, None)

    assert result["batchItemFailures"] == []
    assert fake_redis.zcard("couriers:locations") == 0


def test_partial_batch_failure_continues_rest(monkeypatch, fake_redis):
    """One bad record in a batch of 3 — only the bad one fails; others succeed."""
    _patch_handler(monkeypatch, fake_redis)

    import base64, json

    good_envelope = lambda cid: {
        "eventId": "evt_GOOD",
        "eventType": "LOCATION_UPDATE",
        "schemaVersion": "1.0",
        "timestamp": "2026-04-15T00:00:00Z",
        "traceId": "unknown",
        "payload": {"courierId": cid, "lat": 34.05, "lng": -118.24, "timestamp": "2026-04-15T00:00:00Z"},
    }

    records = [
        {
            "kinesis": {
                "sequenceNumber": "seq-good-1",
                "data": base64.b64encode(json.dumps(good_envelope("cur_GOOD_1")).encode()).decode(),
                "partitionKey": "cur_GOOD_1",
            },
            "eventSource": "aws:kinesis",
        },
        {
            "kinesis": {
                "sequenceNumber": "seq-bad",
                "data": "not_valid_base64!",
                "partitionKey": "bad",
            },
            "eventSource": "aws:kinesis",
        },
        {
            "kinesis": {
                "sequenceNumber": "seq-good-2",
                "data": base64.b64encode(json.dumps(good_envelope("cur_GOOD_2")).encode()).decode(),
                "partitionKey": "cur_GOOD_2",
            },
            "eventSource": "aws:kinesis",
        },
    ]

    result = handler.lambda_handler({"Records": records}, None)

    assert len(result["batchItemFailures"]) == 1
    assert result["batchItemFailures"][0]["itemIdentifier"] == "seq-bad"
    assert fake_redis.zcard("couriers:locations") == 2
