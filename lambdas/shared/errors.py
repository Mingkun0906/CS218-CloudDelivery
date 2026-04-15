import json


def problem(
    status: int,
    error_code: str,
    title: str,
    detail: str,
    instance: str = None,
    trace_id: str = None,
) -> dict:
    """Build an RFC 7807 Problem Details API Gateway response."""
    body = {
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
