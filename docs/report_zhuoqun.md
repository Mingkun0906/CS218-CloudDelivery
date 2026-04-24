# CloudDelivery — Zhuoqun Wei Report Sections

---

## 1. Infrastructure & IaC (AWS SAM / CloudFormation)

All infrastructure is defined as code in `template.yaml` using AWS SAM (Serverless Application Model), which transforms to CloudFormation. A single `sam build && sam deploy` command provisions and updates the entire stack from scratch.

### Resources provisioned

| Resource | Type | Configuration |
|---|---|---|
| `OrdersTable` | DynamoDB | On-demand billing, single-table design, 3 GSIs, TTL enabled |
| `OrderStream` | Kinesis | 1 shard, `clouddelivery-orders-dev` |
| `LocationStream` | Kinesis | 1 shard, `clouddelivery-locations-dev` |
| `MatchTopic` | SNS | Fan-out to notification queue |
| `NotificationQueue` | SQS | 30s visibility timeout, DLQ after 3 failures |
| `NotificationDLQ` | SQS | 14-day retention for manual inspection |
| `RetryQueue` | SQS | Per-message 30s delay for unmatched order retry |
| `MatchingLambda` | Lambda | Kinesis trigger, batch=1, partial batch failure |
| `LocationLambda` | Lambda | Kinesis trigger, batch=10 |
| `NotificationLambda` | Lambda | SQS trigger, batch=10 |
| `RetryLambda` | Lambda | SQS trigger, re-injects to Kinesis |
| `OrderAPILambda` | Lambda | REST API Gateway proxy |
| `CourierAPILambda` | Lambda | REST API Gateway proxy |
| `CloudDeliveryAPI` | API Gateway | Cognito JWT authorizer |
| `CognitoUserPool` | Cognito | Customer and courier identity |
| VPC Endpoints (4) | EC2 | DynamoDB (Gateway), SNS/SQS/CloudWatch (Interface) |
| CloudWatch Dashboard | CloudWatch | 8 metric widgets, matching latency, DLQ depth, cold starts |
| CloudWatch Alarms (5) | CloudWatch | p99 latency, DLQ depth, Kinesis lag, cold starts, throttles |

### Globals pattern

All six Lambda functions inherit common configuration from the SAM `Globals` block: Python 3.12 runtime, 256MB memory, 30s timeout, X-Ray active tracing, VPC placement, and shared environment variables (`ENV`, `ORDERS_TABLE`, `REDIS_HOST`). Function-specific variables (e.g., `MATCH_TOPIC_ARN`, `SEARCH_RADIUS_KM`) are declared per-function.

---

## 2. Security Implementation

### IAM — Least-Privilege Roles

SAM generates a separate execution role for each Lambda. Policies are attached using SAM policy templates (e.g., `DynamoDBCrudPolicy`, `KinesisStreamReadPolicy`) scoped to specific resource ARNs, not wildcards.

Each Lambda is granted only what it needs:

- **MatchingLambda**: DynamoDB CRUD on orders table; SNS publish to match topic only; Kinesis read on orders stream only; SQS send to retry queue only; `cloudwatch:PutMetricData`.
- **LocationLambda**: Kinesis read on locations stream only; `cloudwatch:PutMetricData`.
- **NotificationLambda**: SQS poll/delete on notification queue; DynamoDB read on orders table; `cloudwatch:PutMetricData`.
- **RetryLambda**: SQS poll/delete on retry queue; Kinesis put on orders stream.
- **OrderAPILambda / CourierAPILambda**: DynamoDB CRUD; Kinesis put on respective streams.

No Lambda has `*` resource access. No shared catch-all role exists.

### Network Controls — VPC Isolation

All six Lambda functions run inside the private VPC (`vpc-022347f78ef0a3b2c`) in two AZs, attached to security group `clouddelivery-lambda-sg`. This group has **no inbound rules** from the internet.

**Redis**: ElastiCache security group allows inbound TCP 6379 **from the Lambda security group only**. No other inbound rule exists.

**DynamoDB**: accessed via a **VPC Gateway Endpoint** — traffic never traverses the public internet or NAT. The endpoint is attached to the Lambda route tables via the `pl-63a5400a` prefix list.

**SNS, SQS, CloudWatch**: accessed via **VPC Interface Endpoints** with Private DNS enabled. Lambda subnets can resolve `sns.us-east-1.amazonaws.com` to the endpoint's private IP without needing a NAT gateway or internet gateway.

**API Gateway** is the only public-facing endpoint. All other services have no public routes.

### Input Validation

All Lambda handlers validate required fields before touching any data store. Payload validation is implemented in `shared/validation.py`:

- `ORDER_PLACED` events: required fields checked (orderId, customerId, restaurantId, pickupLat, pickupLng, dropoffLat, dropoffLng, totalAmountCents).
- GPS coordinates: lat ∈ [-90, 90], lng ∈ [-180, 180].
- Kinesis records: envelope schema validated (eventType, schemaVersion, payload).

API Gateway request validators enforce required body fields and path parameters before the Lambda is invoked.

---

## 3. Observability

### CloudWatch Dashboard (`CloudDelivery-dev`)

A single dashboard with 8 widgets monitors the full system:

| Widget | Metric | Source |
|---|---|---|
| Matching latency p50/p95/p99 | `matching.duration_ms` | Custom (MatchingLambda) |
| Assignment success rate | `matching.assignment_result` by Result dimension | Custom |
| Couriers in radius | `matching.couriers_in_radius` | Custom |
| Lambda concurrent executions | `ConcurrentExecutions` | Lambda service |
| Cold start count | `InitDuration > 0` | Lambda Insights |
| Kinesis consumer lag | `GetRecords.IteratorAgeMilliseconds` | Kinesis service |
| DLQ depth | `ApproximateNumberOfMessagesVisible` | SQS service |
| DynamoDB throttles | `ThrottledRequests` | DynamoDB service |

### CloudWatch Alarms

| Alarm | Threshold | Action |
|---|---|---|
| `clouddelivery-dlq-depth-dev` | DLQ > 0 messages | SNS alert |
| `clouddelivery-matching-latency-dev` | p99 > 1,000ms | SNS alert |
| `clouddelivery-kinesis-lag-dev` | IteratorAge > 10,000ms | SNS alert |
| `clouddelivery-lambda-coldstart-dev` | InitDuration count > 20/min | SNS alert |
| `clouddelivery-dynamodb-throttle-dev` | ThrottledRequests > 0 | SNS alert |

### X-Ray Distributed Tracing

All Lambdas run with `Tracing: Active`. MatchingLambda adds custom subsegments:
- `redis.georadius` — captures GEOSEARCH latency and radius metadata
- `dynamo.conditional_write` — captures per-courier assignment attempt latency
- `dynamo.increment_retry` / `dynamo.update_status` — retry path timing
- `sns.publish` — assignment notification publish latency

This lets the X-Ray service map show per-component breakdown for a single order's end-to-end trace.

### Structured Logging

All Lambdas emit JSON log lines via `shared/logger.py`:
```json
{"level": "INFO", "event": "courier_assigned", "env": "dev",
 "order_id": "ord_01KP...", "courier_id": "cur_01KP...", "trace_id": "1-..."}
```

CloudWatch Logs Insights queries can filter by `event`, `order_id`, or `trace_id` across all Lambda log groups simultaneously.

---

## 4. GPS Courier Location Simulator (`simulator/gps_simulator.py`)

The simulator seeds the Redis geo-index with realistic courier movement, enabling end-to-end testing without a real mobile app.

### Design

- Spawns N virtual couriers, each with a ULID-based ID and a starting position near UCI campus (33.6846°N, 117.8265°W).
- Each interval (default 5s), each courier moves by a small random delta (simulating walking/cycling speed: ~0.3–1.5 km/interval).
- For each update, the simulator:
  1. Calls `GEOADD couriers:locations {lng} {lat} {courier_id}` to update the geo-index.
  2. Calls `SET courier:ttl:{courier_id} 1 EX 30` to refresh the 30s TTL sentinel.
- Couriers that stop updating (simulator killed) naturally expire from the index within 30s, preventing stale matches.

### Usage

```bash
python3 simulator/gps_simulator.py --couriers 50 --interval 5
```

### Why Redis TTL sentinel?

The geo-index (`GEOADD`) has no built-in TTL per member. A courier that goes offline leaves a stale entry in the sorted set. The sentinel key (`courier:ttl:{id}`) is a separate key with a 30s TTL. MatchingLambda checks `EXISTS courier:ttl:{id}` before attempting an assignment, skipping couriers whose TTL has expired even if they still appear in the geo-index.

---

## 5. Load Testing (`tests/locustfile.py`)

### Design

Locust simulates customers placing orders by injecting directly into Kinesis (bypassing API Gateway and Cognito), enabling pure matching throughput measurement without auth overhead.

Each virtual user:
1. Pre-creates the DynamoDB order record with `status=placed` (mirroring what OrderAPI does in production).
2. Puts an `ORDER_PLACED` event to the Kinesis orders stream.
3. Polls DynamoDB every 500ms until the order reaches a terminal status (`assigned`, `unassigned`, or `failed`), up to 15s.
4. Reports two separate Locust metrics: Kinesis put latency and end-to-end matching latency.

Two user classes:
- `OrderUser`: 1–3s think time between orders.
- `BurstOrderUser`: 0.1–0.5s think time for burst scenario S-03.

### Test Scenarios

| Scenario | Users | Ramp | Duration | Purpose |
|---|---|---|---|---|
| S-01 baseline | 1 | 1/s | 5m | Single-user latency baseline |
| S-02 ramp | 50 | 5/s | 10m | Sustained throughput, warm Lambda |
| S-03 burst | 100 | 100/s | 5m | Cold start surge, Kinesis backpressure |

---

## 6. Cost Analysis

### Static Monthly Estimate

| Service | Cost Driver | Estimated/Month |
|---|---|---|
| API Gateway | REST calls | $5–15 |
| Kinesis (2 shards) | Shard-hours | $15–30 |
| Lambda | Invocations + GB-s | $0–5 (free tier) |
| DynamoDB (on-demand) | RCU/WCU consumed | $10–25 |
| ElastiCache Redis (t3.micro, Multi-AZ) | Instance-hours | $25–50 |
| CloudWatch + X-Ray + SNS/SQS | Metrics/traces/messages | $5–15 |
| **Total** | | **~$60–140/month** |

### Cost per 1,000 Orders

At 10 req/s sustained (864,000 orders/day):
- Lambda: ~$0.0002 per 1K invocations (256MB, 200ms avg)
- DynamoDB: ~$0.0013 per 1K writes (1 WCU per UpdateItem)
- Kinesis: fixed ~$0.015/hour regardless of volume (shard-hours dominate)
- **Variable cost per 1,000 orders**: ~$0.002–0.005 ✓ (well under $0.10 target)

**Dominant fixed cost**: ElastiCache Redis (~$25–50/month regardless of traffic). At low volume, Redis is the most expensive component per-request. This is the correct trade-off: Redis enables sub-millisecond GEORADIUS queries that DynamoDB cannot match.
