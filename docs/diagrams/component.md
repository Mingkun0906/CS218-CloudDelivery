# Component Diagram — CloudDelivery

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  «component»                    «component»                                  │
│   OrderAPI                       CourierAPI                                  │
│  ─────────────────               ──────────────────                          │
│  POST /orders                    PUT  /couriers/{id}/location                │
│  GET  /orders/{id}               GET  /couriers/{id}/assignment              │
│  DELETE /orders/{id}             PUT  /couriers/{id}/assignment/accept       │
│                                  PUT  /couriers/{id}/assignment/reject       │
│  provided:                       PUT  /couriers/{id}/assignment/status       │
│    REST over API Gateway                                                     │
│    JWT (Cognito) auth            provided:                                   │
│                                    REST over API Gateway                     │
│  uses:                             JWT (Cognito) auth                        │
│    OrderStore (DynamoDB)                                                     │
│    OrderStream (Kinesis put)     uses:                                       │
│                                    OrderStore (DynamoDB)                     │
└────────────────────────────────    LocationStream (Kinesis put)              │
         │ Kinesis put             ──────────────────────────────────────────  │
         │ ORDER_PLACED                      │ Kinesis put                     │
         ▼                                   │ LOCATION_UPDATE                 │
┌──────────────────────────────┐            ▼                                 │
│  «component»                 │  ┌──────────────────────────────┐            │
│   MatchingLambda             │  │  «component»                 │            │
│  ────────────────────────    │  │   LocationLambda             │            │
│  Trigger: Kinesis orders     │  │  ──────────────────────────  │            │
│  Batch:   1 record           │  │  Trigger: Kinesis locations  │            │
│                              │  │  Batch:   10 records         │            │
│  1. Decode ORDER_PLACED      │  │                              │            │
│  2. Validate payload         │  │  1. Decode LOCATION_UPDATE   │            │
│  3. GEOSEARCH Redis          │  │  2. Validate lat/lng         │            │
│  4. Conditional DynamoDB     │  │  3. GEOADD Redis (geo-index) │            │
│     write (FL-01 guard)      │  │  4. SET TTL sentinel key     │            │
│  5. Publish COURIER_ASSIGNED │  │     (30s expiry)             │            │
│  6. On failure: retry queue  │  │                              │            │
│                              │  │  uses:                       │            │
│  uses:                       │  │    GeoIndex (Redis)          │            │
│    GeoIndex (Redis)          │  └──────────────────────────────┘            │
│    OrderStore (DynamoDB)     │                                               │
│    MatchTopic (SNS publish)  │                                               │
│    RetryQueue (SQS send)     │                                               │
└──────────────┬───────────────┘                                               │
               │ SNS publish                                                   │
               │ COURIER_ASSIGNED                                              │
               ▼                                                               │
┌──────────────────────────────┐                                               │
│  «component»                 │                                               │
│   NotificationBus (SNS)      │                                               │
│  ──────────────────────────  │                                               │
│  Topic: match-notifications  │                                               │
│  Fanout → SQS subscription   │                                               │
└──────────────┬───────────────┘                                               │
               │ SQS fanout                                                    │
               ▼                                                               │
┌──────────────────────────────┐    ┌──────────────────────────────┐          │
│  «component»                 │    │  «component»                 │          │
│   DeliveryQueue (SQS)        │    │   RetryQueue (SQS)           │          │
│  ──────────────────────────  │    │  ──────────────────────────  │          │
│  notifications queue         │    │  retry queue (30s delay)     │          │
│  DLQ: notifications-dlq      │    │  Trigger: RetryLambda        │          │
│  maxReceiveCount: 3          │    └──────────────┬───────────────┘          │
└──────────────┬───────────────┘                   │                          │
               │ SQS trigger                       │ SQS trigger              │
               ▼                                   ▼                          │
┌──────────────────────────────┐    ┌──────────────────────────────┐          │
│  «component»                 │    │  «component»                 │          │
│   NotificationLambda         │    │   RetryLambda                │          │
│  ──────────────────────────  │    │  ──────────────────────────  │          │
│  Delivers push notifications │    │  Re-injects unmatched orders │          │
│  to courier and customer     │    │  back to Kinesis as          │          │
│                              │    │  ORDER_PLACED events         │          │
│  uses:                       │    │                              │          │
│    OrderStore (DynamoDB read)│    │  uses:                       │          │
│                              │    │    OrderStream (Kinesis put) │          │
│  on failure → DLQ            │    └──────────────────────────────┘          │
└──────────────────────────────┘                                               │
                                                                               │
┌──────────────────────────────────────────────────────────────────────────┐  │
│  Shared Data Stores                                                       │  │
│                                                                           │  │
│  «datastore»  GeoIndex (Redis)          «datastore»  OrderStore (DynamoDB│  │
│  ──────────────────────────────         ──────────────────────────────── │  │
│  GEOADD couriers:locations              PK=ORDER#{id}  SK=METADATA        │  │
│  GEOSEARCH radius query                 PK=COURIER#{id} SK=ASSIGNMENT     │  │
│  SET courier:ttl:{id} (30s TTL)         GSI1: by customerId               │  │
│                                         GSI2: by restaurantId             │  │
│                                         GSI3: by courierId/status         │  │
└──────────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────────┘
```
