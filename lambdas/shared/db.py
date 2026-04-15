import os
import boto3

# Initialized outside handler — reused across warm Lambda invocations
_dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
_table = _dynamodb.Table(os.environ.get("ORDERS_TABLE", "clouddelivery-dev"))


def get_table():
    return _table
