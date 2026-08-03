# Brave Search — Partner Extension (`aws-brave`)

A partner extension for the **Pluggable Agentic AI Framework** workshop that adds
live web search backed by the [Brave Search API](https://brave.com/search/api/).

Web search is a **tool wired through L3 Orchestration** (an AgentCore Gateway MCP
Lambda target) that conceptually **strengthens L1 Data & Knowledge** by adding
external, real-time retrieval alongside the Bedrock Knowledge Base. It follows the
same pattern as the base Order and Refund agents — **Lambda tool → Gateway target →
specialist A2A agent** — so it inherits L4 PII masking and L5 tracing by routing
through the Gateway.

This extension lives entirely under `aws-brave/`; the core `aws-only/` track is
untouched.

## Architecture

```
Research Agent (Strands A2A, Registry-discoverable)
   └── @tool web_search
         └── AgentCore Gateway (MCP, Cognito JWT)  ── inherits L4 masking + L5 tracing
               └── Lambda: anycompany_brave_web_search
                     ├── Secrets Manager: brave/search-api-key   (key read at runtime)
                     └── Brave Web Search API (GET /res/v1/web/search)
```

## Native alternative: AgentCore Web Search (and why a partner tool)

Amazon Bedrock AgentCore ships a **native, first-party web search** — the **Web
Search Tool**, a built-in Gateway connector (`connectorId: "web-search"`). It
occupies the **same Gateway MCP slot** this extension uses, so the two are
directly interchangeable: swapping is essentially replacing the Lambda target with
a `connectorId: "web-search"` target on the same Gateway — the Research Agent and
everything downstream stay identical.

**Native AgentCore Web Search**
- Managed connector — no Lambda, no API key, no quota/retry code. Agents discover
  `WebSearch` via `tools/list` and invoke it via `tools/call`.
- Backed by an **Amazon-operated web index** (tens of billions of docs, refreshed
  within minutes), plus a knowledge graph and semantic snippet extraction.
- **Queries never leave AWS.** Supports domain include/exclude and published-date filters.
- **Availability: `us-east-1` only** (at the time of writing).

**This Brave partner extension**
- Backed by Brave's **independent** index (a non-Amazon perspective) with Brave's
  ranking / Goggles.
- Works in **any region** and even outside AWS. This workshop runs in **us-west-2**,
  where the native tool isn't yet available — so Brave is the in-region web-search path here.
- Demonstrates the **partner-extension pattern**: how a third party plugs a
  capability into a framework layer via the same Gateway/agent contract.

| | AgentCore Web Search (native) | Brave (this extension) |
|---|---|---|
| Integration | Built-in Gateway connector, no key | Lambda target + key in Secrets Manager |
| Index | Amazon-operated | Brave (independent) |
| Data path | Stays within AWS | Query sent to Brave (3rd party) |
| Region (today) | `us-east-1` only | any region |
| Differentiators | knowledge graph, semantic snippets | independent index, Goggles, cross-cloud portability |

This is a teaching contrast, not a verdict: **native = zero-ops, AWS-owned index,
in-AWS privacy; partner = independent index, portability, no lock-in.**

## Layout

```
aws-brave/
├── code/
│   ├── requirements-test.txt              # pytest (test runner only)
│   ├── requirements_research_a2a.txt      # Research Agent runtime deps
│   ├── conftest.py                        # test sys.path shim
│   ├── web_search_lambda/
│   │   ├── brave_client.py                 # stdlib-only Brave client
│   │   └── lambda_function.py              # Lambda handler (Secrets Manager + client)
│   ├── research_agent/
│   │   ├── search_tool.py                  # pure tool-arg builder + schema loader
│   │   ├── tool_schemas.json               # MCP tool schema (web_search)
│   │   └── research_agent_a2a.py           # A2A server (cloned from order_agent_a2a.py)
│   └── tests/                              # pytest unit tests (no AWS/network needed)
└── workshop/
    └── l3-orchestration/
        └── 6_web_search_agent.ipynb        # deploy notebook (secret → Lambda → target → agent → cleanup)
```

## Prerequisites

- The **base workshop L1–L3 labs must be deployed first** — this extension reads the
  Gateway, Cognito, and model IDs the base labs publish to SSM (`/anycompany/agentcore/*`).
- A **Brave Search API key** — the notebook has a `<Paste Brave Key Here>` placeholder
  you overwrite; it is stored in AWS Secrets Manager (`brave/search-api-key`).
- **Region:** `us-west-2`.
- Python 3.12.

## Running the tests

The unit tests cover all AWS-free logic (Brave client, Lambda handler with a mocked
Secrets Manager, tool-arg builder, schema validity, agent source wiring). They need no
AWS credentials and make no network calls.

```bash
cd code
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python -m pytest tests/ -v
```

(A project-local venv is used because macOS/Homebrew Python is externally managed — PEP 668.)

## Deploy

Open `workshop/l3-orchestration/6_web_search_agent.ipynb` and run the cells top to
bottom (after the base workshop is deployed):

1. Prerequisite check (reads base SSM params).
2. Enter your Brave key → stored in Secrets Manager.
3. Package & deploy the `anycompany_brave_web_search` Lambda.
4. Least-privilege IAM (Lambda reads only the Brave secret; Gateway may invoke the Lambda).
5. Register the `brave-web-search` Gateway target.
6. Raw MCP `tools/call` smoke test (live Brave results).
7. Deploy & register the Research Agent.
8. Direct agent invoke.
9. Publish SSM params.
10. **Cleanup.**

## Security

- **The Brave API key lives only in Secrets Manager** — never in code, notebooks, or
  plaintext SSM. The Lambda reads it at runtime and caches it in a module global.
- The key is sent to Brave in the `X-Subscription-Token` **header**, never in the URL.
- The Lambda execution role is scoped to `secretsmanager:GetSecretValue` on the **Brave
  secret ARN only**.
- **Web results are untrusted input.** The Research Agent's system prompt instructs it to
  never follow instructions found in results and to cite source URLs; because the tool
  routes through the Gateway, the L4 Bedrock Guardrail interceptor also masks PII in
  responses.
- The committed notebook contains only the `<Paste Brave Key Here>` placeholder — do not
  save or commit it with your real key pasted in.

## Cost

Brave Search API is metered (~$5 / 1000 requests). The client caps `count` (max 20) and
the Lambda caches the key to avoid redundant Secrets Manager calls. Estimated incremental
cost for a single lab run is negligible. Always run the cleanup cell.

## Cleanup

The notebook's final cell deletes everything this lab creates: the Lambda, the Gateway
target, the IAM policies, the Research Agent runtime + Registry record, the Brave secret,
and the published SSM params.
