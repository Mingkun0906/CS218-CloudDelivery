import os
from aws_xray_sdk.core import xray_recorder


def get_trace_id() -> str:
    """Extract the X-Ray Root trace ID from the Lambda environment."""
    header = os.environ.get("_X_AMZN_TRACE_ID", "")
    for part in header.split(";"):
        if part.startswith("Root="):
            return part[5:]
    return "unknown"


def subsegment(name: str):
    """Context manager shortcut for X-Ray custom subsegments."""
    return xray_recorder.in_subsegment(name)
