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

    logger.info("courier_api_request", method=method, path=path, trace_id=trace_id)

    try:
        if method == "PUT" and path == "/couriers/{courierId}/location":
            return _put_location(event, trace_id)
        elif method == "GET" and path == "/couriers/{courierId}/assignment":
            return _get_assignment(event, trace_id)
        elif method == "PUT" and path == "/couriers/{courierId}/assignment/accept":
            return _accept_assignment(event, trace_id)
        elif method == "PUT" and path == "/couriers/{courierId}/assignment/reject":
            return _reject_assignment(event, trace_id)
        elif method == "PUT" and path == "/couriers/{courierId}/assignment/status":
            return _update_delivery_status(event, trace_id)
        else:
            return errors.problem(404, "not-found", "Not Found",
                                  f"Route {method} {path} not found",
                                  trace_id=trace_id)
    except Exception as e:
        logger.error("unhandled_error", error=str(e), trace_id=trace_id)
        return errors.problem(500, "internal-error", "Internal Server Error",
                              "An unexpected error occurred.", trace_id=trace_id)


def _put_location(event: dict, trace_id: str) -> dict:
    # TODO: validate body, publish LOCATION_UPDATE to Kinesis
    return {"statusCode": 204, "body": ""}


def _get_assignment(event: dict, trace_id: str) -> dict:
    # TODO: query DynamoDB for active assignment
    return {"statusCode": 204, "body": ""}


def _accept_assignment(event: dict, trace_id: str) -> dict:
    # TODO: update courier status in DynamoDB
    return {"statusCode": 200, "body": json.dumps({"message": "not implemented"})}


def _reject_assignment(event: dict, trace_id: str) -> dict:
    # TODO: update order to reassigning, re-publish to Kinesis
    return {"statusCode": 200, "body": json.dumps({"message": "not implemented"})}


def _update_delivery_status(event: dict, trace_id: str) -> dict:
    # TODO: validate transition, update DynamoDB with optimistic lock
    return {"statusCode": 200, "body": json.dumps({"message": "not implemented"})}
