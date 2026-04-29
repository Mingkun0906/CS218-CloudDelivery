# CloudDelivery

A serverless real-time food delivery matching engine built on AWS, developed as the CS218 final project.

**Team:** Zhuoqun Wei (infrastructure, IaC, observability) · Mingkun Liu (Lambda functions, load testing, failure injection)

---

## Architecture

Customer orders flow through API Gateway → Kinesis → MatchingLambda → Redis geo-query → DynamoDB conditional write → SNS → SQS → push notification.

Courier GPS pings flow through API Gateway → Kinesis → LocationLambda → Redis GEOADD (30s TTL).

```
PUBLIC                          PRIVATE VPC
──────                          ───────────
API Gateway (REST)              MatchingLambda   →  Redis (GEORADIUS)
Cognito (JWT auth)              LocationLambda   →  DynamoDB (conditional write)
                                NotificationLambda  →  SNS → SQS → DLQ
```

**Key design choices:**
- Kinesis over SQS — per-shard ordering prevents stale GPS pings from overwriting fresher ones
- Redis GEORADIUS — sub-millisecond nearest-courier lookup; DynamoDB alone cannot meet matching latency targets
- DynamoDB conditional write (`attribute_not_exists(courierId)`) — prevents double-assignment race between concurrent Lambda invocations
- SNS → SQS + DLQ — decouples notification delivery; failed messages retry 3× then land in DLQ

---

## AWS Resources

| Resource | Name |
|---|---|
| API endpoint | `https://8jgkm1xrqg.execute-api.us-east-1.amazonaws.com/dev` |
| DynamoDB table | `clouddelivery-dev` |
| Kinesis orders stream | `clouddelivery-orders-dev` |
| Kinesis locations stream | `clouddelivery-locations-dev` |
| ElastiCache Redis | `clouddelivery-redis-dev` |
| SNS match topic | `clouddelivery-match-notifications-dev` |
| SQS notification queue | `clouddelivery-notifications-dev` |
| SQS dead-letter queue | `clouddelivery-notifications-dlq-dev` |
| Region | `us-east-1` |

---

## Local Development Setup

```bash
# Clone and set up Python environment
git clone <repo-url>
cd CS218-CloudDelivery
python3 -m venv venv
source venv/bin/activate
pip install -r requirements-dev.txt
```

### Run tests

```bash
# Full suite
python -m pytest tests/ -q

# Single test file
python -m pytest tests/unit/test_order_api.py -v

# Single test
python -m pytest tests/unit/test_order_api.py::test_create_order_returns_202 -v
```

### Run the GPS courier simulator

```bash
# Simulate 20 couriers sending location updates every 5s
python simulator/gps_simulator.py --couriers 20 --interval 5
```

### Place a test order and watch it get matched

Run the simulator in **Terminal 1** (keep it running):

```bash
python simulator/gps_simulator.py --couriers 20 --interval 5
```

Then in **Terminal 2**, place a single order:

```bash
python - <<'EOF'
import boto3, json, time
from ulid import ULID

kinesis = boto3.client('kinesis', region_name='us-east-1')
table = boto3.resource('dynamodb', region_name='us-east-1').Table('clouddelivery-dev')

order_id = f'ord_{ULID()}'
now = '2026-04-24T00:00:00Z'

table.put_item(Item={
    'PK': f'ORDER#{order_id}', 'SK': 'METADATA',
    'orderId': order_id, 'customerId': f'cus_{ULID()}',
    'restaurantId': 'rst_test',
    'pickupLat': '33.6846', 'pickupLng': '-117.8265',
    'dropoffLat': '33.700',  'dropoffLng': '-117.840',
    'totalAmountCents': 1500, 'status': 'placed', 'createdAt': now,
})

event = {
    'eventId': f'evt_{ULID()}', 'eventType': 'ORDER_PLACED',
    'schemaVersion': '1.0', 'timestamp': now, 'traceId': 'manual-test',
    'payload': {
        'orderId': order_id, 'customerId': 'cus_test',
        'restaurantId': 'rst_test',
        'pickupLat': 33.6846, 'pickupLng': -117.8265,
        'dropoffLat': 33.700, 'dropoffLng': -117.840,
        'totalAmountCents': 1500, 'createdAt': now,
    }
}
kinesis.put_record(StreamName='clouddelivery-orders-dev',
                   Data=json.dumps(event).encode(), PartitionKey=order_id)
print(f'Sent order {order_id}. Polling...')

for _ in range(30):
    time.sleep(1)
    item = table.get_item(
        Key={'PK': f'ORDER#{order_id}', 'SK': 'METADATA'},
        ProjectionExpression='#s, courierId',
        ExpressionAttributeNames={'#s': 'status'}
    ).get('Item', {})
    status = item.get('status')
    print(f'  status={status}', end='\r')
    if status in ('assigned', 'unassigned', 'failed'):
        print(f'\nDone: status={status}  courierId={item.get("courierId", "none")}')
        break
EOF
```

Expected: `status=assigned` within ~3 seconds.

### Watch Lambda logs live

```bash
aws logs tail /aws/lambda/clouddelivery-matching-dev --follow --format short
```

### Run the Locust load test

The load test spawns 50 concurrent users placing orders. Run the simulator with enough couriers to exceed that demand — otherwise couriers get exhausted and orders return `unassigned` rather than exercising the matching path.

**Terminal 1** — start couriers (keep running):

```bash
python simulator/gps_simulator.py --couriers 200 --interval 5
```

**Terminal 2** — run the load test:

```bash
locust -f tests/locustfile.py --headless -u 50 -r 10 --run-time 5m --html /tmp/report.html
open /tmp/report.html
```

Target: `place_order` p99 ≤ 500ms; `matching_latency` p99 ≤ 500ms; 0% failures after Lambda warms up (~30s).

---

## Failure Injection

Live AWS injection scripts are in `tests/failure/scripts/`. Each has `--inject`, `--restore`, and `--status` flags.

| Script | Scenario |
|---|---|
| `fl03_redis_failover.py` | Trigger ElastiCache primary failover; monitor ReplicationLag recovery |
| `fl04_dynamodb_throttle.py` | Switch table to 1 WCU provisioned to force throttling |
| `fl06_sqs_block.py` | Deny-all SQS queue policy → messages exhaust retries → DLQ |
| `fl07_kinesis_pause.py` | Disable MatchingLambda event source mapping; verify replay on restore |
| `fl08_lambda_timeout.py` | Set OrderAPILambda timeout to 1s to force timeouts |
| `fl09_cognito_test.py` | Send missing/malformed JWT to live API; assert 401s |

```bash
# Example: inject DynamoDB throttling then restore
python tests/failure/scripts/fl04_dynamodb_throttle.py --inject
python tests/failure/scripts/fl04_dynamodb_throttle.py --status
python tests/failure/scripts/fl04_dynamodb_throttle.py --restore
```

Evidence for each scenario should be saved to `docs/failure_evidence/FL-XX/`.

---

## Target Performance

| Metric | Target |
|---|---|
| Matching latency p50 | ≤ 200ms |
| Matching latency p95 | ≤ 350ms |
| Matching latency p99 | ≤ 500ms |
| Assignment success rate | ≥ 99% |
| Lambda cold start duration | ≤ 500ms |
| Kinesis consumer lag | ≤ 5,000ms at 100 req/s |
| DLQ recovery time | All messages processed within 5 min of redrive |

---

## Deployment

Infrastructure is managed with AWS SAM (`template.yaml`). Deployed to AWS account `043254348860`, region `us-east-1`.

```bash
brew install aws-sam-cli   # one-time
sam build && sam deploy
```

See `INTERFACE_SPEC.md` for the full API contract and `TEST_CASES.md` for the complete test suite (50+ cases).
