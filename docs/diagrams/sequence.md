# Sequence Diagram — Order Matching Flow

```
Customer   API Gateway   Cognito   OrderAPI    Kinesis      MatchingLambda   Redis        DynamoDB     SNS
   │            │            │         │       (orders)           │             │              │          │
   │──POST /orders──────────►│         │           │              │             │              │          │
   │            │──verify JWT►│         │           │              │             │              │          │
   │            │◄──claims────│         │           │              │             │              │          │
   │            │─────────────────────►│           │              │             │              │          │
   │            │             │        │──put_item─────────────────────────────►│              │          │
   │            │             │        │  ORDER#{id} status=placed │             │              │          │
   │            │             │        │◄──success─────────────────────────────│              │          │
   │            │             │        │──put_record──────────────►│            │              │          │
   │            │             │        │  ORDER_PLACED event       │            │              │          │
   │            │             │        │◄──ShardId/SeqNum──────────│            │              │          │
   │◄───202 Accepted──────────────────│           │              │             │              │          │
   │  {orderId, status:placed}│        │           │              │             │              │          │
   │            │             │        │           │              │             │              │          │
   │            │             │        │    [Kinesis triggers Lambda, ~1-3s]    │              │          │
   │            │             │        │           │──invoke──────►│            │              │          │
   │            │             │        │           │              │──GEOSEARCH──►│             │          │
   │            │             │        │           │              │  radius=5km  │             │          │
   │            │             │        │           │              │◄──[courier1,2,3]────────────│         │
   │            │             │        │           │              │             │              │          │
   │            │             │        │           │              │  [for each courier]        │          │
   │            │             │        │           │              │──EXISTS courier:ttl:{id}──►│          │
   │            │             │        │           │              │◄──1 (TTL alive)────────────│          │
   │            │             │        │           │              │             │              │          │
   │            │             │        │           │              │──update_item────────────────────────► │
   │            │             │        │           │              │  SET courierId=X            │          │
   │            │             │        │           │              │  CONDITION: no courierId    │          │
   │            │             │        │           │              │  AND status=placed          │          │
   │            │             │        │           │              │◄──200 OK (assigned)─────────────────── │
   │            │             │        │           │              │             │              │          │
   │            │             │        │           │              │──publish COURIER_ASSIGNED──────────────►│
   │            │             │        │           │              │  {orderId, courierId,       │          │
   │            │             │        │           │              │   customerId, restaurantId} │          │
   │            │             │        │           │              │◄──MessageId────────────────────────────│
   │            │             │        │           │              │             │              │          │
   │            │             │        │           │◄──{batchItemFailures:[]}───│             │          │
   │            │             │        │           │                            │             │          │

[Concurrent double-assignment attempt — FL-01]:
   │            │             │        │           │              │             │              │          │
   │            │             │        │           │──invoke──────►│ (2nd Lambda│              │          │
   │            │             │        │           │              │ same order) │              │          │
   │            │             │        │           │              │──update_item───────────────────────────►│
   │            │             │        │           │              │  CONDITION fails:           │          │
   │            │             │        │           │              │  courierId already exists   │          │
   │            │             │        │           │              │◄──ConditionalCheckFailed────────────────│
   │            │             │        │           │              │  (try next courier or abort)│          │

[No courier found — retry flow]:
   │            │             │        │           │              │             │              │          │
   │            │             │        │           │              │──update_item (retryCount++) ──────────►│
   │            │             │        │           │              │  status=unassigned          │          │
   │            │             │        │           │              │──SQS send (30s delay)──────────────────►│
   │            │             │        │           │  [after 30s RetryLambda re-injects ORDER_PLACED]     │
```

## Key Design Decisions Visible in This Flow

1. **DynamoDB write before Kinesis put** — order exists in DB before Lambda can process it; no race on item existence.
2. **Conditional write as FL-01 guard** — `attribute_not_exists(courierId)` prevents two Lambdas assigning the same order even if triggered concurrently.
3. **Redis TTL sentinel check** — `EXISTS courier:ttl:{id}` before DynamoDB write guards against stale geo-index entries (E-17).
4. **Kinesis partial batch failure** — Lambda returns `batchItemFailures` so Kinesis only retries failed records, not the whole batch.
5. **SQS 30s delay** — retry queue message is invisible for 30s, giving couriers time to become available before re-matching.
