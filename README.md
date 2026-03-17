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

- Cognito User Pool + App Client (OAuth)
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

### Option 1: CDK + Docker (recommended for production)

```bash
# Requires Docker running — bundles agent code with ARM64 dependencies in a container
cd cdk
pip install -r requirements.txt
cdk deploy
```

### Option 2: CDK + agentcore deploy (if Docker is unavailable)

CDK deploys all infrastructure. Agent code is deployed separately using the AgentCore CLI, which handles dependency bundling internally.

```bash
# Step 1: Deploy infrastructure
cd cdk
pip install -r requirements.txt
cdk deploy

# Step 2: Deploy agent code (after CDK completes)
cd ../agent
pip install -r requirements.txt
agentcore deploy --auto-update-on-conflict
```

Note: with Option 2, every `cdk deploy` will overwrite the runtime with a non-bundled asset, breaking the agent. You must re-run `agentcore deploy --auto-update-on-conflict` after each `cdk deploy`.

## Post-Deploy Setup

### Create demo users

```bash
POOL_ID=$(aws cloudformation describe-stacks --stack-name ProductionAgentGuardrailsStack \
  --query 'Stacks[0].Outputs[?OutputKey==`UserPoolId`].OutputValue' --output text)

aws cognito-idp admin-create-user --user-pool-id $POOL_ID --username john.smith \
  --user-attributes Name=email,Value=john.smith@example.com Name=custom:customer_id,Value=12345 \
  --temporary-password <PASSWORDHERE> --message-action SUPPRESS

aws cognito-idp admin-set-user-password --user-pool-id $POOL_ID --username john.smith \
  --password <PASSWORDHERE>  --permanent

aws cognito-idp admin-create-user --user-pool-id $POOL_ID --username sarah.johnson \
  --user-attributes Name=email,Value=sarah.johnson@example.com Name=custom:customer_id,Value=67890 \
  --temporary-password <PASSWORDHERE>  --message-action SUPPRESS

aws cognito-idp admin-set-user-password --user-pool-id $POOL_ID --username sarah.johnson \
  --password <PASSWORDHERE>  --permanent
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

## Troubleshooting

### Agent returns errors / no logs in CloudWatch

The AgentCore Runtime has a 30-second init timeout. If the agent code isn't properly bundled with dependencies, it fails silently. Fix: redeploy agent code with `agentcore deploy --auto-update-on-conflict` from the `agent/` directory.

### Docker auth required / Docker not running

The CDK Docker bundling requires Docker Desktop with a valid license. If unavailable, use Option 2 (CDK + agentcore deploy). The `agentcore` CLI handles bundling internally without Docker.

### Cedar policy CREATE_FAILED "Overly Restrictive"

The forbid policy must be created after the corresponding permit policy. The CDK stack has explicit `add_dependency` to enforce ordering. If deploying from scratch and this fails, delete the failed policies and redeploy.

### Gateway returns 403 on tool calls

Check the Cedar policies are all ACTIVE:
```bash
aws bedrock-agentcore-control list-policies \
  --policy-engine-id <policy-engine-id> \
  --query 'policies[].{name:name,status:status}' --output table
```

### Memory poisoning (guardrail-blocked messages replay)

If a previous guardrail block gets stored in memory, it can poison subsequent turns in the same session. Fix: use a new session ID for each test. In production, implement selective memory writes to avoid persisting blocked responses.

### Fresh account deployment

1. `cdk bootstrap` (one-time)
2. `cdk deploy`
3. Run post-deploy setup (create demo users, seed DynamoDB)
4. Enable CloudWatch Transaction Search (one-time, for observability)
5. If using Option 2: `cd agent && agentcore deploy --auto-update-on-conflict`
