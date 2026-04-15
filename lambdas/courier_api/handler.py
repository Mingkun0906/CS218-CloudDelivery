"""CourierAPILambda — API Gateway handler for /couriers endpoints.

Trigger  : API Gateway REST (PUT /couriers/{id}/location, assignment endpoints)
Owner    : Mingkun Liu (business logic) / Zhuoqun Wei (API Gateway + Cognito wiring)

TODO (Zhuoqun): Wire this Lambda to API Gateway routes and Cognito authorizer
in template.yaml. The handler skeleton is ready; it needs real resource ARNs
from config/dev.json (LOCATION_STREAM_NAME, ORDERS_TABLE) before it can be
tested end-to-end.

Current state: skeleton only. Business logic to be implemented once
template.yaml is deployed and config/dev.json is populated with real values.
"""
import json
import os

from shared import logger
from shared.tracing import get_trace_id

# TODO: implement:
#   PUT /couriers/{id}/location          → validate GPS, publish LOCATION_UPDATE to Kinesis
#   GET /couriers/{id}/assignment        → read DynamoDB for current assignment
#   PUT /couriers/{id}/assignment/accept → update order status, update courier status
#   PUT /couriers/{id}/assignment/reject → trigger reassignment flow
#   PUT /couriers/{id}/assignment/status → update delivery progress (picked_up / delivered)
# See INTERFACE_SPEC.md §3.4–3.8 for request/response schemas.
# See INTERFACE_SPEC.md §4 for allowed status transitions.


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
        "body": json.dumps({"message": "Not implemented — see lambdas/courier_api/handler.py"}),
    }
