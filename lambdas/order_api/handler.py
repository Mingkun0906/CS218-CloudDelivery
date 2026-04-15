"""OrderAPILambda — API Gateway handler for /orders endpoints.

Trigger  : API Gateway REST (POST /orders, GET /orders/{orderId}, DELETE /orders/{orderId})
Owner    : Mingkun Liu (business logic) / Zhuoqun Wei (API Gateway + Cognito wiring)

TODO (Zhuoqun): Wire this Lambda to API Gateway routes and Cognito authorizer
in template.yaml. The handler skeleton is ready; it needs real resource ARNs
from config/dev.json (ORDER_STREAM_NAME, ORDERS_TABLE) before it can be tested
end-to-end.

Current state: skeleton only. Business logic to be implemented once
template.yaml is deployed and config/dev.json is populated with real values.
"""
import json
import os

import boto3
from botocore.exceptions import ClientError

from shared import logger
from shared.errors import invalid_request, not_found, service_unavailable
from shared.tracing import get_trace_id

# TODO: implement POST /orders, GET /orders/{id}, DELETE /orders/{id}
# See INTERFACE_SPEC.md §3.1–3.3 for request/response schemas.
# See INTERFACE_SPEC.md §4 for state machine transition rules.


def lambda_handler(event: dict, context) -> dict:
    """Route API Gateway proxy events to the correct endpoint handler."""
    trace_id = get_trace_id()
    method = event.get("httpMethod", "")
    path = event.get("path", "")

    logger.info("api_request", method=method, path=path, trace_id=trace_id)

    # TODO: implement routing and business logic.
    return {
        "statusCode": 501,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"message": "Not implemented — see lambdas/order_api/handler.py"}),
    }
