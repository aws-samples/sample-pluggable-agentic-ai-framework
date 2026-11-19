# aws-reinvent/infra

CloudFormation pre-provisioning for the workshop. One parent template
(`agent-stack.yaml`) with one nested stack per layer, and two scripts handle
the full lifecycle.

Provisions the SageMaker Studio environment, the **data layer** (DynamoDB
`CustomerOrders` + sample data), the **L3 orchestration** layer (Cognito pool
test users, AgentCore gateways, Agent Registry, SSM handoffs), the **L5
decision model** (Strands Decider on SageMaker), and **L6 observability** so
participants skip the deterministic setup.

## Stack layout

```
agent-stack.yaml                 Parent: parameters, one nested stack per layer, outputs
stacks/
├── platform-studio.yaml         SageMaker Studio domain, user profile, JupyterLab space,
│                                execution role, lifecycle config (syncs workshop.zip)
├── l1-data.yaml                 DynamoDB CustomerOrders + sample-data loader,
│                                knowledge-base S3 bucket + IAM role
├── l3-orchestration.yaml        Cognito pool + test users, AgentCore gateways,
│                                Agent Registry + registry-discovery and
│                                agent-invoker tools, L3 execution roles,
│                                web-search Gateway permissions,
│                                Application Signals, SSM handoffs
├── l3-websearch.yaml            Research Agent runtime role, Brave Search MCP
│                                server image (pre-built) + its runtime role
├── l4-security.yaml             PII interceptor Lambda execution role + SSM handoff
│                                (Gateway invoke grant lives in l3-orchestration.yaml;
│                                the interceptor Lambda + guardrail stay in the L4 notebook)
├── l5-decision.yaml             Strands Decider endpoint (only if DeployDecider=true)
└── l6-observability.yaml        X-Ray Transaction Search
```

- The layers share no resources, so the nested stacks deploy in parallel.
- Each child gets the parent's name as `ParentStackName`, so physical names are
  the same as in the old single-file template (e.g. `agent-stack-SageMakerExecutionRole`).
- The parent keeps every output (`SageMakerDomainId`, `CognitoUserPoolId`, …) by
  reading them from its children, so scripts and notebooks that read outputs
  are unaffected.
- A nested stack that fails rolls back the whole parent, as before.
- **Workshop Studio:** ship `stacks/*.yaml` in the event assets so they land at
  `<AssetsBucketPrefix>stacks/` in the assets bucket — that is where the parent
  loads them from. `AssetsBucketPrefix` must end in `/` (Workshop Studio injects
  it that way; `deploy-stack.sh` appends it; include it when deploying from the
  console, e.g. `paf/`).

The L5 decision stack's endpoint is created in the background after stack
creation finishes (Step Functions + CodeBuild): GPU instance pools first
(`ml.g6.xlarge`, `ml.g5.xlarge`, `ml.g6e.xlarge`), then CPU (`ml.m5.2xlarge`,
`ml.m5.4xlarge`). Progress is published to `/anycompany/agentcore/decider_status`.
The endpoint bills per instance-hour until the stack is deleted; set
`DeployDecider=false` to skip it.

The L3 web-search stack (`l3-websearch.yaml`) builds the **Brave Search MCP server** image in the background
(custom resource → CodeBuild on arm64 → ECR `anycompany-brave-mcp-server`), so
participants who pick Brave in `1_gateway_tools.ipynb` only wait about a minute
for the runtime instead of the image build. Progress is published to
`/anycompany/agentcore/brave_mcp_build_status` (`BUILDING` → `READY` / `FAILED: …`)
and the image to `brave_mcp_image_uri`. The image never contains the Brave key:
the container reads it from Secrets Manager (`anycompany/brave-search-api-key`)
at startup. The build costs a few minutes of CodeBuild; the image itself is free
until a runtime uses it.

## Scripts

```
scripts/
├── build-workshop-zip.sh             Bundle aws-reinvent/workshop/ into assets/workshop.zip.
├── deploy-stack.sh                   One-shot deploy. Idempotent — first-time and re-deploys.
├── teardown.sh                       Delete the stack and the assets bucket.
└── teardown-notebook-resources.sh    Delete resources that notebooks create outside the stack.
```

`deploy-stack.sh` does everything in one pass: builds `workshop.zip`, creates
the assets bucket, uploads `workshop.zip` + sample-data, uploads the parent and
nested templates to S3 (nested stacks are always loaded from S3), validates
each template, create-stack on first run or update-stack on subsequent runs,
and runs post-deploy verification.

## Deploy (any account)

Amazon Bedrock enables serverless models (Claude Sonnet 4.6, Nova, Titan Embed) on
first use in us-east-1. If the account has not used Anthropic Claude before, submit
the one-time Anthropic use-case form from the Bedrock model catalog first. The
deploying identity needs IAM permission to invoke these models.

One command:

```bash
export AWS_PROFILE=<target-account-profile>
cd aws-reinvent/infra
./scripts/deploy-stack.sh
```

`deploy-stack.sh` builds `assets/workshop.zip` on demand by invoking
`build-workshop-zip.sh` if the artifact is missing, so a single command
covers the common case. Run `./scripts/build-workshop-zip.sh` directly only
when you want to pre-build (e.g., inspecting the zip, or shipping it to
Workshop Studio where their automation drops the pre-built zip at
`assets/workshop.zip` before running `deploy-stack.sh`).

Supported regions: `us-east-1`, `us-west-2`.

**Upgrading an account that ran the pre-CloudFormation L4 lab:** the old L4
notebook created `AnyCompanyPIIInterceptorLambdaRole` via the SDK. That role is
now CloudFormation-owned, so on such an account delete the leftover role (and the
notebook-created `anycompany-pii-interceptor` Lambda) *before* the stack update —
otherwise `SecurityStack` fails to create with "already exists". Fresh event
accounts are unaffected.

## Teardown

For a full account wipe, run both scripts. **Order matters:** run
`teardown-notebook-resources.sh` *first* (or in parallel), then `teardown.sh`.
Running notebook-resources first guarantees the `KBSourceBucket` is empty and
the stack-owned gateway's targets are gone, so `teardown.sh` won't trip on
`DELETE_FAILED`.

```bash
export AWS_PROFILE=<account-profile>

# 1. Notebook-created resources (AgentCore, Bedrock KB, Omni, SSM, KB bucket contents)
./scripts/teardown-notebook-resources.sh

# 2. CloudFormation stack + assets S3 bucket
./scripts/teardown.sh
```

### What each script does

`teardown.sh` stops the JupyterLab app first so the SageMaker Space/Domain can
delete cleanly, deletes the stack, then empties and removes the assets bucket.

`teardown-notebook-resources.sh` sweeps resources that notebooks create via
API calls (not owned by the stack): AgentCore harnesses/gateways/runtimes
(order, refund, research, and the Brave MCP server)/memory, the Perplexity
API-key credential provider and the Brave Secrets Manager secret (force-deleted
so a redeploy can reuse the name), Bedrock Knowledge Base + AOSS collection +
policies, CloudWatch Omni domain/space + grants, Omni IAM roles, SSM parameters
under `/anycompany/agentcore/*` (including the saved partner API keys), and the
contents of the `KBSourceBucket`. Scoped by name prefix (`anycompany*`,
`composable-agents-*`, `CloudWatchOmni*`); idempotent and safe to re-run.

### Running in parallel

The two scripts touch disjoint resource sets, so you can run them in two
terminals at the same time to shave the overall teardown time (notebook-
resources finishes in 1-3 min; the stack delete is the long tail at 5-15 min).
Open two terminals with the same `AWS_PROFILE` exported and launch both.

### Not handled by either script

- **Couchbase Capella free-tier cluster** (if the Couchbase path was chosen in
  L1): delete via [cloud.couchbase.com](https://cloud.couchbase.com) → your
  project → cluster → trash. The script can't reach your Capella tenant.

## Env var overrides

Both scripts read from env; defaults in parentheses.

| Variable        | Default                                | Notes                                          |
|-----------------|----------------------------------------|------------------------------------------------|
| `AWS_PROFILE`   | *(required)*                           | Fail-loud guard — no default to prevent typos. |
| `AWS_REGION`    | `us-east-1`                            |                                                |
| `STACK_NAME`    | `agent-stack`                          |                                                |
| `ASSETS_BUCKET` | `aapcf-reinvent-assets-<account-id>`   | Globally-unique, account-scoped by default.    |
| `ASSETS_PREFIX` | `latest`                               | S3 key prefix for workshop assets.             |

## Notes

- **Moving from the single-file template.** A stack created before the split
  can't be updated into the nested layout (the nested stacks would recreate
  fixed-name resources such as `CustomerOrders` before the old ones are gone).
  `deploy-stack.sh` detects this and stops; tear the old stack down first.
- **Native Agent Registry.** The Agent Registry is created with the native
  `AWS::AgentRegistry::Registry` CloudFormation resource type (no custom
  Lambda or vendored SDK layer needed). The deploying principal must have
  `agent-registry:CreateRegistry` / `GetRegistry` / `DeleteRegistry` plus the
  service-linked-role + KMS permissions the resource type requires.
- **Native registry discovery.** The registry discovery tool is a native
  `AWS::BedrockAgentCore::GatewayTarget` on the harness gateway, pointing at the
  registry's MCP endpoint (`https://agent-registry.<region>.api.aws/registry/<id>/mcp`)
  with IAM auth (gateway role signs as service `agent-registry`). No Lambda or SDK
  layer is needed, and the L3 setup notebook no longer creates it. The endpoint
  only searches, lists and gets records; it cannot call an agent.
- **Agent invoker tool.** The "call an agent" half lives in a second harness-gateway
  target, `agent-invoker` (tool `agent-invoker___invoke_agent`). Its small inline
  Lambda (`<stack>-agent-invoker`) sends one A2A `message/send` to the chosen
  AgentCore runtime, signed with its own IAM role, and returns the reply text. It
  accepts a runtime ARN or the `url` from the agent card, and only runtimes in this
  account and region.
- **Pre-provisioned L3 roles.** The execution roles the L3 notebooks used to create
  (Order/Refund tool Lambdas, Order/Refund agent runtimes, Harness) are in the L3
  stack with the same names and permissions; their ARNs are in SSM
  (`order_tools_role_arn`, `refund_tools_role_arn`, `order_agent_runtime_role_arn`,
  `refund_agent_runtime_role_arn`, `harness_role_arn`). Notebook cleanups no longer
  delete them. The same goes for the Research Agent (`research_agent_runtime_role_arn`)
  and the Brave MCP server (`brave_mcp_runtime_role_arn`).
- **Web-search permissions on the Gateway role.** `AnyCompanyAgentCoreGatewayRole`
  is pre-granted the outbound permissions for all three Research Agent providers,
  so the L3 notebooks never edit this stack-owned role:
  `bedrock-agentcore:InvokeWebSearch` (AgentCore Web Search connector),
  `InvokeAgentRuntime` on `runtime/brave_mcp_server-*` (Brave), and the AgentCore
  token-vault / API-key reads the Perplexity credential provider needs.
- **Agent-invoker timeout.** The `<stack>-agent-invoker` Lambda allows 120 s (the
  A2A call itself 115 s) because a Research Agent turn — web search plus an LLM
  turn — can take longer than an order lookup.
- **Registry lifecycle.** Found/deleted by name — intended for isolated
  per-participant accounts.
- **Clean teardown.** Custom resources drain and signal cleanly on
  `delete-stack`.
