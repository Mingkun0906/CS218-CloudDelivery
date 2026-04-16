"""Root test conftest — applies to tests/unit/ and tests/failure/.

IMPORTANT: Environment variables and sys.path adjustments must live here (not
in a subdirectory conftest) because this file is loaded first by pytest, before
any test module is collected or any Lambda handler is imported.
"""
import os
import sys

# ── sys.path setup ─────────────────────────────────────────────────────────────
# Add repo root  → enables `from tests.helpers import ...`
# Add lambdas/   → enables `from shared import ...` inside Lambda handlers
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_LAMBDAS_DIR = os.path.join(_REPO_ROOT, "lambdas")

for _p in (_REPO_ROOT, _LAMBDAS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── Environment variables ─────────────────────────────────────────────────────
# Must be set before any lambdas.* module is imported because module-level
# clients (cache.py, db.py) read these at import time.
os.environ.setdefault("ENV", "test")
os.environ.setdefault("LOG_LEVEL", "DEBUG")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")

os.environ.setdefault("ORDERS_TABLE", "clouddelivery-test")
os.environ.setdefault("REDIS_HOST", "localhost")
os.environ.setdefault("REDIS_PORT", "6379")
os.environ.setdefault("SEARCH_RADIUS_KM", "5")
os.environ.setdefault("MAX_COURIER_CANDIDATES", "10")
os.environ.setdefault("MATCH_TOPIC_ARN", "arn:aws:sns:us-east-1:123456789012:clouddelivery-match-notifications-test")
os.environ.setdefault("LOCATION_TTL_SECONDS", "30")
os.environ.setdefault("NOTIFICATION_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123456789012/clouddelivery-notifications-test")

# Disable X-Ray so subsegment() is a no-op in all tests.
os.environ["AWS_XRAY_SDK_DISABLED"] = "true"

# ── Shared fixtures ───────────────────────────────────────────────────────────
import pytest
import fakeredis
import boto3
from moto import mock_aws


@pytest.fixture
def fake_redis():
    """fakeredis client with GEO command support; replaces the real Redis client."""
    server = fakeredis.FakeServer()
    client = fakeredis.FakeRedis(server=server, decode_responses=True)
    yield client
    client.close()


@pytest.fixture
def aws_mock():
    """Activate moto AWS mock for DynamoDB, SNS, SQS, CloudWatch."""
    with mock_aws():
        yield


@pytest.fixture
def dynamodb_table(aws_mock):
    """Moto-backed DynamoDB table matching the clouddelivery single-table schema."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = dynamodb.create_table(
        TableName="clouddelivery-test",
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
            {"AttributeName": "GSI1SK", "AttributeType": "S"},
            {"AttributeName": "GSI2PK", "AttributeType": "S"},
            {"AttributeName": "GSI2SK", "AttributeType": "S"},
            {"AttributeName": "GSI3PK", "AttributeType": "S"},
            {"AttributeName": "GSI3SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
        GlobalSecondaryIndexes=[
            {
                "IndexName": "GSI1-CustomerOrders",
                "KeySchema": [
                    {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "GSI2-RestaurantOrders",
                "KeySchema": [
                    {"AttributeName": "GSI2PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI2SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "GSI3-CourierStatus",
                "KeySchema": [
                    {"AttributeName": "GSI3PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI3SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
    )
    table.wait_until_exists()
    yield table


@pytest.fixture
def sns_topic(aws_mock):
    """Moto-backed SNS topic; returns its ARN."""
    sns = boto3.client("sns", region_name="us-east-1")
    response = sns.create_topic(Name="clouddelivery-match-notifications-test")
    return response["TopicArn"]
