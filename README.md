# Pluggable Agentic AI Framework Workshop

Build a multi-agent customer support system on AWS using Amazon Bedrock AgentCore and third-party integrations.

## ⚠️ Workshop Content Notice

This is sample code for educational purposes. It demonstrates architectural patterns and is not intended for production deployment without additional security hardening, testing, and review. Before using in production:

- Conduct a security review
- Implement additional monitoring and alerting
- Review and harden all IAM policies
- Add comprehensive error handling
- Implement proper secrets management 

## Workshop Scenario: AnyCompany Customer Support

You are the AI architect for AnyCompany, an online retailer. Your mission: build a multi-agent AI system that handles order inquiries and refund processing — all without hardcoding agent endpoints. Specialist agents that are deployed and the orchestrator discovers them dynamically through a centralized registry.

## Architecture Overview 

The workshop is organized into five layers, each with its own hands-on lab. Security (L4) and observability (L5) are cross-cutting concerns that wrap around the core stack.

```
┌─────────────────────────────────────────────────────────────────┐
│  L5 — Observability (CloudWatch, X-Ray, Transaction Search)     │
├─────────────────────────────────────────────────────────────────┤
│  L4 — Security (Bedrock Guardrails, Gateway Interceptors, IAM)  │
├─────────────────────────────────────────────────────────────────┤
│  L3 — Orchestration (Harness → Registry → Specialist Agents)    │
├─────────────────────────────────────────────────────────────────┤
│  L2 — Inference (LiteLLM, Bedrock, Fireworks AI)                │
├─────────────────────────────────────────────────────────────────┤
│  L1 — Data & Knowledge (S3, OpenSearch, DynamoDB)               │
└─────────────────────────────────────────────────────────────────┘
```

---

### L1 — Data & Knowledge Foundation

Sets up the two data stores that power the agents: a multimodal Bedrock Knowledge Base (return-policy documents and product images, backed by OpenSearch Serverless) and a `CustomerOrders` DynamoDB table loaded with 25 sample orders. Resource IDs are published to SSM for downstream layers.

**AWS services:** S3, Bedrock Knowledge Bases, OpenSearch Serverless, DynamoDB, SSM Parameter Store

---

### L2 — Model & Inference

Builds a provider-agnostic inference layer using [LiteLLM](https://docs.litellm.ai/) so the entire system can swap LLM backends by changing a single string. Configures Bedrock Guardrails for PII anonymization and prompt-injection blocking, then publishes the active model ID and guardrail IDs to SSM for use by L3 and L4.

**AWS services:** Amazon Bedrock, Bedrock Guardrails, SSM Parameter Store  
**Third-party:** LiteLLM, Fireworks AI (optional)

---

### L3 — Agent Orchestration & Tooling

The core of the workshop. Builds a multi-agent system where a supervisor orchestrator discovers and delegates to specialist agents through a centralized registry.

```
User
 │
 ▼
AgentCore Harness (Orchestrator — Claude Sonnet)
 │   └── AgentCore Memory (short-term + long-term)
 │
 ├── AgentCore Registry (agent discovery)
 │       ├── Order Agent (A2A, Strands)
 │       └── Refund Agent (A2A, Strands)
 │
 └── AgentCore Gateway (MCP, Cognito JWT auth)
         ├── Lambda: check_order_details ──► DynamoDB (CustomerOrders)
         └── Lambda: refund_policy       ──► Bedrock Knowledge Base (return policy + product images)
                     check_eligible
                     process_refund
```

**AWS services:** AgentCore Runtime, AgentCore Registry, AgentCore Gateway, AgentCore Memory, AgentCore Harness, Code Interpreter, Lambda, Cognito, SSM Parameter Store

---

### L4 — Security, Safety & Guardrails

Attaches a Lambda interceptor to the existing AgentCore Gateway that applies the Bedrock Guardrail (created in L2) to every tool response before it reaches the orchestrator, anonymizing PII such as email addresses and shipping addresses.

```
Orchestrator → Gateway → [Lambda Interceptor] → Lambda Tools → DynamoDB
                                ↓
                        Bedrock Guardrail (masks PII in tool responses)
```

**AWS services:** Bedrock Guardrails, AgentCore Gateway (interceptors), Lambda, IAM

---

### L5 — Observability & Monitoring

Queries X-Ray distributed traces and CloudWatch metrics to follow a request end-to-end through the system, calculate cost per session, and identify latency bottlenecks. Also walks through the GenAI Observability dashboard in CloudWatch for ongoing monitoring.

**AWS services:** Amazon CloudWatch (GenAI Observability), AWS X-Ray, Application Signals

---

## Repo Structure

```
pluggable-agentic-ai-framework/
├── l1-data-knowledge/
│   ├── 1_s3_knowledge_base_setup.ipynb   # Bedrock KB + OpenSearch Serverless
│   ├── 2_dynamodb_tables.ipynb           # CustomerOrders table + sample data
│   └── sample-data/
├── l2-inference/
│   └── 1_pluggable_inference_layer.ipynb # LiteLLM backends + Bedrock Guardrails
├── l3-orchestration/
│   ├── 1_pre-requisites.ipynb            # Registry + Gateway + Cognito
│   ├── 2_order_agent.ipynb               # Order Agent (Lambda + Runtime + Registry)
│   ├── 3_refund_agent.ipynb              # Refund Agent (Lambda + Runtime + Registry)
│   ├── 4_orchestrator_agent.ipynb        # Harness + Memory
│   ├── 5_chatbot_ui.ipynb                # Streamlit chatbot
│   ├── agents/                           # A2A agent source + requirements
│   └── chatbot_app.py
├── l4-security/
│   └── 1_sensitive_data_masking.ipynb    # Gateway interceptor + PII masking
├── l5-observability/
│   ├── 1_end_to_end_tracing.ipynb        # X-Ray traces + cost calculation
│   └── 2_observability_dashboard.ipynb   # CloudWatch GenAI Observability walkthrough
└── README.md
```

---

## Execution Order

Each layer publishes resource IDs to SSM that the next layer reads. Run notebooks in the order below.

> **Note:** Notebooks with the same order number can be run in parallel.

| Notebook | Order |
|----------|-------|
| `l1-data-knowledge/1_s3_knowledge_base_setup.ipynb` | 1 |
| `l1-data-knowledge/2_dynamodb_tables.ipynb` | 1 |
| `l2-inference/1_pluggable_inference_layer.ipynb` | 1 |
| `l3-orchestration/1_pre-requisites.ipynb` | 1 |
| `l3-orchestration/2_order_agent.ipynb` | 2 |
| `l3-orchestration/3_refund_agent.ipynb` | 3 |
| `l3-orchestration/4_orchestrator_agent.ipynb` | 4 |
| `l3-orchestration/5_chatbot_ui.ipynb` | 5 |
| `l4-security/1_sensitive_data_masking.ipynb` | 6 |
| `l5-observability/1_end_to_end_tracing.ipynb` | 7 |
| `l5-observability/2_observability_dashboard.ipynb` | 8 |
---

## Next Steps: 

### Third-Party Integration Options

**Pipeline: We have active work in progress to support integrations with NVIDIA, Datadog, Dynatrace and others.**  

Each layer is pluggable. AWS services are the default, but every layer can be swapped:

| Layer | Default (AWS) | Supported Integrations |
|-------|--------------|-------------|
| L1 | Bedrock KB — OpenSearch Serverless | Upcoming |
| L2 | Amazon Bedrock | Fireworks AI (notebooks will be available soon) |
| L3 | Strands (A2A agents) | Upcoming |
| L4 | Bedrock Guardrails, Custom Lambda interceptors | Upcoming|
| L5 | CloudWatch GenAI Observability | Upcoming |

> **NOTE:Partner Integration Content will be published soon.**

---

## Prerequisites

- AWS account with Bedrock model access enabled (Claude Sonnet, Titan Embeddings V2)
- Python 3.9+ with `boto3` configured (IAM credentials or instance role)
- *(Optional)* Fireworks AI API key for non-Bedrock inference backends

> **Cost note:** Estimated cost for a single workshop run is under $5. Each notebook includes a cleanup cell to delete created resources.

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the LICENSE file.
