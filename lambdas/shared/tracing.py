"""X-Ray tracing helpers.

Provides:
- get_trace_id()  — extract the Root trace ID from the Lambda environment
- subsegment(name) — context manager for a named X-Ray subsegment

If AWS_XRAY_SDK_DISABLED=true (set in test environments) or aws_xray_sdk is not
installed, subsegment() is a no-op context manager. The application never fails
due to a missing or inactive X-Ray segment.

Subsegment names used in this codebase (from INTERFACE_SPEC.md §11.2):
  MatchingLambda  : redis.georadius, dynamo.conditional_write, sns.publish
  LocationLambda  : redis.geoadd, redis.set_ttl
  OrderAPILambda  : dynamo.put_order, kinesis.put_record
  CourierAPILambda: kinesis.put_location, dynamo.update_courier
  NotificationLambda: sns.publish_notification
"""
import os
from contextlib import contextmanager
from typing import Generator, Optional

_XRAY_DISABLED = os.environ.get("AWS_XRAY_SDK_DISABLED", "").lower() == "true"

try:
    from aws_xray_sdk.core import xray_recorder as _recorder

    # LOG_ERROR: log a warning instead of raising when there is no active segment.
    # This prevents test failures and handles edge cases in Lambda cold starts.
    _recorder.configure(context_missing="LOG_ERROR")
    _XRAY_AVAILABLE = True
except ImportError:  # pragma: no cover
    _XRAY_AVAILABLE = False


class _NoOpSegment:
    """Returned by subsegment() when X-Ray is disabled or unavailable."""

    def put_metadata(self, key: str, value, namespace: str = "default") -> None:  # noqa: ARG002
        pass

    def put_annotation(self, key: str, value) -> None:
        pass


@contextmanager
def subsegment(name: str) -> Generator[Optional[_NoOpSegment], None, None]:
    """Context manager that wraps a block in an X-Ray subsegment.

    Yields the subsegment (or a no-op stub) so callers can call
    put_metadata / put_annotation without checking for None.

    Example::

        with subsegment("redis.georadius") as seg:
            seg.put_metadata("radius_km", 5)
            results = redis_client.geosearch(...)
    """
    if _XRAY_DISABLED or not _XRAY_AVAILABLE:
        yield _NoOpSegment()
        return

    try:
        with _recorder.in_subsegment(name) as seg:
            yield seg
    except Exception:
        # Never let X-Ray instrumentation break the application.
        yield _NoOpSegment()


def get_trace_id() -> str:
    """Extract the X-Ray Root trace ID from the Lambda runtime environment.

    Returns 'unknown' if the environment variable is absent or malformed.
    The _X_AMZN_TRACE_ID format is:
        Root=1-5f1c2d3e-4a5b6c7d8e9f0a1b2c3d4e5f;Parent=...;Sampled=1
    """
    header = os.environ.get("_X_AMZN_TRACE_ID", "")
    for part in header.split(";"):
        if part.startswith("Root="):
            return part[5:]
    return "unknown"
