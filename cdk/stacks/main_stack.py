"""
Production-ready AI Agent with layered safety controls.

Architecture: Cognito → API Gateway (REST, OAuth) → AgentCore Runtime (OAuth)
              → AgentCore Gateway (CUSTOM_JWT, interceptor, Cedar) → Tool Lambdas
              + AgentCore Memory, Bedrock Guardrails, WAF
"""
from aws_cdk import (
    BundlingOptions, CfnOutput, CustomResource, DockerImage, Duration, Fn,
    Stack, RemovalPolicy,
    aws_apigateway as apigw,
    aws_bedrock as bedrock,
    aws_bedrockagentcore as bac,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_s3 as s3,
    aws_s3_assets as s3_assets,
    aws_wafv2 as wafv2,
    custom_resources as cr,
)
from constructs import Construct
import json, os


class ProductionAgentGuardrailsStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        _repo = os.path.join(os.path.dirname(__file__), "..", "..")
        _tools = os.path.join(_repo, "tool-lambdas")
        _lambdas = os.path.join(os.path.dirname(__file__), "..", "lambdas")
        _cognito_issuer = f"https://cognito-idp.{self.region}.amazonaws.com"

        # ── Cognito ───────────────────────────────────────────────────────────

        self.user_pool = cognito.UserPool(self, "UserPool",
            user_pool_name="robot-vacuum-customer-service",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(username=True, email=True),
            custom_attributes={"customer_id": cognito.StringAttribute(mutable=False)},
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.user_pool.add_domain("Domain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=f"robot-vacuum-{self.account}",
            ),
        )
        self.app_client = self.user_pool.add_client("AppClient",
            user_pool_client_name="robot-vacuum-web",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_password=True, admin_user_password=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.PROFILE],
                callback_urls=["http://localhost:8080/callback", "http://localhost:8000/callback", "http://localhost:3000/callback"],
                logout_urls=["http://localhost:8080/", "http://localhost:8000/", "http://localhost:3000/"],
            ),
            id_token_validity=Duration.hours(1),
            access_token_validity=Duration.hours(1),
        )

        # ── Storage ───────────────────────────────────────────────────────────

        self.return_labels_bucket = s3.Bucket(self, "ReturnLabelsBucket",
            bucket_name="robot-vacuum-return-labels",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL, versioned=True,
            removal_policy=RemovalPolicy.DESTROY, auto_delete_objects=True,
            lifecycle_rules=[s3.LifecycleRule(id="Expire30d", enabled=True, expiration=Duration.days(30))],
        )
        self.transcripts_bucket = s3.Bucket(self, "TranscriptsBucket",
            bucket_name="customer-service-transcripts-mw",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL, versioned=True,
            removal_policy=RemovalPolicy.DESTROY, auto_delete_objects=True,
        )
        self.orders_table = dynamodb.Table(self, "OrdersTable",
            table_name="Orders",
            partition_key=dynamodb.Attribute(name="orderId", type=dynamodb.AttributeType.NUMBER),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.refunds_table = dynamodb.Table(self, "RefundsTable",
            table_name="Refunds",
            partition_key=dynamodb.Attribute(name="refund_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.refunds_table.add_global_secondary_index(
            index_name="order_id-index",
            partition_key=dynamodb.Attribute(name="order_id", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )
        self.cases_table = dynamodb.Table(self, "CasesTable",
            table_name="Cases",
            partition_key=dynamodb.Attribute(name="case_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ── Tool Lambdas ─────────────────────────────────────────────────────

        _ld = dict(runtime=lambda_.Runtime.PYTHON_3_12, handler="lambda_function.lambda_handler",
                    timeout=Duration.seconds(30), memory_size=128)

        self.order_lookup_fn = lambda_.Function(self, "OrderLookup", function_name="order-lookup-tool",
            code=lambda_.Code.from_asset(os.path.join(_tools, "order-lookup")),
            environment={"ORDERS_TABLE_NAME": self.orders_table.table_name}, **_ld)
        self.orders_table.grant_read_data(self.order_lookup_fn)

        self.warranty_lookup_fn = lambda_.Function(self, "WarrantyLookup", function_name="warranty-lookup-tool",
            code=lambda_.Code.from_asset(os.path.join(_tools, "warranty-lookup")),
            environment={"ORDERS_TABLE_NAME": self.orders_table.table_name}, **_ld)
        self.orders_table.grant_read_data(self.warranty_lookup_fn)

        self.return_label_fn = lambda_.Function(self, "ReturnLabel", function_name="return-label-generator",
            code=lambda_.Code.from_asset(os.path.join(_tools, "return-label-generator")),
            environment={"BUCKET_NAME": self.return_labels_bucket.bucket_name,
                         "ORDERS_TABLE_NAME": self.orders_table.table_name}, **_ld)
        self.return_labels_bucket.grant_read_write(self.return_label_fn)
        self.orders_table.grant_read_data(self.return_label_fn)

        self.policy_lookup_fn = lambda_.Function(self, "PolicyLookup", function_name="company-policy-lookup",
            code=lambda_.Code.from_asset(os.path.join(_tools, "company-policy-lookup")),
            environment={"KB_ID": "G2OQB9OV5N"}, **_ld)
        self.policy_lookup_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:RetrieveAndGenerate", "bedrock:Retrieve"],
            resources=[f"arn:aws:bedrock:{self.region}:{self.account}:knowledge-base/*"]))

        self.check_eligibility_fn = lambda_.Function(self, "CheckEligibility", function_name="check-return-eligibility-tool",
            code=lambda_.Code.from_asset(os.path.join(_tools, "check-return-eligibility")),
            environment={"ORDERS_TABLE_NAME": self.orders_table.table_name}, **_ld)
        self.orders_table.grant_read_data(self.check_eligibility_fn)

        self.create_case_fn = lambda_.Function(self, "CreateCase", function_name="create-case-tool",
            code=lambda_.Code.from_asset(os.path.join(_tools, "create-case")),
            environment={"CASES_TABLE_NAME": self.cases_table.table_name}, **_ld)
        self.cases_table.grant_write_data(self.create_case_fn)

        all_tools = [self.order_lookup_fn, self.warranty_lookup_fn, self.return_label_fn,
                     self.policy_lookup_fn,
                     self.check_eligibility_fn, self.create_case_fn]

        # ── Bedrock Guardrails ────────────────────────────────────────────────

        self.guardrail = bedrock.CfnGuardrail(self, "Guardrail",
            name="robot-vacuum-guardrail",
            blocked_input_messaging="I can only help with AnyCompany Robotics robot vacuum support.",
            blocked_outputs_messaging="I can only help with AnyCompany Robotics robot vacuum support.",
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(filters_config=[
                bedrock.CfnGuardrail.ContentFilterConfigProperty(type="PROMPT_ATTACK", input_strength="MEDIUM", output_strength="NONE"),
                bedrock.CfnGuardrail.ContentFilterConfigProperty(type="VIOLENCE", input_strength="HIGH", output_strength="HIGH"),
                bedrock.CfnGuardrail.ContentFilterConfigProperty(type="HATE", input_strength="HIGH", output_strength="HIGH"),
                bedrock.CfnGuardrail.ContentFilterConfigProperty(type="INSULTS", input_strength="HIGH", output_strength="HIGH"),
                bedrock.CfnGuardrail.ContentFilterConfigProperty(type="MISCONDUCT", input_strength="HIGH", output_strength="HIGH"),
                bedrock.CfnGuardrail.ContentFilterConfigProperty(type="SEXUAL", input_strength="HIGH", output_strength="HIGH"),
            ]),
            sensitive_information_policy_config=bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(pii_entities_config=[
                bedrock.CfnGuardrail.PiiEntityConfigProperty(type="CREDIT_DEBIT_CARD_NUMBER", action="ANONYMIZE"),
                bedrock.CfnGuardrail.PiiEntityConfigProperty(type="US_SOCIAL_SECURITY_NUMBER", action="ANONYMIZE"),
            ]),
            topic_policy_config=bedrock.CfnGuardrail.TopicPolicyConfigProperty(topics_config=[
                bedrock.CfnGuardrail.TopicConfigProperty(name="CodingHelp", type="DENY",
                    definition="Requests for programming or software development help.",
                    examples=["Write me a Python script", "How do I fix this JavaScript error?"]),
                bedrock.CfnGuardrail.TopicConfigProperty(name="Weather", type="DENY",
                    definition="Requests for weather forecasts or climate info.",
                    examples=["What is the weather in Seattle?", "Will it rain tomorrow?"]),
                bedrock.CfnGuardrail.TopicConfigProperty(name="Finance", type="DENY",
                    definition="Requests for investment or financial advice.",
                    examples=["Should I buy Bitcoin?", "What stocks should I invest in?"]),
            ]),
        )

        # ── AgentCore Gateway ─────────────────────────────────────────────────

        gw_role = iam.Role(self, "GatewayRole",
            role_name="BedrockAgentCoreGatewayCustomerServiceRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
        )
        for fn in all_tools:
            fn.grant_invoke(gw_role)

        # Interceptor Lambda — customer ID mismatch detection
        self.interceptor_fn = lambda_.Function(self, "Interceptor",
            function_name="gateway-interceptor",
            runtime=lambda_.Runtime.PYTHON_3_12, handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(os.path.join(_lambdas, "gateway_interceptor")),
            timeout=Duration.seconds(30), memory_size=128)
        self.interceptor_fn.grant_invoke(gw_role)

        self.gateway = bac.CfnGateway(self, "Gateway",
            name="customer-service-agent-gateway",
            protocol_type="MCP",
            role_arn=gw_role.role_arn,
            authorizer_type="CUSTOM_JWT",
            authorizer_configuration=bac.CfnGateway.AuthorizerConfigurationProperty(
                custom_jwt_authorizer=bac.CfnGateway.CustomJWTAuthorizerConfigurationProperty(
                    discovery_url=f"{_cognito_issuer}/{self.user_pool.user_pool_id}/.well-known/openid-configuration",
                    allowed_audience=[self.app_client.user_pool_client_id],
                ),
            ),
            interceptor_configurations=[
                bac.CfnGateway.GatewayInterceptorConfigurationProperty(
                    interception_points=["REQUEST"],
                    interceptor=bac.CfnGateway.InterceptorConfigurationProperty(
                        lambda_=bac.CfnGateway.LambdaInterceptorConfigurationProperty(
                            arn=self.interceptor_fn.function_arn)),
                    input_configuration=bac.CfnGateway.InterceptorInputConfigurationProperty(
                        pass_request_headers=True),
                ),
            ],
            exception_level="DEBUG",
        )

        # ── Gateway Targets (native L1) ────────────────────────────────────

        _cred = [bac.CfnGatewayTarget.CredentialProviderConfigurationProperty(
            credential_provider_type="GATEWAY_IAM_ROLE")]

        def _mcp_lambda_target(lid, name, desc, fn, tools_schema):
            t = bac.CfnGatewayTarget(self, lid,
                name=name, description=desc,
                gateway_identifier=self.gateway.attr_gateway_identifier,
                target_configuration=bac.CfnGatewayTarget.TargetConfigurationProperty(
                    mcp=bac.CfnGatewayTarget.McpTargetConfigurationProperty(
                        lambda_=bac.CfnGatewayTarget.McpLambdaTargetConfigurationProperty(
                            lambda_arn=fn.function_arn,
                            tool_schema=bac.CfnGatewayTarget.ToolSchemaProperty(
                                inline_payload=tools_schema)))),
                credential_provider_configurations=_cred)
            t.add_dependency(self.gateway)
            return t

        _mcp_lambda_target("TargetOrderLookup", "order-lookup-tool", "Customer Order Lookup Tool",
            self.order_lookup_fn, [bac.CfnGatewayTarget.ToolDefinitionProperty(
                name="order-lookup-tool", description="Tool to look up a customer's order by order ID.",
                input_schema=bac.CfnGatewayTarget.SchemaDefinitionProperty(
                    type="object", properties={"orderId": {"type": "string", "description": "The order ID to look up"}},
                    required=["orderId"]))])

        _mcp_lambda_target("TargetWarrantyLookup", "warranty-lookup-tool", "Order Warranty Lookup Tool",
            self.warranty_lookup_fn, [bac.CfnGatewayTarget.ToolDefinitionProperty(
                name="warranty-lookup-tool", description="Tool to look up warranty information for an order",
                input_schema=bac.CfnGatewayTarget.SchemaDefinitionProperty(
                    type="object", properties={"orderId": {"type": "string"}},
                    required=["orderId"]))])

        _mcp_lambda_target("TargetReturnLabel", "return-label-generator", "Return Label Generator Tool",
            self.return_label_fn, [bac.CfnGatewayTarget.ToolDefinitionProperty(
                name="return-label-generator",
                description="Tool to generate a return shipping label for an order. The refund is processed automatically when the item is received. Requires orderId and amount (integer, the order total).",
                input_schema=bac.CfnGatewayTarget.SchemaDefinitionProperty(
                    type="object", properties={
                        "orderId": {"type": "string", "description": "The order ID"},
                        "amount": {"type": "integer", "description": "The order total amount in dollars (integer)"}},
                    required=["orderId", "amount"]))])

        _mcp_lambda_target("TargetPolicyLookup", "company-policy-lookup", "Company Policy Lookup Tool",
            self.policy_lookup_fn, [bac.CfnGatewayTarget.ToolDefinitionProperty(
                name="company-policy-lookup", description="Tool to look up company policy information",
                input_schema=bac.CfnGatewayTarget.SchemaDefinitionProperty(
                    type="object", properties={"query": {"type": "string"}},
                    required=["query"]))])

        _mcp_lambda_target("TargetCheckEligibility", "check-return-eligibility-tool", "Check Return Eligibility Tool",
            self.check_eligibility_fn, [bac.CfnGatewayTarget.ToolDefinitionProperty(
                name="check-return-eligibility-tool", description="Tool to check whether an order is eligible for return",
                input_schema=bac.CfnGatewayTarget.SchemaDefinitionProperty(
                    type="object", properties={"order_id": {"type": "string"}},
                    required=["order_id"]))])

        _mcp_lambda_target("TargetCreateCase", "create-case-tool", "Create Escalation Case Tool",
            self.create_case_fn, [bac.CfnGatewayTarget.ToolDefinitionProperty(
                name="create-case-tool", description="Tool to create a customer service escalation case",
                input_schema=bac.CfnGatewayTarget.SchemaDefinitionProperty(
                    type="object", properties={"reason": {"type": "string"}},
                    required=["reason"]))])

        # ── Cedar Policy Engine + Policies (native L1) ───────────────────────

        self.policy_engine = bac.CfnPolicyEngine(self, "PolicyEngine",
            name="refund_policy_engine",
            description="Cedar policy engine for customer service tool authorization",
        )

        _gw_arn = Fn.sub(
            "arn:aws:bedrock-agentcore:${Region}:${AccountId}:gateway/${GwId}",
            {"Region": self.region, "AccountId": self.account,
             "GwId": self.gateway.attr_gateway_identifier})

        # Broad permit for all authenticated users (IGNORE_ALL_FINDINGS to bypass overly-permissive check)
        permit_all = bac.CfnPolicy(self, "PermitAllTools",
            name="permit_all_other_tools",
            policy_engine_id=self.policy_engine.attr_policy_engine_id,
            description="Allow all authenticated users to call any tool",
            definition=bac.CfnPolicy.PolicyDefinitionProperty(
                cedar=bac.CfnPolicy.CedarPolicyProperty(
                    statement=Fn.sub(
                        'permit(principal is AgentCore::OAuthUser, action, resource == AgentCore::Gateway::"${GwArn}");',
                        {"GwArn": _gw_arn}))),
            validation_mode="IGNORE_ALL_FINDINGS",
        )

        # Permit return labels under $500
        permit_labels = bac.CfnPolicy(self, "PermitReturnLabelsUnder500",
            name="permit_return_labels_under_500",
            policy_engine_id=self.policy_engine.attr_policy_engine_id,
            description="Allow return labels for orders under 500 dollars",
            definition=bac.CfnPolicy.PolicyDefinitionProperty(
                cedar=bac.CfnPolicy.CedarPolicyProperty(
                    statement=Fn.sub(
                        'permit(principal, action == AgentCore::Action::"return-label-generator___return-label-generator", resource == AgentCore::Gateway::"${GwArn}") when { context.input.amount < 500 };',
                        {"GwArn": _gw_arn}))),
        )

        # Forbid return labels >= $500 — must be created AFTER the permits
        forbid_labels = bac.CfnPolicy(self, "ForbidReturnLabelsOver500",
            name="forbid_return_labels_over_500",
            policy_engine_id=self.policy_engine.attr_policy_engine_id,
            description="Block return labels for orders 500 dollars or more — must escalate to human",
            definition=bac.CfnPolicy.PolicyDefinitionProperty(
                cedar=bac.CfnPolicy.CedarPolicyProperty(
                    statement=Fn.sub(
                        'forbid(principal, action == AgentCore::Action::"return-label-generator___return-label-generator", resource == AgentCore::Gateway::"${GwArn}") when { context.input.amount >= 500 };',
                        {"GwArn": _gw_arn}))),
        )
        forbid_labels.add_dependency(permit_all)
        forbid_labels.add_dependency(permit_labels)

        # ── Policy Engine → Gateway attachment (custom resource — CFN gap) ───

        attach_fn = lambda_.Function(self, "PolicyAttachCR",
            function_name="policy-engine-attach-cr",
            runtime=lambda_.Runtime.PYTHON_3_12, handler="policy_attach.lambda_handler",
            code=lambda_.Code.from_asset(os.path.join(_lambdas, "gateway_custom_resource")),
            timeout=Duration.seconds(60), memory_size=128)
        attach_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock-agentcore:GetGateway", "bedrock-agentcore:UpdateGateway"],
            resources=[f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/*"]))
        attach_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["iam:PassRole"], resources=[gw_role.role_arn]))

        attach_provider = cr.Provider(self, "PolicyAttachProvider", on_event_handler=attach_fn)
        policy_attach = CustomResource(self, "PolicyEngineAttach",
            service_token=attach_provider.service_token,
            properties={
                "GatewayId": self.gateway.attr_gateway_identifier,
                "PolicyEngineArn": self.policy_engine.attr_policy_engine_arn,
                "Mode": "ENFORCE",
            },
        )
        policy_attach.node.add_dependency(self.gateway)
        policy_attach.node.add_dependency(self.policy_engine)

        # ── AgentCore Memory ──────────────────────────────────────────────────

        mem_role = iam.Role(self, "MemoryRole",
            role_name="BedrockAgentCoreMemoryExecutionRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
        )
        self.memory = bac.CfnMemory(self, "Memory",
            name="robot_vacuum_memory",
            event_expiry_duration=90,
            memory_execution_role_arn=mem_role.role_arn,
            description="Conversation memory for robot vacuum customer service agent",
        )

        # ── AgentCore Runtime ─────────────────────────────────────────────────

        rt_role = iam.Role(self, "RuntimeRole",
            role_name="BedrockAgentCoreRuntimeExecutionRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
        )
        # Bedrock model invocation (scoped to account/region)
        rt_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:ApplyGuardrail"],
            resources=[
                f"arn:aws:bedrock:{self.region}::foundation-model/*",
                f"arn:aws:bedrock:{self.region}:{self.account}:guardrail/*",
            ]))
        # CloudWatch logs (scoped to agentcore log groups)
        rt_role.add_to_policy(iam.PolicyStatement(
            actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
            resources=[f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore/*"]))
        # SSM parameters (scoped to our namespace)
        rt_role.add_to_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter", "ssm:GetParameters"],
            resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter/robot-vacuum/*"]))
        # AgentCore memory and gateway access
        rt_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock-agentcore:InvokeGateway", "bedrock-agentcore:CreateEvent",
                     "bedrock-agentcore:GetMemory", "bedrock-agentcore:ListEvents",
                     "bedrock-agentcore:CreateSession", "bedrock-agentcore:GetSession"],
            resources=[
                f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/*",
                f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:memory/*",
            ]))
        # X-Ray tracing for observability
        rt_role.add_to_policy(iam.PolicyStatement(
            actions=["xray:PutTraceSegments", "xray:PutTelemetryRecords",
                     "xray:GetSamplingRules", "xray:GetSamplingTargets"],
            resources=[f"arn:aws:xray:{self.region}:{self.account}:*"]))
        # CloudWatch metrics for observability
        rt_role.add_to_policy(iam.PolicyStatement(
            actions=["cloudwatch:PutMetricData"],
            resources=["*"],
            conditions={"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}}))

        # Read the create_zip script for Docker bundling
        _scripts = os.path.join(os.path.dirname(__file__), "..", "scripts")
        with open(os.path.join(_scripts, "create_zip.py"), "r") as f:
            create_zip_script = f.read()

        agent_code = s3_assets.Asset(self, "AgentCode",
            path=os.path.join(_repo, "agent"),
            bundling=BundlingOptions(
                image=DockerImage.from_registry("python:3.11-slim"),
                platform="linux/arm64",
                command=["bash", "-c", f"""
                    set -e
                    apt-get update -qq && apt-get install -y -qq curl gcc g++ > /dev/null 2>&1
                    mkdir -p /tmp/agent-bundle
                    cp -r /asset-input/* /tmp/agent-bundle/ 2>/dev/null || true
                    rm -rf /tmp/agent-bundle/__pycache__ /tmp/agent-bundle/.env \
                           /tmp/agent-bundle/.bedrock_agentcore /tmp/agent-bundle/*.pyc
                    cd /tmp/agent-bundle
                    echo "Installing dependencies..."
                    pip install --target /tmp/agent-bundle --upgrade \
                        -r requirements.txt 2>&1 | tail -10
                    cd /tmp
                    cat > /tmp/create_zip.py << 'PYEOF'
{create_zip_script}
PYEOF
                    python3 /tmp/create_zip.py
                """],
            ),
        )

        self.runtime = bac.CfnRuntime(self, "Runtime",
            agent_runtime_name="robot_vacuum_agent",
            role_arn=rt_role.role_arn,
            network_configuration=bac.CfnRuntime.NetworkConfigurationProperty(network_mode="PUBLIC"),
            agent_runtime_artifact=bac.CfnRuntime.AgentRuntimeArtifactProperty(
                code_configuration=bac.CfnRuntime.CodeConfigurationProperty(
                    code=bac.CfnRuntime.CodeProperty(
                        s3=bac.CfnRuntime.S3LocationProperty(
                            bucket=agent_code.s3_bucket_name,
                            prefix=agent_code.s3_object_key,
                        ),
                    ),
                    runtime="PYTHON_3_11",
                    entry_point=["agent.py"],
                ),
            ),
            # OAuth authorizer — same Cognito JWT validates here too
            authorizer_configuration=bac.CfnRuntime.AuthorizerConfigurationProperty(
                custom_jwt_authorizer=bac.CfnRuntime.CustomJWTAuthorizerConfigurationProperty(
                    discovery_url=f"{_cognito_issuer}/{self.user_pool.user_pool_id}/.well-known/openid-configuration",
                    allowed_audience=[self.app_client.user_pool_client_id],
                ),
            ),
            # Allow Authorization header to pass through to agent code
            request_header_configuration=bac.CfnRuntime.RequestHeaderConfigurationProperty(
                request_header_allowlist=["Authorization"],
            ),
            environment_variables={
                "AWS_REGION": self.region,
            },
        )

        # SSM parameters for agent config
        from aws_cdk import aws_ssm as ssm
        ssm.StringParameter(self, "SSMGatewayUrl",
            parameter_name="/robot-vacuum/gateway-url",
            string_value=self.gateway.attr_gateway_url)
        ssm.StringParameter(self, "SSMGuardrailId",
            parameter_name="/robot-vacuum/guardrail-id",
            string_value=self.guardrail.attr_guardrail_id)
        ssm.StringParameter(self, "SSMMemoryId",
            parameter_name="/robot-vacuum/memory-id",
            string_value=self.memory.attr_memory_id)

        # ── REST API Gateway (OAuth pass-through to AgentCore Runtime) ────────

        api = apigw.RestApi(self, "Api",
            rest_api_name="customer-service-api",
            description="REST API proxying to AgentCore Runtime with Cognito auth",
            deploy_options=apigw.StageOptions(stage_name="prod", tracing_enabled=True),
        )

        # Cognito authorizer
        authorizer = apigw.CognitoUserPoolsAuthorizer(self, "CognitoAuth",
            cognito_user_pools=[self.user_pool],
            authorizer_name="CognitoAuthorizer",
        )

        # POST /chat → HTTP_PROXY to AgentCore Runtime
        chat = api.root.add_resource("chat")

        # CORS preflight
        chat.add_method("OPTIONS", apigw.MockIntegration(
            integration_responses=[apigw.IntegrationResponse(
                status_code="200",
                response_parameters={
                    "method.response.header.Access-Control-Allow-Headers": "'Content-Type,Authorization,X-Amzn-Bedrock-AgentCore-Runtime-Session-Id'",
                    "method.response.header.Access-Control-Allow-Methods": "'POST,OPTIONS'",
                    "method.response.header.Access-Control-Allow-Origin": "'*'",
                },
            )],
            passthrough_behavior=apigw.PassthroughBehavior.WHEN_NO_MATCH,
            request_templates={"application/json": '{"statusCode": 200}'},
        ), method_responses=[apigw.MethodResponse(
            status_code="200",
            response_parameters={
                "method.response.header.Access-Control-Allow-Headers": True,
                "method.response.header.Access-Control-Allow-Methods": True,
                "method.response.header.Access-Control-Allow-Origin": True,
            },
        )])

        # POST /chat with OAuth pass-through
        runtime_url = Fn.sub(
            "https://bedrock-agentcore.${Region}.amazonaws.com/runtimes/${RuntimeId}/invocations?qualifier=DEFAULT&accountId=${AccountId}",
            {
                "Region": self.region,
                "RuntimeId": self.runtime.attr_agent_runtime_id,
                "AccountId": self.account,
            },
        )

        chat.add_method("POST",
            apigw.HttpIntegration(runtime_url,
                http_method="POST",
                options=apigw.IntegrationOptions(
                    connection_type=apigw.ConnectionType.INTERNET,
                    timeout=Duration.seconds(29),
                    request_parameters={
                        "integration.request.header.Authorization": "method.request.header.Authorization",
                        "integration.request.header.X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": "method.request.header.X-Amzn-Bedrock-AgentCore-Runtime-Session-Id",
                    },
                    integration_responses=[
                        apigw.IntegrationResponse(status_code="200",
                            response_parameters={"method.response.header.Access-Control-Allow-Origin": "'*'"}),
                        apigw.IntegrationResponse(status_code="400", selection_pattern="4\\d{2}",
                            response_parameters={"method.response.header.Access-Control-Allow-Origin": "'*'"}),
                        apigw.IntegrationResponse(status_code="500", selection_pattern="5\\d{2}",
                            response_parameters={"method.response.header.Access-Control-Allow-Origin": "'*'"}),
                    ],
                ),
            ),
            authorizer=authorizer,
            authorization_type=apigw.AuthorizationType.COGNITO,
            request_parameters={
                "method.request.header.Authorization": True,
                "method.request.header.X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": False,
            },
            method_responses=[
                apigw.MethodResponse(status_code="200",
                    response_parameters={"method.response.header.Access-Control-Allow-Origin": True}),
                apigw.MethodResponse(status_code="400",
                    response_parameters={"method.response.header.Access-Control-Allow-Origin": True}),
                apigw.MethodResponse(status_code="500",
                    response_parameters={"method.response.header.Access-Control-Allow-Origin": True}),
            ],
        )

        # CORS gateway responses for error codes
        apigw.GatewayResponse(self, "GwRespUnauth",
            rest_api=api, type=apigw.ResponseType.UNAUTHORIZED,
            response_headers={"Access-Control-Allow-Origin": "'*'", "Access-Control-Allow-Headers": "'Content-Type,Authorization'"},
        )
        apigw.GatewayResponse(self, "GwRespDenied",
            rest_api=api, type=apigw.ResponseType.ACCESS_DENIED,
            response_headers={"Access-Control-Allow-Origin": "'*'", "Access-Control-Allow-Headers": "'Content-Type,Authorization'"},
        )
        apigw.GatewayResponse(self, "GwResp5xx",
            rest_api=api, type=apigw.ResponseType.DEFAULT_5_XX,
            response_headers={"Access-Control-Allow-Origin": "'*'", "Access-Control-Allow-Headers": "'Content-Type,Authorization'"},
        )

        # ── WAF ──────────────────────────────────────────────────────────────

        waf = wafv2.CfnWebACL(self, "WAF",
            name="customer-service-waf", scope="REGIONAL",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True, metric_name="cs-waf", sampled_requests_enabled=True),
            rules=[
                wafv2.CfnWebACL.RuleProperty(name="CommonRules", priority=10,
                    override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                            vendor_name="AWS", name="AWSManagedRulesCommonRuleSet")),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True, metric_name="common", sampled_requests_enabled=True)),
                wafv2.CfnWebACL.RuleProperty(name="RateLimit", priority=20,
                    action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                            limit=100, aggregate_key_type="IP")),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True, metric_name="rate", sampled_requests_enabled=True)),
            ],
        )
        wafv2.CfnWebACLAssociation(self, "WAFAssoc",
            resource_arn=api.deployment_stage.stage_arn,
            web_acl_arn=waf.attr_arn,
        )

        # ── Outputs ──────────────────────────────────────────────────────────

        CfnOutput(self, "ApiEndpoint", value=f"{api.url}chat")
        CfnOutput(self, "UserPoolId", value=self.user_pool.user_pool_id)
        CfnOutput(self, "ClientId", value=self.app_client.user_pool_client_id)
        CfnOutput(self, "CognitoDomain",
            value=f"https://robot-vacuum-{self.account}.auth.{self.region}.amazoncognito.com")
        CfnOutput(self, "RuntimeId", value=self.runtime.attr_agent_runtime_id)
        CfnOutput(self, "GatewayId", value=self.gateway.attr_gateway_identifier)
        CfnOutput(self, "PolicyEngineId", value=self.policy_engine.attr_policy_engine_id)
