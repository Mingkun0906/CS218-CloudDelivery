"""DynamoDB resource accessor.

The resource and Table are created at module level so the connection is reused
across warm Lambda invocations. boto3 resource creation is lazy — no network
call occurs until the first DynamoDB operation.

Table name: clouddelivery-{env}  (set via ORDERS_TABLE env var)
"""
import os

import boto3
from botocore.config import Config

_dynamodb = boto3.resource(
    "dynamodb",
    region_name="us-east-1",
    config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 1, "mode": "standard"}),
)
_table = _dynamodb.Table(os.environ.get("ORDERS_TABLE", "clouddelivery-dev"))


def get_table():
    """Return the shared DynamoDB Table resource."""
    return _table
