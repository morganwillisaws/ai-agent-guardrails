# Production AI Agent with Layered Safety Controls

Production-ready AI agent reference architecture for AnyCompany Robotics customer service.
Demonstrates layered safety controls where failures are prevented by architecture, not model behavior.

> "You cannot prompt-engineer your way out of this. You fix it with architecture."

## Architecture

```
Customer → Cognito (OAuth) → API Gateway (REST) → AgentCore Runtime (CUSTOM_JWT)
                                                        ↓
                                                  Bedrock Guardrails
                                                  Steering Handler
                                                        ↓
                                                  AgentCore Gateway (CUSTOM_JWT)
                                                   ↓          ↓
                                            Interceptor    Cedar Policy
                                            (JWT → ID)    (amount limits)
                                                   ↓
                                              Tool Lambdas
                                              (ownership checks)
                                                   ↓
                                            AgentCore Memory
```

## Safety Layers

| Layer | What it does | What it catches |
|-------|-------------|-----------------|
| Cognito + CUSTOM_JWT | OAuth authentication end-to-end | Unauthenticated access |
| WAF | Rate limiting, common exploits | DDoS, injection patterns |
| Bedrock Guardrails | Content filtering, PII anonymization, topic denial | Prompt attacks, off-topic requests, credit card/SSN leaks |
| Steering Handler | Reviews agent responses before delivery | Unconfirmed promises, leaked system details, hallucinated contact info |
| Gateway Interceptor | Extracts `custom:customer_id` from JWT, injects into tool args | Customer ID spoofing — agent never controls identity |
| Cedar Policy Engine | Declarative authorization on tool calls | Return labels for orders over $500 (must escalate to human) |
| Tool-Level Ownership | Each Lambda verifies `_authenticated_customer_id` against order data | Cross-customer data access |

## Structure

```
├── agent/                              # Strands agent (bundled + deployed via CDK)
│   ├── agent.py                        # Main agent entrypoint
│   ├── steering.py                     # Response reviewer (SteeringHandler plugin)
│   └── requirements.txt                # Dependencies (bundled via Docker at deploy time)
├── cdk/                                # CDK infrastructure (everything)
│   ├── app.py
│   ├── stacks/main_stack.py            # Full stack definition
│   ├── scripts/create_zip.py           # Agent code bundling script (used by Docker)
│   └── lambdas/
│       ├── gateway_custom_resource/    # Bundled boto3 + policy engine attach handler
│       └── gateway_interceptor/        # JWT → customer_id injection
├── tool-lambdas/                       # Tool Lambdas (invoked via AgentCore Gateway)
│   ├── order-lookup/                   # Order details with ownership check
│   ├── warranty-lookup/                # Warranty status with ownership check
│   ├── return-label-generator/         # Generates return label, verifies amount + ownership
│   ├── check-return-eligibility/       # 30-day return window check with ownership
│   ├── company-policy-lookup/          # Bedrock Knowledge Base RAG lookup
│   └── create-case/                    # Escalation case creation (customer_id from JWT)
└── frontend/                           # Single-page app with Cognito auth
    ├── index.html
    └── server.py                       # Local dev server (port 8080)
```

## What CDK Deploys

Everything is managed by `cdk deploy` — a single command deploys the full stack:

- Cognito User Pool + App Client (OAuth authorization code flow)
- REST API Gateway with Cognito authorizer + CORS
- WAF (rate limiting + AWS managed rules)
- AgentCore Runtime with Docker-bundled agent code (dependencies installed at deploy time)
- AgentCore Gateway with CUSTOM_JWT auth + interceptor
- 6 Gateway Targets (native `CfnGatewayTarget` L1 constructs)
- Cedar Policy Engine + 3 policies (native `CfnPolicyEngine` + `CfnPolicy`)
- Policy Engine → Gateway attachment (tiny custom resource — only CFN gap)
- Bedrock Guardrails (content filters, PII anonymization, topic denial)
- AgentCore Memory
- 6 Tool Lambdas with scoped IAM permissions
- Gateway Interceptor Lambda
- DynamoDB tables (Orders, Refunds, Cases)
- S3 buckets (return labels, transcripts)
- SSM parameters for agent config
- AgentCore Observability (auto-instrumented via `aws-opentelemetry-distro`)

## What's NOT in CDK (manual setup)

- Demo users in Cognito (`john.smith`, `sarah.johnson`) — create via CLI after deploy
- Sample data in DynamoDB (orders) — seed via CLI or script after deploy
- CloudWatch Transaction Search — one-time account-level enablement for observability

## Prerequisites

- AWS account with Bedrock model access enabled
- Docker running (required for agent code bundling)
- Python 3.11+
- Node.js 18+ (for CDK)
- CDK bootstrapped (`cdk bootstrap`)

## Deploy

```bash
# Ensure Docker is running
cd cdk
pip install -r requirements.txt
cdk deploy
```

That's it. No separate `agentcore deploy` needed — CDK bundles the agent code with all dependencies using Docker and deploys it directly to the AgentCore Runtime.

## Post-Deploy Setup

### Create demo users

```bash
POOL_ID=$(aws cloudformation describe-stacks --stack-name ProductionAgentGuardrailsStack \
  --query 'Stacks[0].Outputs[?OutputKey==`UserPoolId`].OutputValue' --output text)

aws cognito-idp admin-create-user --user-pool-id $POOL_ID --username john.smith \
  --user-attributes Name=email,Value=john.smith@example.com Name=custom:customer_id,Value=12345 \
  --temporary-password TempPass123! --message-action SUPPRESS

aws cognito-idp admin-set-user-password --user-pool-id $POOL_ID --username john.smith \
  --password DemoPass123! --permanent

aws cognito-idp admin-create-user --user-pool-id $POOL_ID --username sarah.johnson \
  --user-attributes Name=email,Value=sarah.johnson@example.com Name=custom:customer_id,Value=67890 \
  --temporary-password TempPass123! --message-action SUPPRESS

aws cognito-idp admin-set-user-password --user-pool-id $POOL_ID --username sarah.johnson \
  --password DemoPass123! --permanent
```

### Seed sample orders

```bash
aws dynamodb put-item --table-name Orders --item '{
  "orderId":{"N":"12345"},"customer_id":{"S":"12345"},"customerName":{"S":"John Smith"},
  "email":{"S":"john.smith@example.com"},"product":{"S":"RoboVac Pro X1"},
  "total":{"N":"249.99"},"purchaseDate":{"S":"2026-02-15"},
  "deliveryDate":{"S":"2026-02-18"},"shippingStatus":{"S":"delivered"},
  "warrantyEligible":{"BOOL":true}}'

aws dynamodb put-item --table-name Orders --item '{
  "orderId":{"N":"67890"},"customer_id":{"S":"67890"},"customerName":{"S":"Sarah Johnson"},
  "email":{"S":"sarah.johnson@example.com"},"product":{"S":"RoboVac Lite"},
  "total":{"N":"349.99"},"purchaseDate":{"S":"2026-01-10"},
  "deliveryDate":{"S":"2026-01-13"},"shippingStatus":{"S":"delivered"},
  "warrantyEligible":{"BOOL":false}}'

aws dynamodb put-item --table-name Orders --item '{
  "orderId":{"N":"99999"},"customer_id":{"S":"12345"},"customerName":{"S":"John Smith"},
  "email":{"S":"john.smith@example.com"},"product":{"S":"RoboVac Ultra Pro Max"},
  "total":{"N":"899.99"},"purchaseDate":{"S":"2026-02-20"},
  "deliveryDate":{"S":"2026-02-23"},"shippingStatus":{"S":"delivered"},
  "warrantyEligible":{"BOOL":true}}'
```

## Frontend

```bash
cd frontend
python server.py
# Open http://localhost:8080
```

## Key Design Decisions

- OAuth (CUSTOM_JWT) all the way through — no IAM auth at the gateway
- No BFF Lambda — API Gateway proxies directly to AgentCore Runtime via HTTP_PROXY
- Single `cdk deploy` — agent code bundled via Docker with ARM64 dependencies
- No direct refund tool — returns generate a shipping label, refund is automatic on receipt
- Cedar policy blocks return labels for orders over $500 (escalated to human review)
- Customer identity flows from JWT through the interceptor — the agent never controls it
- Tools are narrow and deterministic — the tool defines the boundary, not the model
- All IAM permissions scoped to specific resources (no `*` wildcards except where required)
- Observability auto-instrumented via `aws-opentelemetry-distro` + Strands OTEL integration

## IAM Permissions

All roles follow least-privilege:

- Runtime role: scoped to specific Bedrock models, guardrails, SSM parameter namespace (`/robot-vacuum/*`), agentcore memory/gateway resources, and X-Ray
- Tool Lambdas: each gets only the DynamoDB/S3 access it needs (via CDK `grant_*` methods)
- Gateway role: only `lambda:InvokeFunction` on the specific tool Lambdas
- Policy attach CR: only `GetGateway` + `UpdateGateway` on gateways, plus `iam:PassRole` on the gateway role
- Interceptor: no extra permissions (just reads the JWT from the request)
