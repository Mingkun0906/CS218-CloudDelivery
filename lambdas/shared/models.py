"""Typed data models for CloudDelivery events.

Mirrors the schemas in INTERFACE_SPEC.md §1, §6, §8.
Using dataclasses (no external dependency) so these work in all Lambdas.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


# ── ID prefixes (INTERFACE_SPEC.md §1) ────────────────────────────────────────

ORDER_PREFIX = "ord_"
COURIER_PREFIX = "cur_"
CUSTOMER_PREFIX = "cus_"
RESTAURANT_PREFIX = "rst_"
EVENT_PREFIX = "evt_"

# ── Kinesis event envelopes (INTERFACE_SPEC.md §6) ────────────────────────────

EVENT_TYPE_ORDER_PLACED = "ORDER_PLACED"
EVENT_TYPE_LOCATION_UPDATE = "LOCATION_UPDATE"
EVENT_TYPE_ORDER_STATUS_CHANGED = "ORDER_STATUS_CHANGED"


@dataclass
class EventEnvelope:
    event_id: str
    event_type: str
    schema_version: str
    timestamp: str
    trace_id: str
    payload: Dict[str, Any]

    @classmethod
    def from_dict(cls, d: dict) -> "EventEnvelope":
        return cls(
            event_id=d["eventId"],
            event_type=d["eventType"],
            schema_version=d.get("schemaVersion", "1.0"),
            timestamp=d["timestamp"],
            trace_id=d.get("traceId", "unknown"),
            payload=d.get("payload", {}),
        )


@dataclass
class OrderPlacedPayload:
    order_id: str
    customer_id: str
    restaurant_id: str
    pickup_lat: float
    pickup_lng: float
    dropoff_lat: float
    dropoff_lng: float
    total_amount_cents: int
    created_at: str

    @classmethod
    def from_dict(cls, d: dict) -> "OrderPlacedPayload":
        return cls(
            order_id=d["orderId"],
            customer_id=d["customerId"],
            restaurant_id=d["restaurantId"],
            pickup_lat=float(d["pickupLat"]),
            pickup_lng=float(d["pickupLng"]),
            dropoff_lat=float(d["dropoffLat"]),
            dropoff_lng=float(d["dropoffLng"]),
            total_amount_cents=int(d["totalAmountCents"]),
            created_at=d["createdAt"],
        )


@dataclass
class LocationUpdatePayload:
    courier_id: str
    lat: float
    lng: float
    timestamp: str

    @classmethod
    def from_dict(cls, d: dict) -> "LocationUpdatePayload":
        return cls(
            courier_id=d["courierId"],
            lat=float(d["lat"]),
            lng=float(d["lng"]),
            timestamp=d["timestamp"],
        )


# ── SNS / SQS notification schemas (INTERFACE_SPEC.md §8) ─────────────────────

NOTIFICATION_TYPE_COURIER_ASSIGNED = "COURIER_ASSIGNED"
NOTIFICATION_TYPE_ORDER_STATUS_UPDATE = "ORDER_STATUS_UPDATE"
NOTIFICATION_TYPE_NO_COURIER_FOUND = "NO_COURIER_FOUND"


@dataclass
class NotificationMessage:
    notification_type: str
    event_id: str
    timestamp: str
    trace_id: str
    recipients: Dict[str, Optional[str]]
    data: Dict[str, Any]

    @classmethod
    def from_dict(cls, d: dict) -> "NotificationMessage":
        return cls(
            notification_type=d["notificationType"],
            event_id=d["eventId"],
            timestamp=d["timestamp"],
            trace_id=d.get("traceId", "unknown"),
            recipients=d.get("recipients", {}),
            data=d.get("data", {}),
        )


# ── Order status values (INTERFACE_SPEC.md §4) ────────────────────────────────

class OrderStatus:
    PLACED = "placed"
    MATCHING = "matching"
    ASSIGNED = "assigned"
    REASSIGNING = "reassigning"
    UNASSIGNED = "unassigned"
    CONFIRMED = "confirmed"
    PREPARING = "preparing"
    READY_FOR_PICKUP = "ready_for_pickup"
    PICKED_UP = "picked_up"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    FAILED = "failed"

    TERMINAL = {DELIVERED, CANCELLED, FAILED}
    CANCELLABLE = {PLACED, MATCHING, ASSIGNED, REASSIGNING, UNASSIGNED, CONFIRMED, PREPARING, READY_FOR_PICKUP}
