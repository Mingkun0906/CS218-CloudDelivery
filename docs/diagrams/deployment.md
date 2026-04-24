# Deployment Diagram — CloudDelivery

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              PUBLIC TRUST ZONE                                  │
│                                                                                 │
│   Customer / Courier App                                                        │
│          │  HTTPS                                                               │
│          ▼                                                                      │
│   ┌─────────────────────┐    Cognito JWT validation                            │
│   │   API Gateway (REST) │◄──────────────────────────┐                         │
│   │  clouddelivery-api   │                           │                         │
│   └──────────┬──────────┘               ┌────────────┴────────┐               │
│              │                          │  Amazon Cognito      │               │
│              │ Lambda proxy             │  clouddelivery-users │               │
│              │                          └─────────────────────┘               │
└──────────────┼──────────────────────────────────────────────────────────────────┘
               │ VPC endpoint / public invoke
               ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│              PRIVATE VPC  vpc-022347f78ef0a3b2c  (us-east-1)                   │
│                                                                                 │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  Lambda Subnet Group  (us-east-1a: subnet-01aa..  us-east-1b: subnet-02..)  │
│  │  Security Group: clouddelivery-lambda-sg (sg-026d9252cc3d23c4c)         │   │
│  │                                                                          │   │
│  │  ┌────────────────┐  ┌──────────────────┐  ┌──────────────────────┐    │   │
│  │  │  OrderAPI       │  │  CourierAPI       │  │  MatchingLambda      │    │   │
│  │  │  Lambda         │  │  Lambda           │  │  (Kinesis trigger)   │    │   │
│  │  │  256MB / 30s    │  │  256MB / 30s      │  │  256MB / 30s         │    │   │
│  │  └───────┬────────┘  └────────┬──────────┘  └──────────┬───────────┘    │   │
│  │          │                    │                          │                │   │
│  │  ┌────────────────┐  ┌──────────────────┐  ┌──────────────────────┐    │   │
│  │  │  LocationLambda │  │ NotificationLambda│  │  RetryLambda         │    │   │
│  │  │  (Kinesis trg.) │  │  (SQS trigger)   │  │  (SQS trigger)       │    │   │
│  │  │  256MB / 30s    │  │  256MB / 30s      │  │  256MB / 30s         │    │   │
│  │  └───────┬────────┘  └──────────────────┘  └──────────────────────┘    │   │
│  └──────────┼──────────────────────────────────────────────────────────────┘   │
│             │                                                                   │
│  ┌──────────┴──────────────────────────────────────────────────────────────┐   │
│  │  Shared Data / Messaging Layer                                           │   │
│  │                                                                          │   │
│  │  ┌──────────────────────┐    ┌──────────────────────────────────────┐   │   │
│  │  │  ElastiCache Redis    │    │  Amazon DynamoDB                     │   │   │
│  │  │  (Multi-AZ)           │    │  clouddelivery-dev  (on-demand)      │   │   │
│  │  │  t3.micro primary     │    │  ← VPC Gateway Endpoint              │   │   │
│  │  │  t3.micro replica     │    │  PK/SK single-table + 3 GSIs         │   │   │
│  │  │  Port 6379            │    └──────────────────────────────────────┘   │   │
│  │  │  SG: Lambda SG only   │                                               │   │
│  │  └──────────────────────┘    ┌──────────────────────────────────────┐   │   │
│  │                               │  Amazon Kinesis                      │   │   │
│  │  ┌──────────────────────┐    │  orders stream   (1 shard)           │   │   │
│  │  │  Amazon SNS           │    │  locations stream (1 shard)          │   │   │
│  │  │  match-notifications  │    │  ← VPC Interface Endpoint            │   │   │
│  │  │  ← VPC Interface EP   │    └──────────────────────────────────────┘   │   │
│  │  └──────────────────────┘                                               │   │
│  │                               ┌──────────────────────────────────────┐   │   │
│  │  ┌──────────────────────┐    │  Amazon SQS                          │   │   │
│  │  │  CloudWatch           │    │  notifications queue                 │   │   │
│  │  │  ← VPC Interface EP   │    │  retry queue                         │   │   │
│  │  │  Namespace:           │    │  notifications-dlq                   │   │   │
│  │  │  CloudDelivery/dev    │    │  ← VPC Interface Endpoint            │   │   │
│  │  └──────────────────────┘    └──────────────────────────────────────┘   │   │
│  └──────────────────────────────────────────────────────────────────────────┘   │
│                                                                                 │
│  Route Tables:  Lambda subnets → DynamoDB via prefix list (pl-63a5400a)        │
│  NACLs:         Deny all inbound from 0.0.0.0/0 on Lambda subnets              │
└─────────────────────────────────────────────────────────────────────────────────┘

Observability (cross-cutting):
  All Lambdas → X-Ray (Active tracing, subsegments per Redis/DynamoDB/SNS call)
  All Lambdas → CloudWatch Logs (/aws/lambda/clouddelivery-{fn}-dev)
  CloudWatch Alarms → SNS clouddelivery-alarms-dev → email
```
