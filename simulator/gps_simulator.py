#!/usr/bin/env python3
"""GPS courier location simulator for CloudDelivery.

Simulates N couriers performing random walks around a configurable geographic
area, publishing LOCATION_UPDATE events directly to the Kinesis location
stream at a fixed interval.

Bypasses API Gateway and Cognito — intended for load testing and development
seeding only. In production, couriers would push updates via the REST API.

Usage:
    python simulator/gps_simulator.py                         # 10 couriers, 5s interval
    python simulator/gps_simulator.py --couriers 100          # scale up
    python simulator/gps_simulator.py --interval 2 --duration 30  # 30-minute burst
    python simulator/gps_simulator.py --dry-run               # print events, no AWS calls

Requirements:
    pip install boto3 python-ulid
"""

import argparse
import json
import logging
import math
import random
import signal
import sys
import time
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

try:
    from ulid import ULID
    def _new_id(prefix: str) -> str:
        return f"{prefix}{ULID()}"
except ImportError:
    import uuid
    def _new_id(prefix: str) -> str:
        return f"{prefix}{uuid.uuid4().hex.upper()}"

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    datefmt="%H:%M:%S",
)
log = logging.getLogger("gps_simulator")

# ── Defaults ──────────────────────────────────────────────────────────────────

# Irvine, CA — UCI campus area
DEFAULT_CENTER_LAT: float = 33.6846
DEFAULT_CENTER_LNG: float = -117.8265
DEFAULT_RADIUS_KM: float = 5.0

# Max distance a courier drifts per tick (degrees). At 5s interval this is
# roughly 30–55 m/tick, comparable to a cyclist at 20–40 km/h.
MAX_DRIFT_DEG: float = 0.0005

# Kinesis PutRecords limit
KINESIS_MAX_BATCH: int = 500


# ── Courier ───────────────────────────────────────────────────────────────────

class Courier:
    """A single simulated courier performing a bounded random walk."""

    def __init__(self, courier_id: str, lat: float, lng: float) -> None:
        self.courier_id = courier_id
        self.lat = lat
        self.lng = lng
        # Heading in radians; changes slowly each tick.
        self.heading: float = random.uniform(0, 2 * math.pi)

    def move(self, center_lat: float, center_lng: float, radius_km: float) -> None:
        """Advance position by one tick; reflect off the boundary."""
        # 20% chance to turn up to 45 degrees per tick
        if random.random() < 0.2:
            self.heading += random.uniform(-math.pi / 4, math.pi / 4)

        drift = random.uniform(MAX_DRIFT_DEG * 0.3, MAX_DRIFT_DEG)
        candidate_lat = self.lat + drift * math.cos(self.heading)
        candidate_lng = self.lng + drift * math.sin(self.heading)

        if _within_radius(candidate_lat, candidate_lng, center_lat, center_lng, radius_km):
            self.lat = candidate_lat
            self.lng = candidate_lng
        else:
            # Turn back toward center with some random spread
            self.heading = math.atan2(
                center_lat - self.lat,
                center_lng - self.lng,
            ) + random.uniform(-math.pi / 6, math.pi / 6)


# ── Geometry helpers ──────────────────────────────────────────────────────────

def _within_radius(lat: float, lng: float, center_lat: float, center_lng: float, radius_km: float) -> bool:
    """Approximate Euclidean check in degrees → km."""
    dlat_km = (lat - center_lat) * 111.0
    dlng_km = (lng - center_lng) * 111.0 * math.cos(math.radians(center_lat))
    return math.hypot(dlat_km, dlng_km) <= radius_km


def _random_point(center_lat: float, center_lng: float, radius_km: float) -> Tuple[float, float]:
    """Uniform random point within a circular area."""
    radius_deg_lat = radius_km / 111.0
    radius_deg_lng = radius_km / (111.0 * math.cos(math.radians(center_lat)))
    while True:
        dlat = random.uniform(-radius_deg_lat, radius_deg_lat)
        dlng = random.uniform(-radius_deg_lng, radius_deg_lng)
        if _within_radius(center_lat + dlat, center_lng + dlng, center_lat, center_lng, radius_km):
            return center_lat + dlat, center_lng + dlng


# ── Event building ────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_event(courier: Courier) -> dict:
    now = _now_iso()
    return {
        "eventId": _new_id("evt_"),
        "eventType": "LOCATION_UPDATE",
        "schemaVersion": "1.0",
        "timestamp": now,
        "traceId": f"sim-{courier.courier_id[-8:]}",
        "payload": {
            "courierId": courier.courier_id,
            "lat": round(courier.lat, 6),
            "lng": round(courier.lng, 6),
            "timestamp": now,
        },
    }


# ── Kinesis publishing ────────────────────────────────────────────────────────

def _send_batch(
    kinesis,
    stream_name: str,
    couriers: List[Courier],
) -> Tuple[int, int]:
    """Send one location update per courier via PutRecords. Returns (sent, failed)."""
    records = [
        {
            "Data": json.dumps(_build_event(c)).encode("utf-8"),
            "PartitionKey": c.courier_id,
        }
        for c in couriers
    ]

    sent = 0
    failed = 0

    # Split into Kinesis-allowed batches of 500
    for i in range(0, len(records), KINESIS_MAX_BATCH):
        chunk = records[i : i + KINESIS_MAX_BATCH]
        try:
            resp = kinesis.put_records(StreamName=stream_name, Records=chunk)
            chunk_failed = resp.get("FailedRecordCount", 0)
            sent += len(chunk) - chunk_failed
            failed += chunk_failed
            if chunk_failed:
                log.warning(
                    "%d record(s) failed in Kinesis response (throttle or shard error)",
                    chunk_failed,
                )
        except ClientError as exc:
            log.error("PutRecords error: %s", exc)
            failed += len(chunk)

    return sent, failed


# ── Main ──────────────────────────────────────────────────────────────────────

def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="CloudDelivery GPS courier simulator",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--couriers", type=int, default=10,
                   help="Number of simulated couriers")
    p.add_argument("--interval", type=float, default=5.0,
                   help="Location update interval in seconds")
    p.add_argument("--duration", type=int, default=0,
                   help="Run duration in minutes (0 = run until Ctrl-C)")
    p.add_argument("--env", default="dev", choices=["dev", "prod"],
                   help="Environment (determines default stream name)")
    p.add_argument("--stream-name", default="",
                   help="Kinesis stream name (overrides --env default)")
    p.add_argument("--region", default="us-east-1",
                   help="AWS region")
    p.add_argument("--center-lat", type=float, default=DEFAULT_CENTER_LAT,
                   help="Simulation center latitude")
    p.add_argument("--center-lng", type=float, default=DEFAULT_CENTER_LNG,
                   help="Simulation center longitude")
    p.add_argument("--radius-km", type=float, default=DEFAULT_RADIUS_KM,
                   help="Simulation area radius in km")
    p.add_argument("--dry-run", action="store_true",
                   help="Print events to stdout instead of sending to Kinesis")
    p.add_argument("--seed", type=int, default=None,
                   help="Random seed for reproducible runs")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    args = _parse_args(argv)

    if args.seed is not None:
        random.seed(args.seed)

    stream_name = args.stream_name or f"clouddelivery-locations-{args.env}"
    end_time = time.monotonic() + args.duration * 60 if args.duration > 0 else None

    log.info(
        "Simulator starting | couriers=%d interval=%.1fs stream=%s%s",
        args.couriers,
        args.interval,
        stream_name,
        " [DRY RUN]" if args.dry_run else "",
    )
    log.info(
        "Area | center=(%.4f, %.4f) radius=%.1f km",
        args.center_lat,
        args.center_lng,
        args.radius_km,
    )

    # Spawn couriers
    couriers = []
    for _ in range(args.couriers):
        lat, lng = _random_point(args.center_lat, args.center_lng, args.radius_km)
        couriers.append(Courier(_new_id("cur_"), lat, lng))
    log.info("Spawned %d couriers", len(couriers))

    kinesis = None if args.dry_run else boto3.client("kinesis", region_name=args.region)

    # Graceful shutdown
    running = True

    def _handle_stop(sig, _frame) -> None:
        nonlocal running
        log.info("Signal %s received — stopping after current tick", sig)
        running = False

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    tick = 0
    total_sent = 0
    total_failed = 0

    while running:
        if end_time is not None and time.monotonic() >= end_time:
            log.info("Duration reached — stopping")
            break

        tick += 1
        tick_start = time.monotonic()

        for c in couriers:
            c.move(args.center_lat, args.center_lng, args.radius_km)

        if args.dry_run:
            sample = _build_event(couriers[0])
            log.info("tick=%d [dry-run] sample event: %s", tick, json.dumps(sample))
            sent, failed = len(couriers), 0
        else:
            sent, failed = _send_batch(kinesis, stream_name, couriers)
            total_sent += sent
            total_failed += failed

        elapsed = time.monotonic() - tick_start
        log.info(
            "tick=%d sent=%d failed=%d elapsed=%.3fs",
            tick, sent, failed, elapsed,
        )

        sleep_secs = max(0.0, args.interval - elapsed)
        if sleep_secs > 0 and running:
            time.sleep(sleep_secs)

    log.info(
        "Stopped | ticks=%d total_sent=%d total_failed=%d",
        tick, total_sent, total_failed,
    )


if __name__ == "__main__":
    main()
