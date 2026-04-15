"""Unit tests for shared/validation.py."""
import pytest

from shared.validation import (
    require_fields,
    validate_gps_coords,
    validate_location_update_payload,
    validate_order_placed_payload,
    validate_payload_size,
)


# ── GPS validation ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("lat,lng", [
    (0, 0),
    (90, 180),
    (-90, -180),
    (34.0522, -118.2437),
])
def test_valid_gps_coords_do_not_raise(lat, lng):
    validate_gps_coords(lat, lng)  # should not raise


@pytest.mark.parametrize("lat,lng,match", [
    (91, 0, "Latitude"),
    (-91, 0, "Latitude"),
    (0, 181, "Longitude"),
    (0, -181, "Longitude"),
    (200, 500, "Latitude"),  # E-05 from TEST_CASES.md
])
def test_invalid_gps_coords_raise_value_error(lat, lng, match):
    with pytest.raises(ValueError, match=match):
        validate_gps_coords(lat, lng)


def test_non_numeric_gps_raises():
    with pytest.raises(ValueError, match="numeric"):
        validate_gps_coords("not_a_number", 0)


# ── require_fields ─────────────────────────────────────────────────────────────

def test_require_fields_passes_when_all_present():
    require_fields({"a": 1, "b": "x"}, ["a", "b"])


def test_require_fields_raises_on_missing():
    with pytest.raises(ValueError, match="Missing required fields"):
        require_fields({"a": 1}, ["a", "b"])


def test_require_fields_raises_on_none_value():
    with pytest.raises(ValueError, match="Missing required fields"):
        require_fields({"a": None, "b": 2}, ["a", "b"])


# ── Payload size ───────────────────────────────────────────────────────────────

def test_payload_within_size_limit_passes():
    validate_payload_size("x" * 100, max_bytes=10_240)


def test_payload_exceeding_size_limit_raises():
    with pytest.raises(ValueError, match="exceeds limit"):
        validate_payload_size("x" * 10_241, max_bytes=10_240)


# ── ORDER_PLACED payload ───────────────────────────────────────────────────────

def test_valid_order_placed_payload_passes():
    validate_order_placed_payload({
        "orderId": "ord_TEST",
        "customerId": "cus_TEST",
        "restaurantId": "rst_TEST",
        "pickupLat": 34.0522,
        "pickupLng": -118.2437,
        "dropoffLat": 34.0611,
        "dropoffLng": -118.24,
    })


def test_order_placed_missing_order_id_raises():
    with pytest.raises(ValueError):
        validate_order_placed_payload({
            "customerId": "cus_TEST",
            "restaurantId": "rst_TEST",
            "pickupLat": 34.0,
            "pickupLng": -118.0,
            "dropoffLat": 34.0,
            "dropoffLng": -118.0,
        })


# ── LOCATION_UPDATE payload ────────────────────────────────────────────────────

def test_valid_location_update_payload_passes():
    validate_location_update_payload({
        "courierId": "cur_TEST",
        "lat": 34.0505,
        "lng": -118.2420,
    })


def test_location_update_invalid_lat_raises():
    with pytest.raises(ValueError, match="Latitude"):
        validate_location_update_payload({
            "courierId": "cur_TEST",
            "lat": 999,
            "lng": -118.24,
        })
