"""Input validation helpers.

All validation raises ValueError with a human-readable message.
Callers should catch ValueError and return HTTP 400 / drop the record.

Validation rules (INTERFACE_SPEC.md §3, CLAUDE.md Security section):
- GPS: lat ∈ [-90, 90], lng ∈ [-180, 180]
- Payload size: reject > 10 KB
- Required fields: checked before any AWS call
"""
from typing import Any, Dict, List


def validate_gps_coords(lat: Any, lng: Any) -> None:
    """Raise ValueError if lat/lng are out of valid range."""
    try:
        lat_f = float(lat)
        lng_f = float(lng)
    except (TypeError, ValueError):
        raise ValueError(f"GPS coordinates must be numeric; got lat={lat!r}, lng={lng!r}")

    if not (-90.0 <= lat_f <= 90.0):
        raise ValueError(f"Latitude {lat_f} out of range [-90, 90]")
    if not (-180.0 <= lng_f <= 180.0):
        raise ValueError(f"Longitude {lng_f} out of range [-180, 180]")


def require_fields(data: Dict[str, Any], fields: List[str]) -> None:
    """Raise ValueError if any required field is missing or None."""
    missing = [f for f in fields if data.get(f) is None]
    if missing:
        raise ValueError(f"Missing required fields: {missing}")


def validate_payload_size(raw: str, max_bytes: int = 10_240) -> None:
    """Raise ValueError if the payload string exceeds max_bytes."""
    size = len(raw.encode("utf-8"))
    if size > max_bytes:
        raise ValueError(f"Payload size {size} bytes exceeds limit of {max_bytes} bytes")


def validate_order_placed_payload(payload: dict) -> None:
    """Full validation for an ORDER_PLACED Kinesis event payload."""
    require_fields(
        payload,
        ["orderId", "customerId", "restaurantId", "pickupLat", "pickupLng", "dropoffLat", "dropoffLng"],
    )
    validate_gps_coords(payload["pickupLat"], payload["pickupLng"])
    validate_gps_coords(payload["dropoffLat"], payload["dropoffLng"])


def validate_location_update_payload(payload: dict) -> None:
    """Full validation for a LOCATION_UPDATE Kinesis event payload."""
    require_fields(payload, ["courierId", "lat", "lng"])
    validate_gps_coords(payload["lat"], payload["lng"])
