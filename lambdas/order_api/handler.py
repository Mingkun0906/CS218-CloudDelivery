import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))

import logger
import tracing
import errors
from db import get_table

# Module-level — reused across warm invocations
table = get_table()
ENV = os.environ.get("ENV", "dev")


def lambda_handler(event, context):
    trace_id = tracing.get_trace_id()
    method = event["httpMethod"]
    path = event["resource"]

    logger.info("api_request", method=method, path=path, trace_id=trace_id)

    try:
        if method == "POST" and path == "/orders":
            return _post_order(event, trace_id)
        elif method == "GET" and path == "/orders/{orderId}":
            return _get_order(event, trace_id)
        elif method == "DELETE" and path == "/orders/{orderId}":
            return _delete_order(event, trace_id)
        else:
            return errors.problem(404, "not-found", "Not Found",
                                  f"Route {method} {path} not found",
                                  trace_id=trace_id)
    except Exception as e:
        logger.error("unhandled_error", error=str(e), trace_id=trace_id)
        return errors.problem(500, "internal-error", "Internal Server Error",
                              "An unexpected error occurred.", trace_id=trace_id)


def _post_order(event: dict, trace_id: str) -> dict:
    # TODO: implement order placement
    # 1. Parse and validate body
    # 2. Write to DynamoDB
    # 3. Publish ORDER_PLACED to Kinesis
    # 4. Return 202
    return {"statusCode": 202, "body": json.dumps({"message": "not implemented"})}


def _get_order(event: dict, trace_id: str) -> dict:
    # TODO: implement order fetch
    return {"statusCode": 200, "body": json.dumps({"message": "not implemented"})}


def _delete_order(event: dict, trace_id: str) -> dict:
    # TODO: implement order cancellation
    return {"statusCode": 200, "body": json.dumps({"message": "not implemented"})}
