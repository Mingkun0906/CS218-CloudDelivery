# CloudDelivery — Test Cases

Covers all required categories: functional, scaling, failure, security, performance, and edge cases.

---

## Category 1: Functional Tests

| ID | Test Case | Input | Expected Output | Pass Criteria |
|----|-----------|-------|-----------------|---------------|
| F-01 | End-to-end order placement | Valid customer JWT + order payload (location, restaurant ID) | Courier assigned, DynamoDB order record updated, SNS notification sent | HTTP 200, assignment record in DynamoDB within 500ms, SNS event delivered |
| F-02 | Courier location registration | Courier ID + GPS coordinates (lat/lng) | Redis GEOADD succeeds, TTL = 30s set | GEORADIUS returns courier within 1km of submitted location |
| F-03 | Courier location expiry | Courier submits location, waits 31s | Courier no longer in geo-index | GEORADIUS returns empty after TTL expiry; courier not assigned to new orders |
| F-04 | Restaurant order confirmation | Restaurant confirms assigned order | DynamoDB status: `assigned → confirmed → preparing` | Status transition correct; customer receives SNS notification |
| F-05 | Full order lifecycle | Order placed → courier assigned → restaurant confirms → order ready → delivered | Status chain: `placed → assigned → confirmed → preparing → ready → delivered` | All 6 status transitions recorded in DynamoDB with timestamps |
| F-06 | Courier accept/reject | Courier receives assignment; rejects via API | System reassigns to next-best courier | New assignment within 2s; original courier marked available |
| F-07 | Multiple concurrent orders | 10 simultaneous order requests from different locations | Each order gets a distinct courier | No two orders share same courier; all 10 assignments in DynamoDB |
| F-08 | JWT authentication — valid token | Valid Cognito JWT in Authorization header | Request processed normally | HTTP 200 |
| F-09 | Idempotent order submission | Same order ID submitted twice within 5s | Second request returns existing assignment without creating a duplicate | DynamoDB has exactly 1 record per order ID |

---

## Category 2: Scaling Tests

| ID | Test Case | Input | Expected Output | Pass Criteria |
|----|-----------|-------|-----------------|---------------|
| S-01 | Linear load ramp | Locust: 1 → 100 req/s over 10 min | Lambda concurrency scales proportionally; latency remains within SLA | p95 ≤ 400ms throughout ramp; no 5xx errors |
| S-02 | Sustained peak load | 100 req/s for 30 min | Stable throughput; no memory/timeout errors | Assignment success rate ≥ 99%; DLQ depth stays at 0 |
| S-03 | Burst spike | Instant jump from 5 → 50 req/s | Lambda cold starts absorbed; brief p99 spike then recovery | p99 recovers to ≤ 500ms within 60s of spike |
| S-04 | Lambda concurrency scaling delay | Ramp from 1 → 50 concurrent invocations | Measure time from first request to full concurrency reached | Scaling delay captured via CloudWatch `ConcurrentExecutions` metric; target ≤ 30s |
| S-05 | Kinesis consumer lag under load | 100 req/s sustained for 10 min | Kinesis `GetRecords.IteratorAgeMilliseconds` stays low | Consumer lag ≤ 5,000ms (5s) |
| S-06 | Redis geo-index under high courier density | 1,000 simulated courier locations in single city area | GEORADIUS queries return correct nearest courier | Query latency ≤ 5ms at p99 |
| S-07 | DynamoDB write throughput | 100 conditional writes/s | All writes succeed or fail with ConditionalCheckFailedException | No throttling exceptions; on-demand scaling absorbs load within 30s |
| S-08 | Load removal / scale-down | Drop from 100 → 1 req/s | Lambda concurrency and Kinesis shards scale back | CloudWatch shows concurrency drop within 5 min; cost normalizes |

---

## Category 3: Failure Tests

| ID | Test Case | Failure Injected | Expected Behavior | Recovery Evidence |
|----|-----------|-----------------|-------------------|-------------------|
| FL-01 | Double courier assignment | Two Lambda instances invoked simultaneously for same order | Second write throws `ConditionalCheckFailedException`; retries with next-best courier | Zero duplicate assignments in DynamoDB; X-Ray trace shows retry path |
| FL-02 | Lambda cold start surge | 10x request spike from idle | p99 spikes to 200–500ms; no data loss (Kinesis retains) | CloudWatch `InitDuration` metric captured; Kinesis backlog clears within 60s |
| FL-03 | Redis primary node failure | Kill ElastiCache primary via AWS console | Replica promotes (~15–30s); geo-queries fail temporarily; Kinesis retries unacknowledged records | CloudWatch `ReplicationLag` shows failover; system resumes after promotion |
| FL-04 | DynamoDB throttling | Artificially limit table capacity; submit burst of writes | Throttled requests caught by retry logic with exponential backoff | No data loss; all orders eventually written; DLQ depth stays 0 |
| FL-05 | Lambda timeout / crash | Set Lambda timeout to 100ms intentionally | Kinesis triggers retry; message processed on re-invocation | No stuck orders; CloudWatch `Errors` metric shows spike then clearance |
| FL-06 | SNS/SQS delivery failure | Block SQS endpoint via VPC policy | Message goes to DLQ; alert fires | DLQ depth > 0 triggers CloudWatch alarm; SQS redrive recovers all messages |
| FL-07 | Kinesis shard iterator expiry | Pause Lambda consumer for > 24h (simulated via code) | GetRecords fails with `ExpiredIteratorException`; consumer restarts from latest checkpoint | No unrecoverable data loss if within Kinesis 24h retention window |
| FL-08 | API Gateway timeout | Downstream Lambda deliberately delayed > 29s | API Gateway returns HTTP 504; client receives error | CloudWatch shows 504 spike; no orphaned DynamoDB records |
| FL-09 | Cognito outage simulation | Disable Cognito user pool temporarily | All API requests return HTTP 401/403 | System degrades gracefully; no partial orders created; Cognito re-enabled restores access |
| FL-10 | Cascading failure (Redis + DynamoDB throttle simultaneously) | Kill Redis + throttle DynamoDB at same time | System returns HTTP 503; DLQ accumulates; no data corruption | After both services recover, DLQ redrive processes all queued orders correctly |

---

## Category 4: Security Tests

| ID | Test Case | Input | Expected Output | Pass Criteria |
|----|-----------|-------|-----------------|---------------|
| SC-01 | Unauthenticated API access | Request without Authorization header | HTTP 401 Unauthorized | API Gateway Cognito authorizer rejects; no Lambda invoked |
| SC-02 | Expired JWT token | Token with `exp` in the past | HTTP 401 Unauthorized | Cognito authorizer rejects before Lambda invocation |
| SC-03 | Invalid JWT signature | Token with tampered payload | HTTP 401 Unauthorized | Authorizer detects signature mismatch |
| SC-04 | Courier accessing another courier's data | Courier A's JWT used to query Courier B's assignments | HTTP 403 Forbidden | Lambda IAM policy or application-level check blocks cross-user access |
| SC-05 | Direct Redis access outside VPC | Attempt TCP connection to Redis endpoint from public internet | Connection refused / timeout | Security group allows only Lambda VPC traffic on port 6379 |
| SC-06 | Direct DynamoDB access with Lambda role from outside | Attempt to assume Lambda execution role from external AWS account | Access denied | IAM trust policy restricts role assumption to Lambda service principal only |
| SC-07 | Overprivileged IAM role test | Try to call S3, EC2, or other unrelated APIs using Lambda role | Access denied for all non-required services | Lambda role has no permissions beyond: DynamoDB (specific table), ElastiCache, Kinesis, SNS, CloudWatch |
| SC-08 | VPC isolation — DynamoDB | Verify DynamoDB accessed via VPC endpoint, not public internet | No traffic exits VPC for DynamoDB calls | VPC Flow Logs show no outbound traffic to DynamoDB public endpoint |
| SC-09 | SQL/NoSQL injection in order payload | Malformed JSON or DynamoDB expression syntax in order fields | HTTP 400 Bad Request; no DynamoDB write | Input validation in Lambda rejects before any DB call |
| SC-10 | Kinesis stream access control | Unauthorized IAM user attempts PutRecord to order stream | Access denied | Kinesis resource policy + IAM deny blocks external producers |

---

## Category 5: Performance Tests

| ID | Test Case | Measurement Method | Target | Metric Source |
|----|-----------|-------------------|--------|---------------|
| P-01 | End-to-end matching latency at 10 req/s | Locust response time + X-Ray trace | p50 ≤ 200ms, p95 ≤ 350ms, p99 ≤ 500ms | X-Ray service map, CloudWatch |
| P-02 | End-to-end matching latency at 100 req/s | Same as P-01 under sustained load | p50 ≤ 250ms, p95 ≤ 450ms, p99 ≤ 700ms | X-Ray service map, CloudWatch |
| P-03 | Lambda cold start overhead | CloudWatch `InitDuration` metric across 1,000 invocations | Cold start ≤ 500ms; warm p99 ≤ 50ms | CloudWatch Lambda Insights |
| P-04 | Redis GEORADIUS query latency | X-Ray subsegment timing for Redis call | p99 ≤ 5ms | X-Ray custom subsegments |
| P-05 | DynamoDB conditional write latency | X-Ray subsegment for DynamoDB call | p99 ≤ 10ms | X-Ray custom subsegments |
| P-06 | Kinesis consumer lag | IteratorAgeMilliseconds metric | ≤ 5,000ms at 100 req/s | CloudWatch |
| P-07 | Throughput ceiling | Increase Locust users until first 5xx error | Document max sustained req/s before degradation | Locust failure rate chart |
| P-08 | DLQ recovery throughput | Fill DLQ with 1,000 failed messages; trigger redrive | All messages processed within 5 min | CloudWatch SQS ApproximateNumberOfMessagesVisible |
| P-09 | Assignment success rate | Ratio of successful assignments / total orders at 100 req/s | ≥ 99% | Custom CloudWatch metric emitted by Lambda |
| P-10 | Cost per 1,000 orders | AWS Cost Explorer during sustained 100 req/s run | ≤ $0.10 per 1,000 orders | AWS Cost Explorer + billing data |

---

## Category 6: Edge Case Tests

| ID | Test Case | Scenario | Expected Behavior | Notes |
|----|-----------|----------|-------------------|-------|
| E-01 | No couriers available in radius | Order placed; zero couriers within 5km | HTTP 200 with status `unassigned`; order queued for retry | Retry logic re-checks every 30s |
| E-02 | Courier disappears during matching | Courier TTL expires between GEORADIUS query and DynamoDB write | `ConditionalCheckFailedException`; retry with next courier | Redis TTL race condition — system must handle gracefully |
| E-03 | All couriers busy simultaneously | 50 active orders, 50 couriers all assigned | New orders return `queued`; assigned as couriers complete deliveries | DynamoDB courier status `busy` flag controls availability |
| E-04 | Zero-distance courier | Courier GPS exactly matches restaurant location | Assignment succeeds; no divide-by-zero or geo-query error | GEORADIUS handles 0m distance |
| E-05 | Invalid GPS coordinates | lat=200, lng=500 submitted by courier | HTTP 400 Bad Request; location not written to Redis | Input validation layer rejects out-of-range values |
| E-06 | Extremely large search radius | GEORADIUS query with radius = 10,000 km | Returns all available couriers; system selects nearest | No crash; performance may degrade — log and alert if > 100 results |
| E-07 | Duplicate order ID (replay attack) | Same order ID submitted twice within 5s | Second request returns existing assignment; no new record created | DynamoDB conditional put with `attribute_not_exists(orderId)` |
| E-08 | Restaurant confirmation timeout | Restaurant doesn't confirm within 10 min | Order status → `restaurant_unresponsive`; customer notified via SNS | TTL-based DynamoDB stream triggers Lambda cleanup function |
| E-09 | Courier cancels mid-delivery | Courier marks delivery cancelled after assignment | Order status → `reassigning`; new courier lookup initiated | System must not leave order in permanently `assigned` state |
| E-10 | Simultaneous restaurant + courier failure | Restaurant goes offline AND courier cancels at same time | Order falls back to `failed`; customer receives refund notification | Both failure paths handled independently; no deadlock |
| E-11 | GPS spam (high-frequency location updates) | Courier submits 10 location updates/s | Redis GEOADD overwrites previous; no data corruption; Kinesis handles burst | Kinesis shard capacity limits burst; excess messages queued |
| E-12 | Malformed JSON payload | Missing required field (e.g., `customerId`) | HTTP 400; no Lambda execution beyond validation | API Gateway request validator rejects before invocation |
| E-13 | Empty order (no items) | Order with empty item list | HTTP 400 | Application-level validation catches empty orders |
| E-14 | Kinesis shard hot partition | All courier GPS from one city on same shard key | Shard throughput limit hit; Kinesis throttles PutRecord | Use courier ID as partition key to distribute load; test resharding |
| E-15 | Order during Redis failover window | Order placed during 15–30s Redis promotion window | Lambda catches Redis connection error; returns HTTP 503; Kinesis retains event for retry | Error handling must distinguish Redis unavailable from Redis empty |
| E-16 | Very large payload | Order with 10,000-character special instructions field | HTTP 413 or trimmed at validation; no DynamoDB write failure | DynamoDB item size limit is 400KB; validate before write |
| E-17 | Courier with stale location (just after TTL) | Courier's Redis entry expires at exact moment of GEORADIUS query | Query returns empty; order queued as if no courier available | Race window handled identically to E-01 |
| E-18 | Clock skew between services | JWT issued with future `iat` due to courier device clock skew | JWT validation rejects token with `iat` > current time + 5min tolerance | Cognito handles via configurable clock tolerance setting |
