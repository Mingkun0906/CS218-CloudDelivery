"""RFC 7807 Problem Details response builder.

See: https://www.rfc-editor.org/rfc/rfc7807
Every error response includes a traceId field for X-Ray correlation.
"""
import json
from typing import Optional


def problem(
    status: int,
    error_code: str,
    title: str,
    detail: str,
    instance: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> dict:
    """Build an API Gateway-compatible RFC 7807 error response."""
    body: dict = {
        "type": f"/errors/{error_code}",
        "title": title,
        "status": status,
        "detail": detail,
    }
    if instance:
        body["instance"] = instance
    if trace_id:
        body["traceId"] = trace_id

    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/problem+json"},
        "body": json.dumps(body),
    }


# Convenience constructors for the error codes in INTERFACE_SPEC.md §10.

def invalid_request(detail: str, instance: str = None, trace_id: str = None) -> dict:
    return problem(400, "invalid-request", "Invalid Request", detail, instance, trace_id)


def unauthorized(detail: str = "Missing or invalid token.", trace_id: str = None) -> dict:
    return problem(401, "unauthorized", "Unauthorized", detail, trace_id=trace_id)


def forbidden(detail: str, instance: str = None, trace_id: str = None) -> dict:
    return problem(403, "forbidden", "Forbidden", detail, instance, trace_id)


def not_found(detail: str, instance: str = None, trace_id: str = None) -> dict:
    return problem(404, "not-found", "Not Found", detail, instance, trace_id)


def conflict(detail: str, instance: str = None, trace_id: str = None) -> dict:
    return problem(409, "conflict", "Conflict", detail, instance, trace_id)


def service_unavailable(detail: str, trace_id: str = None) -> dict:
    return problem(503, "service-unavailable", "Service Unavailable", detail, trace_id=trace_id)
