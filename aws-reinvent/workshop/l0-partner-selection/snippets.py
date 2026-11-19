"""Layer / provider definitions and copy-paste code snippets.

This is the single source of truth for the partner-selection UI:

- ``LAYERS`` describes each pluggable layer, its selectable providers, each
  provider's optional credential field, and a short description.
- ``get_snippet(layer_id, provider)`` returns the Python code the user copies
  into the matching layer notebook once they've chosen that provider.

Credential fields are stored in SSM at:
    /anycompany/agentcore/config/<layer_id>/<credential_key>   (SecureString)
Provider selections are stored at:
    /anycompany/agentcore/config/<layer_id>/provider            (String)

The AWS-default snippets mirror the patterns already used in the aws-reinvent
notebooks. Partner snippets correspond to the partner variant folders in this
repo (aws-perplexity, aws-datadog, aws-brave-nvidia, ...). Where a partner
folder does not yet exist (Couchbase, Baseten), the snippet is a clearly
marked starting point.
"""

SSM_PREFIX = "/anycompany/agentcore"
CONFIG_PREFIX = f"{SSM_PREFIX}/config"

# Allowlist of valid Datadog site identifiers. The site is interpolated into the
# OTLP traces endpoint right next to the ingest key, so only known-good values
# may be stored. Keep in sync with the L3 helper
# (l3-orchestration/helpers/observability.py DATADOG_SITES).
DATADOG_SITES = (
    "datadoghq.com",        # US1
    "us3.datadoghq.com",    # US3
    "us5.datadoghq.com",    # US5
    "datadoghq.eu",         # EU1
    "ap1.datadoghq.com",    # AP1
    "ddog-gov.com",         # US1-FED
)

# Partner keys that more than one layer uses live at ONE path, keyed by partner,
# so participants enter them once. The Perplexity key serves both
# Orchestration > Tools (Perplexity Search) and Application > Decision Models
# (Perplexity Decisions). Keep in sync with l3-orchestration/helpers/websearch.py
# and l5-application/decision_models.py.
PERPLEXITY_KEY_PARAM = f"{CONFIG_PREFIX}/perplexity/api_key"
BRAVE_KEY_PARAM = f"{CONFIG_PREFIX}/brave/api_key"


def _doc(url: str, label: str) -> str:
    """A documentation link appended to a provider description (opens a new tab)."""
    return f' <a href="{url}" target="_blank" rel="noopener noreferrer">{label} ↗</a>'


def cred_param(layer_id: str, cred: dict) -> str:
    """SSM path for a provider credential: an explicit ``param`` (shared partner
    key) wins; otherwise the per-layer default config/<layer>/<key>."""
    return cred.get("param") or f"{CONFIG_PREFIX}/{layer_id}/{cred['key']}"


# Each layer belongs to a top-level nav ``section``. Every section renders as a
# heading in the sidebar with its layers listed underneath:
#   Data > Vector Store, Inference > Model Provider,
#   Orchestration > Tools / Observability, Security > Guardrails,
#   Application > Decision Models.
# The list order is the nav and step order. Layer ``id``s are part of the SSM
# paths, so rename labels freely but never change an id.
SECTIONS = ["Data", "Inference", "Orchestration", "Security", "Application"]

LAYERS = [
    {
        "id": "data_kb",
        "section": "Data",
        "label": "Vector Store",
        "help": "Store AnyCompany's return-policy documents as vector embeddings, so "
                "the Refund Agent can find the right policy passage by meaning, not "
                "just by matching keywords.",
        "providers": {
            "Bedrock KB (AWS)": {
                "tagline": "Managed RAG on OpenSearch Serverless",
                "description": "Bedrock Knowledge Base backed by OpenSearch Serverless. "
                               "The notebook creates the collection, ingests the policy "
                               "docs from S3, and exposes a retrieval API.",
            },
            "Couchbase": {
                "tagline": "Vector search in Capella",
                "description": "Couchbase Capella as the vector store. Policy docs are "
                               "chunked, embedded with Amazon Titan, and searched with a "
                               "vector index.",
                "credential": {
                    "key": "api_key",
                    "label": "Couchbase API key",
                    "placeholder": "cb-...",
                },
            },
        },
    },
    # old-L1 (structured+unstructured combined) is hidden from the sidebar. Its
    # code paths - STRUCTURED_PROVIDERS, STRUCTURED_STAGES, structured_snippet,
    # and the `elif layer_id == "data":` branch in partner_selector_app.py -
    # are kept intact for the day structured data becomes pluggable again.
    # To restore the page, re-add a layer entry here with id="data".
    {
        "id": "inference",
        "section": "Inference",
        "label": "Model Provider",
        "help": "Choose the model behind each agent role. Specialist agents call their model "
                "through LiteLLM, so moving between Amazon Bedrock and a partner endpoint is "
                "a model id change, not a code change.",
        "providers": {
            "Amazon Bedrock (AWS)": {
                "tagline": "Claude and more via LiteLLM",
                "description": "Claude on Amazon Bedrock via LiteLLM - the default inference backend.",
            },
            "Baseten": {
                "tagline": "Hosted open models via LiteLLM",
                "description": "Baseten-hosted open models, called through LiteLLM's "
                               "baseten/ provider.",
                "credential": {
                    "key": "api_key",
                    "label": "Baseten API key",
                    "placeholder": "...",
                },
            },
        },
    },
    {
        "id": "websearch",
        "section": "Orchestration",
        "label": "Tools",
        "help": "Give the Research Agent live web search with cited sources. Every provider "
                "plugs into the same AgentCore Gateway as an MCP target, so the agent code "
                "stays the same whichever you pick.",
        # Provider names must match WEBSEARCH_PROVIDERS below. "param" points the
        # credential at the shared partner key.
        "providers": {
            "AgentCore Web Search (AWS)": {
                "tagline": "Managed web search on the Gateway",
            },
            "Brave Search": {
                "tagline": "Open source MCP server on AgentCore Runtime",
                "credential": {
                    "key": "api_key",
                    "param": BRAVE_KEY_PARAM,
                    "label": "Brave Search API key",
                    "placeholder": "BSA...",
                },
            },
            "Perplexity Search": {
                "tagline": "Hosted MCP server: search + ask",
                "credential": {
                    "key": "api_key",
                    "param": PERPLEXITY_KEY_PARAM,
                    "label": "Perplexity API key",
                    "placeholder": "pplx-...",
                },
            },
        },
    },
    {
        "id": "security",
        "section": "Security",
        "label": "Guardrails",
        "help": "How tool responses and model output are guarded.",
        # Hidden from the UI: no partner integration is showcased here for now.
        # Remove this flag to bring the page back.
        "hidden": True,
        "providers": {
            "Bedrock Guardrails (AWS)": {
                "tagline": "PII masking + prompt-attack filters",
                "description": "Amazon Bedrock Guardrails for PII masking and prompt-injection blocking.",
            },
        },
    },
    {
        "id": "observability",
        "section": "Orchestration",
        "label": "Agent Observability",
        "help": "Choose where your agents' traces go. The agents emit OpenTelemetry spans "
                "for every model call and tool call; this choice decides which backend "
                "collects them. Logs stay in CloudWatch either way.",
        "providers": {
            "CloudWatch (AWS)": {
                # "value" is the lowercase provider id persisted to SSM (the UI
                # label may differ from the stored value); L3/L6 read this id.
                "value": "cloudwatch",
                "tagline": "GenAI Observability and CloudWatch Omni",
                "description": "<b>Agent telemetry that stays in your AWS account.</b> "
                               "AgentCore's built-in OpenTelemetry instrumentation sends "
                               "spans to CloudWatch, where X-Ray Transaction Search makes "
                               "them searchable. Explore sessions, traces and token usage in "
                               "CloudWatch GenAI Observability, or ask questions in plain "
                               "language with CloudWatch Omni in L6. No external portal or "
                               "credentials."
                               + _doc("https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability.html",
                                      "AgentCore Observability documentation"),
            },
            "Datadog": {
                "value": "datadog",
                "kind": "key",
                "tagline": "Agent Observability, OTEL-direct",
                "description": "<b>Every conversation as one session in Datadog.</b> The "
                               "agents export spans straight to Datadog's OTLP intake, with "
                               "no Datadog Agent, sidecar or ddtrace. A small span processor "
                               "stamps each span with the conversation id, so the "
                               "orchestrator and its specialists show up as a single session "
                               "in Datadog Agent Observability, with model, tool and token "
                               "detail. Logs stay in CloudWatch."
                               + _doc("https://docs.datadoghq.com/llm_observability/instrument/otel_instrumentation/",
                                      "Datadog OpenTelemetry instrumentation documentation"),
                # Link shown in the UI so participants can create their keys.
                "portal": {
                    "url": "https://docs.datadoghq.com/account_management/api-app-keys/",
                    "label": "How to create Datadog API and application keys",
                },
                # Datadog needs THREE credentials. "credentials" (a list) is the
                # multi-field form of the single-field "credential" shape the
                # other layers use; each field carries a "secure" flag so the UI
                # knows which SSM param type to write (SecureString vs String).
                "credentials": [
                    {
                        "key": "api_key",
                        "label": "Datadog API key",
                        "placeholder": "Datadog API (ingest) key",
                        "secure": True,
                    },
                    {
                        "key": "app_key",
                        "label": "Datadog application key",
                        "placeholder": "Datadog application key (for L6 API reads)",
                        "secure": True,
                    },
                    {
                        "key": "site",
                        "label": "Datadog site",
                        "placeholder": "datadoghq.com (or us5.datadoghq.com, datadoghq.eu)",
                        "secure": False,
                        # Allowlisted values — the UI renders a dropdown and the
                        # save handler rejects anything not in this list (the site
                        # feeds the OTLP endpoint the ingest key is sent to).
                        "options": list(DATADOG_SITES),
                    },
                ],
            },
        },
    },
    {
        "id": "decision",
        "section": "Application",
        "label": "Decision Models",
        "help": "Add a fast decision step in front of the orchestrator. A decision model "
                "answers typed questions (yes/no, pick one, or a score) with probabilities "
                "instead of free text, so the chatbot can act on the numbers.",
        # Provider names must match DECISION_PROVIDERS below. The Perplexity key is
        # shared with Tools.
        "providers": {
            "Strands Decider (AWS)": {
                "tagline": "Open source decider on SageMaker",
            },
            "Perplexity Decisions": {
                "tagline": "Hosted Decisions API",
                "credential": {
                    "key": "api_key",
                    "param": PERPLEXITY_KEY_PARAM,
                    "label": "Perplexity API key",
                    "placeholder": "pplx-...",
                },
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Snippets. Keyed by (layer_id, provider label).
# ---------------------------------------------------------------------------

def _cred_read(layer_id: str, key: str = "api_key") -> str:
    """Return a code line that reads a stored credential from SSM."""
    name = f"{CONFIG_PREFIX}/{layer_id}/{key}"
    return (
        "import boto3, os\n"
        "ssm = boto3.client('ssm', region_name=os.environ.get('AWS_DEFAULT_REGION', 'us-east-1'))\n"
        f"API_KEY = ssm.get_parameter(Name='{name}', WithDecryption=True)['Parameter']['Value']"
    )


SNIPPETS = {
    # ----- L1 Data -----
    ("data", "Bedrock KB + DynamoDB (AWS)"): (
        "# L1 default: Bedrock Knowledge Base (OpenSearch Serverless) + DynamoDB.\n"
        "# Use the l1-data-knowledge notebooks as-is:\n"
        "#   1_s3_knowledge_base_setup.ipynb   (Bedrock KB + OpenSearch)\n"
        "#   2_dynamodb_tables.ipynb           (CustomerOrders table)\n"
        "# Resource IDs are published to SSM under /anycompany/agentcore/."
    ),
    ("data", "Couchbase"): (
        "# L1 swap: Couchbase as vector store + operational data store.\n"
        + _cred_read("data")
        + "\n\n"
        "from couchbase.cluster import Cluster\n"
        "from couchbase.auth import PasswordAuthenticator\n"
        "from couchbase.options import ClusterOptions\n\n"
        "COUCHBASE_CONN = os.environ['COUCHBASE_CONNECTION_STRING']  # couchbases://...\n"
        "cluster = Cluster(\n"
        "    COUCHBASE_CONN,\n"
        "    ClusterOptions(PasswordAuthenticator('apikey', API_KEY)),\n"
        ")\n"
        "bucket = cluster.bucket('anycompany')\n"
        "collection = bucket.default_collection()\n"
        "# TODO: create the vector search index and load return-policy docs.\n"
        "# Starting point - align with the aws-couchbase variant once available."
    ),

    # ----- L2 Inference -----
    ("inference", "Amazon Bedrock (AWS)"): (
        "# L2 default: Claude on Amazon Bedrock through LiteLLM.\n"
        "MODEL_ID = 'bedrock/us.anthropic.claude-sonnet-5'\n"
        "# Used directly in l2-inference/1_pluggable_inference_layer.ipynb."
    ),
    ("inference", "Baseten"): (
        "# L2 swap: Baseten-hosted model via LiteLLM's baseten/ provider.\n"
        + _cred_read("inference")
        + "\n\n"
        "import os\n"
        "os.environ['BASETEN_API_KEY'] = API_KEY   # LiteLLM reads it for baseten/ models\n"
        "MODEL_ID = 'baseten/moonshotai/Kimi-K3.1-Pro'"
    ),

    # ----- L3 Tools (web search): see WEBSEARCH_PROVIDERS / websearch_snippet() -----
    # ----- L5 Decision model: see DECISION_PROVIDERS / decision_snippet() -----

    # ----- L4 Security -----
    ("security", "Bedrock Guardrails (AWS)"): (
        "# L4 default: Bedrock Guardrails applied by a Gateway Lambda interceptor.\n"
        "# Use l4-security/1_sensitive_data_masking.ipynb as-is. The guardrail ID\n"
        "# is created and published to SSM by that notebook (Step 2)."
    ),

}


def get_snippet(layer_id: str, provider: str) -> str:
    """Return the copy-paste snippet for a (layer, provider) pair, or ''."""
    return SNIPPETS.get((layer_id, provider), "")

# ---------------------------------------------------------------------------
# Agent Observability (L3): one provider for every agent's traces.
#
# The choice is saved on the Agent Observability page. The snippet pastes into
# Step 6 of l3-orchestration/1_gateway_tools.ipynb and confirms it before the
# agents are deployed (they read it at deploy time).
# ---------------------------------------------------------------------------
OBSERVABILITY_STAGES = [
    {"id": "confirm", "label": "1 · Confirm the observability provider",
     "help": "Paste into Step 6 in l3-orchestration/1_gateway_tools.ipynb. "
             "Checks the saved choice before you deploy the agents."},
]


def _obs_snip_cloudwatch() -> str:
    return "\n".join([
        "# ┌─ L3 Agent Observability · CloudWatch (AWS) · Confirm",
        "# │ Paste into the empty 'Step 6' cell in 1_gateway_tools.ipynb and run.",
        "# │ No keys needed: the agents send their traces to CloudWatch.",
        "# └────────────────────────────────────────────────────────────",
        "from helpers.observability import get_observability_provider",
        "",
        "OBS_PROVIDER = get_observability_provider(ssm, SSM_PREFIX)   # \"cloudwatch\" if none selected",
        "if OBS_PROVIDER != \"cloudwatch\":",
        "    raise RuntimeError(f\"The saved provider is {OBS_PROVIDER}, not CloudWatch. Click CloudWatch in the \"",
        "                       \"Partner Selection UI (Orchestration -> Agent Observability), then re-run this cell.\")",
        "print(f\"Order, Refund, Research and Orchestrator agents will send traces to: {OBS_PROVIDER}\")",
    ])


def _obs_snip_datadog() -> str:
    return "\n".join([
        "# ┌─ L3 Agent Observability · Datadog · Confirm",
        "# │ Paste into the empty 'Step 6' cell in 1_gateway_tools.ipynb and run.",
        "# │ The agents need the Datadog keys when they deploy, so this checks them now.",
        "# └────────────────────────────────────────────────────────────",
        "from helpers.observability import get_observability_provider, datadog_otel_env",
        "",
        "OBS_PROVIDER = get_observability_provider(ssm, SSM_PREFIX)",
        "if OBS_PROVIDER != \"datadog\":",
        "    raise RuntimeError(\"Datadog is not saved as the provider. Save your Datadog keys in the Partner \"",
        "                       \"Selection UI (Orchestration -> Agent Observability), then re-run this cell.\")",
        "try:",
        "    datadog_otel_env(ssm, \"anycompany-check\", SSM_PREFIX)",
        "except ssm.exceptions.ParameterNotFound:",
        "    raise RuntimeError(\"Datadog is selected but its keys are missing. Save them in the Partner \"",
        "                       \"Selection UI (Orchestration -> Agent Observability), then re-run this cell.\")",
        "print(f\"Order, Refund, Research and Orchestrator agents will send traces to: {OBS_PROVIDER}\")",
    ])


def observability_snippet(provider: str, stage_id: str) -> str:
    """Return the per-provider Confirm block for the Agent Observability layer."""
    if provider.startswith("CloudWatch"):
        return _obs_snip_cloudwatch()
    if provider == "Datadog":
        return _obs_snip_datadog()
    return ""



# ---------------------------------------------------------------------------
# Inference layer: per-ROLE model selection.
#
# The orchestrator and the sub-agents each pick their own model
# provider. Both go through LiteLLM, so a swap is just a different model_id
# string. Selections are stored in SSM so the L3 notebooks read them per role:
#   /anycompany/agentcore/config/inference/<role>/provider   (String)
#   /anycompany/agentcore/config/inference/<role>/model_id   (String)
#   /anycompany/agentcore/config/inference/<role>/api_key    (SecureString, partners only)
#
# role is "orchestrator" or "subagents".
# ---------------------------------------------------------------------------

# Agent roles shown in the inference layer, in the SAME order as the L2
# notebook: configure & test the sub-agents first, then the orchestrator.
# The evaluator (L7 judge) is not a choice: the L7 evaluations notebook always uses
# Claude Haiku 5.5 on Amazon Bedrock, so it has no tab here.
INFERENCE_ROLES = [
    {
        "id": "subagents",
        "label": "1 · Specialist Agents",
        "help": "Configure first. The specialist A2A agents the orchestrator calls "
                "- they use LiteLLM, so the snippet adds a BACKENDS entry.",
    },
    {
        "id": "orchestrator",
        "label": "2 · Orchestrator Agent",
        "help": "Configure second. The supervisor Harness that plans and delegates. Like "
                "the specialists, it calls its model through LiteLLM, so the snippet sets a "
                "LiteLLM harness_model_id (plus the endpoint for a partner model).",
    },
]

# Model providers selectable for each role. model_id is the LiteLLM id the
# agents pass to LiteLLMModel(model_id=...). needs_key marks partner providers
# whose API key is captured and stored in SSM.
#
# "models" is keyed by ROLE so each role shows its own curated 3 options:
#   orchestrator -> reasoning/planning-grade models
#   subagents    -> fast, lower-cost models for bounded tasks
# NOTE: confirm exact model ids and the us./global. inference-profile prefix
# against Bedrock Model access in the target us-east-1 account before relying
# on them.
MODEL_PROVIDERS = {
    "Amazon Bedrock (AWS)": {
        "logo": "Amazon Bedrock (AWS)",          # reuse the Bedrock icon
        "needs_key": False,
        "models": {
            # The first model in each list is the default in the UI dropdown.
            # Claude and GPT-5.6 are reachable through their us. inference profiles;
            # Kimi K2.5 is on-demand. (Bedrock's Kimi K3/K3.1 start with a 0 token quota.)
            "orchestrator": {
                "Claude Opus 5": "bedrock/us.anthropic.claude-opus-5",
                "Claude Sonnet 5": "bedrock/us.anthropic.claude-sonnet-5",
            },
            "subagents": {
                "Kimi K2.5": "bedrock/moonshotai.kimi-k2.5",
                "Claude Sonnet 5": "bedrock/us.anthropic.claude-sonnet-5",
                "GPT-5.6 Luna": "bedrock/us.openai.gpt-5.6-luna",
            },
        },
    },
    # --- Partner provider: routed via LiteLLM's native "baseten/<model>" provider
    # (same as the aws-nvidia edition). LiteLLM reads the key from key_env; no
    # api_base is needed. NOTE: confirm exact model slugs on the platform's model
    # library before relying on them; slugs change as providers add/retire models.
    "Baseten": {
        "logo": "Baseten",
        "needs_key": True,
        "key_env": "BASETEN_API_KEY",
        "models": {
            # Slugs from Baseten's Model APIs catalog (docs.baseten.co/development/model-apis/overview).
            "orchestrator": {
                "Kimi K3.1 Pro": "baseten/moonshotai/Kimi-K3.1-Pro",
                "GLM 5.3": "baseten/zai-org/GLM-5.3",
            },
            "subagents": {
                "Kimi K3.1 Pro": "baseten/moonshotai/Kimi-K3.1-Pro",
                "GLM 5.3": "baseten/zai-org/GLM-5.3",
            },
        },
    },
}


def litellm_setup_snippet() -> str:
    """Shared LiteLLM configuration used by BOTH roles: imports, region, and
    the InferenceBackend wrapper. Paste once into the shared setup cell before
    configuring either role."""
    return "\n".join([
        "# ┌─ L2 Inference · Shared LiteLLM setup (used by sub-agents AND orchestrator)",
        "# │ Paste into the empty 'Step 1: Shared LiteLLM setup' cell; run once, before configuring",
        "# │ either role. Installs deps, imports, sets region, and defines InferenceBackend.",
        "# └────────────────────────────────────────────────────────────",
        "!pip install -q litellm==1.83.7 --upgrade boto3",
        "",
        "import os",
        "import time",
        "import litellm",
        "",
        "# Route Bedrock calls to the right region for every LiteLLM call.",
        'REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")',
        'os.environ["AWS_REGION_NAME"] = REGION',
        'print("REGION:", REGION)',
        "",
        "",
        "class InferenceBackend:",
        '    """Pluggable inference backend powered by LiteLLM."""',
        "    def __init__(self, model: str, system_prompt: str = None,",
        "                 max_tokens: int = 1024, temperature: float = None):",
        "        self.model = model",
        "        self.system_prompt = system_prompt",
        "        self.max_tokens = max_tokens",
        "        # None = the model's own default. Newer models (Claude Sonnet 5, Opus 5,",
        "        # Haiku 5.5, GPT-5.6) reject the temperature parameter, so it is only",
        "        # sent when you set it explicitly.",
        "        self.temperature = temperature",
        "    @property",
        "    def name(self) -> str:",
        "        return self.model",
        "    def invoke(self, prompt: str, **kwargs) -> dict:",
        '        """Invoke the model and return the response text and latency."""',
        "        messages = []",
        "        if self.system_prompt:",
        '            messages.append({"role": "system", "content": self.system_prompt})',
        '        messages.append({"role": "user", "content": prompt})',
        "        start = time.time()",
        '        params = {"max_tokens": kwargs.get("max_tokens", self.max_tokens)}',
        '        temperature = kwargs.get("temperature", self.temperature)',
        "        if temperature is not None:",
        '            params["temperature"] = temperature',
        "        resp = litellm.completion(model=self.model, messages=messages, **params)",
        "        latency = time.time() - start",
        '        return {"content": resp.choices[0].message.content, "latency": latency}',
    ])


def inference_snippet(role_id: str, role_label: str, provider: str,
                      model_label: str, model_id: str) -> str:
    """Build a copy/paste-ready snippet for one role + model choice.

    The snippet is designed to drop into l2-inference/1_pluggable_inference_layer.ipynb:
      - sub-agents  -> a BACKENDS registry entry + active_backend line (LiteLLM)
      - orchestrator-> harness_model_id (LiteLLM id) + harness_api_base (only for a
                       provider with a custom endpoint, else None); L3 passes both
                       to the Harness as liteLlmModelConfig
    <<< ... >>> marks any blank the user must fill in.
    """
    meta = MODEL_PROVIDERS[provider]
    needs_key = meta.get("needs_key", False)
    api_base = meta.get("api_base", "")
    key_env = meta.get("key_env", "OPENAI_API_KEY")

    def _key_lines(role: str) -> list[str]:
        """Read the partner API key from SSM (saved by the L0 UI)."""
        return [
            "import boto3, os",
            "# Partner provider - read the API key saved by the L0 UI from SSM.",
            "ssm = boto3.client('ssm', region_name=REGION)",
            f"os.environ['{key_env}'] = ssm.get_parameter(",
            f"    Name='{CONFIG_PREFIX}/inference/{role}/api_key',",
            f"    WithDecryption=True,",
            f")['Parameter']['Value']",
            *([f"os.environ['OPENAI_API_BASE'] = \"{api_base}\""] if api_base else []),
            "",
        ]

    if role_id == "orchestrator":
        # The AgentCore Harness calls its model through LiteLLM (liteLlmModelConfig),
        # like the specialist agents, so harness_model_id is the same LiteLLM id the
        # other roles use. A bedrock/... id runs on the Harness execution role (no
        # key). A partner id (baseten/...) gets its API key from AgentCore Identity:
        # L3 stores the key saved by this UI there (apiKeyArn).
        lines = [
            f"# ┌─ L2 Inference · Orchestrator (Harness) ─ {provider} / {model_label}",
            f"# │ LiteLLM model id. The Harness calls it through LiteLLM",
            f"# │ (liteLlmModelConfig), the same routing the specialist agents use.",
            f"# │ Paste into the empty 'Step 4: Orchestrator — Configure the model' cell.",
            f"# └────────────────────────────────────────────────────────────",
        ]
        if needs_key:
            # The env vars only serve the Step 5 test call in this notebook; the
            # Harness gets the key from AgentCore Identity (set up in L3).
            lines += _key_lines("orchestrator")
            if api_base:
                lines += [f'harness_api_base = "{api_base}"   # {provider} endpoint']
            else:
                lines += [f"harness_api_base = None   # {provider}: LiteLLM's {model_id.split('/')[0]}/ provider knows the endpoint"]
        else:
            lines += ["harness_api_base = None   # Bedrock: the Harness execution role authenticates, API key not needed"]
        lines += [
            f'harness_model_id = "{model_id}"   # {model_label} (LiteLLM id)',
            'print(f"Orchestrator model set: {harness_model_id}")',
        ]
        return "\n".join(lines)

    # --- sub-agents: a complete, self-contained block the user pastes into the
    # empty "Step 2" cell and runs as-is (defines SYSTEM_PROMPT, the backend,
    # and selects it). Assumes InferenceBackend is already defined earlier. ---
    backend_key = f"{model_label.lower().replace(' ', '-').replace('.', '')}-subagents"
    lines = [
        f"# ┌─ L2 Inference · Sub-agents ─ {provider} / {model_label}",
        f"# │ Paste into the empty 'Step 2: Sub-agents — Configure the model' cell and run.",
        f"# └────────────────────────────────────────────────────────────",
    ]
    if needs_key:
        lines += _key_lines("subagents")
    lines += [
        "# Shared system prompt for the specialist agents:",
        "SYSTEM_PROMPT = (",
        '    "You are a helpful customer service agent. Be concise, empathetic, "',
        '    "and accurate. If you don\'t know the answer, say so and offer to escalate."',
        ")",
        "",
        "# Register the sub-agent backend:",
        "BACKENDS = {",
        f'    "{backend_key}": InferenceBackend(',
        f'        model="{model_id}",   # {model_label}',
        "        system_prompt=SYSTEM_PROMPT,",
        "    ),",
        "}",
        "",
        "# Select it as the active sub-agent model (the one-line swap):",
        f'active_backend = BACKENDS["{backend_key}"]',
        'print(f"Active sub-agent model: {active_backend.name}")',
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Data layer (L1): ONE notebook, swappable provider code pasted per stage.
#
# The notebook owns the constant skeleton (install, region, load sample data,
# test queries, publish handles). The UI generates provider-specific blocks for
# three paste cells:
#   "stores"    -> create the knowledge store + orders store
#   "ingest_kb" -> load/ingest the return-policy knowledge (embeddings/sync)
#   "ingest_orders" -> load the customer orders
#
# Providers differ in shape (Bedrock = managed KB sync; Couchbase = in-notebook
# chunk+embed), so each provider owns its whole store+ingest block. Both expose
# the same two downstream capabilities and publish handles under
# /anycompany/agentcore (AWS: kb_id + dynamodb_orders_table; Couchbase: a bundle
# under couchbase/).
# ---------------------------------------------------------------------------

DATA_PROVIDERS = {
    "Bedrock KB + DynamoDB (AWS)": {
        "logo": "Bedrock KB + DynamoDB (AWS)",
        "needs_key": False,
    },
    "Couchbase": {
        "logo": "Couchbase",
        "needs_key": True,
    },
}

DATA_STAGES = [
    {"id": "stores", "label": "1 · Create the data stores",
     "help": "Create the knowledge (vector) store and the orders store."},
    {"id": "ingest_kb", "label": "2 · Ingest the policy knowledge",
     "help": "Embed and index the return-policy documents."},
    {"id": "ingest_orders", "label": "3 · Load the customer orders",
     "help": "Load the structured customer orders into the orders store."},
]


def _data_snip_bedrock(stage_id: str) -> str:
    if stage_id == "stores":
        return "\n".join([
            "# ── L1 Data · Bedrock KB + DynamoDB · Create the data stores ──",
            "# Knowledge store: Bedrock Knowledge Base on OpenSearch Serverless.",
            "# Orders store:   DynamoDB CustomerOrders table.",
            "# (Full resource creation - AOSS collection/policies/index, IAM role,",
            "#  KB + data source, DynamoDB table + GSIs - is provider-specific.)",
            "#",
            "# Paste the AWS store-creation block here. Key outputs to keep:",
            "#   KB_ID        -> the Bedrock Knowledge Base id",
            "#   TABLE_NAME   -> the DynamoDB orders table name (e.g. 'CustomerOrders')",
            "# See the aws-only L1 setup notebooks for the full creation code.",
            "KB_ID = \"<<< set from the Bedrock KB you create >>>\"",
            "TABLE_NAME = \"CustomerOrders\"",
        ])
    if stage_id == "ingest_kb":
        return "\n".join([
            "# ── L1 Data · Bedrock KB · Ingest the policy knowledge ──",
            "# Bedrock embeds + indexes during a managed sync job (no manual chunking).",
            "import boto3, time",
            "agent = boto3.client('bedrock-agent', region_name=REGION)",
            "job = agent.start_ingestion_job(knowledgeBaseId=KB_ID, dataSourceId=DS_ID)",
            "# Poll until COMPLETE:",
            "jid = job['ingestionJob']['ingestionJobId']",
            "while True:",
            "    st = agent.get_ingestion_job(knowledgeBaseId=KB_ID, dataSourceId=DS_ID,",
            "                                 ingestionJobId=jid)['ingestionJob']['status']",
            "    print('ingestion:', st)",
            "    if st in ('COMPLETE', 'FAILED'): break",
            "    time.sleep(10)",
        ])
    # ingest_orders
    return "\n".join([
        "# ── L1 Data · DynamoDB · Load the customer orders ──",
        "# ORDERS is loaded by the shared skeleton from sample-data/customer_orders_data.json.",
        "import boto3",
        "from boto3.dynamodb.types import TypeSerializer",
        "ser = TypeSerializer()",
        "ddb = boto3.client('dynamodb', region_name=REGION)",
        "for o in ORDERS:",
        "    ddb.put_item(TableName=TABLE_NAME,",
        "                 Item={k: ser.serialize(v) for k, v in o.items() if v is not None})",
        "print(f'Loaded {len(ORDERS)} orders into {TABLE_NAME}')",
    ])


def _data_snip_couchbase(stage_id: str) -> str:
    cred = f"{CONFIG_PREFIX}/data/api_key"
    if stage_id == "stores":
        return "\n".join([
            "# ── L1 Data · Couchbase · Create the data stores ──",
            "# Capella cluster + bucket + scopes + collections (orders, policy_chunks)",
            "# and the vector index. Paste the Couchbase setup block here.",
            "import boto3, os",
            "ssm = boto3.client('ssm', region_name=REGION)",
            f"CB_API_KEY = ssm.get_parameter(Name='{cred}', WithDecryption=True)['Parameter']['Value']",
            "from couchbase.cluster import Cluster",
            "from couchbase.auth import PasswordAuthenticator",
            "from couchbase.options import ClusterOptions",
            "CB_CONN = os.environ['COUCHBASE_CONNECTION_STRING']  # couchbases://...",
            "cluster = Cluster(CB_CONN, ClusterOptions(PasswordAuthenticator('apikey', CB_API_KEY)))",
            "bucket = cluster.bucket('retail')",
            "# TODO: create scopes 'support'/'knowledge', collections 'orders'/'policy_chunks',",
            "#       and 'CREATE VECTOR INDEX idx_policy_chunks_embedding ...'.",
        ])
    if stage_id == "ingest_kb":
        return "\n".join([
            "# ── L1 Data · Couchbase · Ingest the policy knowledge ──",
            "# Couchbase embeds in-notebook: chunk the docs, call Titan, upsert vectors.",
            "import boto3",
            "bedrock = boto3.client('bedrock-runtime', region_name=REGION)",
            "def embed(text):",
            "    import json",
            "    r = bedrock.invoke_model(modelId='amazon.titan-embed-text-v2:0',",
            "                             body=json.dumps({'inputText': text}))",
            "    return json.loads(r['body'].read())['embedding']",
            "kchunks = cluster.bucket('retail').scope('knowledge').collection('policy_chunks')",
            "for i, chunk in enumerate(POLICY_CHUNKS):   # POLICY_CHUNKS from the skeleton",
            "    kchunks.upsert(f'chunk::{i}', {'text': chunk, 'embedding': embed(chunk)})",
            "print(f'Embedded + upserted {len(POLICY_CHUNKS)} policy chunks')",
        ])
    # ingest_orders
    return "\n".join([
        "# ── L1 Data · Couchbase · Load the customer orders ──",
        "orders_col = cluster.bucket('retail').scope('support').collection('orders')",
        "for o in ORDERS:          # ORDERS from the shared skeleton",
        "    orders_col.upsert(o['orderId'], o)",
        "print(f'Loaded {len(ORDERS)} orders into retail.support.orders')",
    ])


def data_snippet(provider: str, stage_id: str) -> str:
    """Return the provider-specific copy/paste block for one data-layer stage."""
    if provider.startswith("Bedrock"):
        return _data_snip_bedrock(stage_id)
    if provider == "Couchbase":
        return _data_snip_couchbase(stage_id)
    return ""


# ---------------------------------------------------------------------------
# Structured data (customer orders): DynamoDB vs Couchbase.
#
# The UI generates two per-provider snippets - "load" and "test" - that paste
# into the data-layer notebook. The notebook owns the shared skeleton (region,
# load ORDERS from sample-data). No provider branching in the notebook.
# ---------------------------------------------------------------------------

STRUCTURED_PROVIDERS = {
    "DynamoDB (AWS)": {"logo": "Bedrock KB + DynamoDB (AWS)", "needs_key": False},
    "Couchbase":      {"logo": "Couchbase", "needs_key": True},
}

STRUCTURED_STAGES = [
    {"id": "load", "label": "1 · Load the orders",
     "help": "Write the sample customer orders into the chosen store."},
    {"id": "test", "label": "2 · Test query",
     "help": "Read the orders back and show results from the store."},
]


def _struct_dynamo(stage_id: str) -> str:
    if stage_id == "load":
        return "\n".join([
            "# ── Structured data · DynamoDB · Load the orders ──",
            "# Creates the CustomerOrders table (composite key + GSIs) and loads ORDERS.",
            "import boto3, json",
            "from decimal import Decimal",
            "from boto3.dynamodb.types import TypeSerializer",
            "",
            "ddb = boto3.client('dynamodb', region_name=REGION)",
            "TABLE_NAME = 'CustomerOrders'",
            "",
            "with open('./sample-data/customer_orders_table.json') as f:",
            "    tdef = json.load(f)",
            "try:",
            "    ddb.create_table(**{k: tdef[k] for k in",
            "        ('TableName','BillingMode','AttributeDefinitions','KeySchema') }|",
            "        ({'GlobalSecondaryIndexes': tdef['GlobalSecondaryIndexes']}",
            "         if 'GlobalSecondaryIndexes' in tdef else {}))",
            "    ddb.get_waiter('table_exists').wait(TableName=TABLE_NAME)",
            "    print('Table ACTIVE:', TABLE_NAME)",
            "except ddb.exceptions.ResourceInUseException:",
            "    print('Table already exists:', TABLE_NAME)",
            "",
            "ser = TypeSerializer()",
            "def _dec(o):",
            "    if isinstance(o, float): return Decimal(str(o))",
            "    if isinstance(o, dict): return {k: _dec(v) for k, v in o.items()}",
            "    if isinstance(o, list): return [_dec(i) for i in o]",
            "    return o",
            "for o in ORDERS:   # ORDERS loaded by the notebook skeleton",
            "    item = {k: ser.serialize(v) for k, v in _dec(o).items() if v is not None}",
            "    ddb.put_item(TableName=TABLE_NAME, Item=item)",
            "print(f'Loaded {len(ORDERS)} orders into {TABLE_NAME}')",
        ])
    # test
    return "\n".join([
        "# ── Structured data · DynamoDB · Test query ──",
        "import boto3, json",
        "from boto3.dynamodb.types import TypeDeserializer",
        "ddb = boto3.client('dynamodb', region_name=REGION)",
        "TABLE_NAME = 'CustomerOrders'",
        "des = TypeDeserializer()",
        "def _d(it): return {k: des.deserialize(v) for k, v in it.items()}",
        "",
        "print('Total:', ddb.scan(TableName=TABLE_NAME, Select='COUNT')['Count'], 'orders')",
        "print('\\n--- Orders for CUST-789 ---')",
        "r = ddb.query(TableName=TABLE_NAME,",
        "              KeyConditionExpression='customerId = :c',",
        "              ExpressionAttributeValues={':c': {'S': 'CUST-789'}})",
        "for it in r['Items'][:3]:",
        "    o = _d(it)",
        "    print(f\"  {o['orderId']}: {o['productName']} ({o['category']}) - ${o['totalAmount']} - {o['status']}\")",
        "",
        "print('\\n--- Lookup ORD-10001 (OrderIdIndex) ---')",
        "r = ddb.query(TableName=TABLE_NAME, IndexName='OrderIdIndex',",
        "              KeyConditionExpression='orderId = :o',",
        "              ExpressionAttributeValues={':o': {'S': 'ORD-10001'}})",
        "for it in r['Items']:",
        "    print(json.dumps(_d(it), indent=2, default=str))",
    ])


def _struct_couchbase(stage_id: str) -> str:
    cred = f"{CONFIG_PREFIX}/data/api_key"
    if stage_id == "load":
        return "\n".join([
            "# ── Structured data · Couchbase · Load the orders ──",
            "# Upserts ORDERS into retail.support.orders and creates query indexes.",
            "import boto3, os",
            "ssm = boto3.client('ssm', region_name=REGION)",
            f"CB_API_KEY = ssm.get_parameter(Name='{cred}', WithDecryption=True)['Parameter']['Value']",
            "from couchbase.cluster import Cluster",
            "from couchbase.auth import PasswordAuthenticator",
            "from couchbase.options import ClusterOptions",
            "cluster = Cluster(os.environ['COUCHBASE_CONNECTION_STRING'],",
            "                  ClusterOptions(PasswordAuthenticator('apikey', CB_API_KEY)))",
            "ORDERS_KEYSPACE = '`retail`.`support`.`orders`'",
            "orders_coll = cluster.bucket('retail').scope('support').collection('orders')",
            "",
            "for o in ORDERS:   # ORDERS loaded by the notebook skeleton",
            "    orders_coll.upsert(o['orderId'], o)",
            "print(f'Loaded {len(ORDERS)} orders into {ORDERS_KEYSPACE}')",
            "",
            "for name, field in {'idx_orders_customer':'customerId',",
            "                    'idx_orders_category':'category',",
            "                    'idx_orders_status':'status',",
            "                    'idx_orders_productname':'productName'}.items():",
            "    cluster.query(f'CREATE INDEX IF NOT EXISTS {name} ON {ORDERS_KEYSPACE}({field})').execute()",
            "    print('Index ready:', name)",
        ])
    # test
    return "\n".join([
        "# ── Structured data · Couchbase · Test query ──",
        "import json",
        "from couchbase.exceptions import DocumentNotFoundException",
        "ORDERS_KEYSPACE = '`retail`.`support`.`orders`'",
        "orders_coll = cluster.bucket('retail').scope('support').collection('orders')",
        "def run(sql, **kw):",
        "    from couchbase.options import QueryOptions",
        "    return cluster.query(sql, QueryOptions(named_parameters=kw))",
        "",
        "n = list(run(f'SELECT COUNT(*) AS n FROM {ORDERS_KEYSPACE} WHERE customerId IS NOT MISSING'))[0]['n']",
        "print(f'{ORDERS_KEYSPACE}: {n} orders')",
        "",
        "print('\\n--- Orders for CUST-789 ---')",
        "for o in run(f'SELECT o.* FROM {ORDERS_KEYSPACE} AS o WHERE o.customerId = $cid LIMIT 3', cid='CUST-789'):",
        "    print(f\"  {o['orderId']}: {o['productName']} ({o['category']}) - ${o['totalAmount']} - {o['status']}\")",
        "",
        "print('\\n--- Lookup ORD-10001 (key-value get) ---')",
        "try:",
        "    print(json.dumps(orders_coll.get('ORD-10001').content_as[dict], indent=2, default=str))",
        "except DocumentNotFoundException:",
        "    print('  Not found.')",
    ])


def structured_snippet(provider: str, stage_id: str) -> str:
    """Return the per-provider copy/paste block for a structured-data stage."""
    if provider.startswith("DynamoDB"):
        return _struct_dynamo(stage_id)
    if provider == "Couchbase":
        return _struct_couchbase(stage_id)
    return ""

# ---------------------------------------------------------------------------
# Unstructured data (return-policy knowledge): Bedrock KB vs Couchbase.
#
# The UI generates two per-provider snippets - "ingest" and "test" - that
# paste into the l1-data-knowledge/1_knowledge_base.ipynb empty cells.
# The notebook owns the shared skeleton (region, read SSM, load POLICY_DOCS,
# upload to S3). The provider-specific plumbing lives in sibling helper
# modules (bedrock_kb_helpers.py, couchbase_kb_helpers.py) and the pasted
# snippets just orchestrate those helpers.
# ---------------------------------------------------------------------------

UNSTRUCTURED_PROVIDERS = {
    "Bedrock KB (AWS)": {
        "logo": "Bedrock KB + DynamoDB (AWS)",
        "tag":  "AWS SERVICE",
        "kind": "aws",      # styling hint (pill color)
    },
    "Couchbase": {
        # Couchbase credentials are collected inline in the Partner UI
        # (Org ID, Project ID, API Token) and saved to SSM. The notebook's
        # kb.provision() reads them and provisions the free-tier cluster.
        "logo": "Couchbase",
        "tag":  "PARTNER SOLUTION",
        "kind": "key",      # styling hint (pill color)
    },
}

UNSTRUCTURED_STAGES = [
    {"id": "ingest", "label": "1 · Ingest the knowledge",
     "help": "Create the vector store, embed the policy documents, "
             "save the KB id (or equivalent) to SSM."},
    {"id": "test", "label": "2 · Test retrieval",
     "help": "Run semantic retrieval against the vector store to confirm "
             "it is ready for the Refund Agent."},
]


def _unstruct_bedrock(stage_id: str) -> str:
    if stage_id == "ingest":
        return "\n".join([
            "# \u2500\u2500 Unstructured data \u00b7 Bedrock KB \u00b7 Ingest the knowledge \u2500\u2500",
            "# Uses the helpers/bedrock_kb module to hide the OpenSearch Serverless",
            "# + Bedrock KB plumbing. Reads the pre-provisioned S3 bucket and KB IAM",
            "# role from SSM (populated by the infra CFN stack).",
            "from helpers import bedrock_kb as kb",
            "",
            "S3_BUCKET   = ssm.get_parameter(Name='/anycompany/agentcore/s3_bucket')['Parameter']['Value']",
            "KB_ROLE_ARN = ssm.get_parameter(Name='/anycompany/agentcore/kb_role_arn')['Parameter']['Value']",
            "print(f'S3 bucket:   {S3_BUCKET}')",
            "print(f'KB role ARN: {KB_ROLE_ARN}')",
            "",
            "# Bedrock KB ingests from S3, so upload POLICY_DOCS under the return-policy/ prefix.",
            "s3 = boto3.client('s3', region_name=REGION)",
            "for name, content in POLICY_DOCS.items():",
            "    s3.put_object(Bucket=S3_BUCKET, Key=f'return-policy/{name}',",
            "                  Body=content.encode('utf-8'))",
            "    print(f'  Uploaded: s3://{S3_BUCKET}/return-policy/{name}')",
            "",
            "store = kb.create_vector_store(name='composable-agents-collection')",
            "knowledge_base = kb.create_knowledge_base(",
            "    name='composable-agents-kb',",
            "    collection_arn=store['arn'],",
            "    s3_bucket=S3_BUCKET,",
            "    s3_prefix='return-policy/',",
            "    role_arn=KB_ROLE_ARN,",
            ")",
            "# kb.ingest(wait=True) blocks until the data-source sync completes, so",
            "# the test step below is safe to run as soon as this cell returns.",
            "stats = kb.ingest(knowledge_base['kb_id'], knowledge_base['data_source_id'])",
            "print(f\"Ingested: {stats['indexed']} chunks\")",
            "",
            "# Publish the KB id so downstream notebooks (L3 Refund Agent) can retrieve.",
            "ssm.put_parameter(",
            "    Name='/anycompany/agentcore/kb_id', Value=knowledge_base['kb_id'],",
            "    Type='String', Overwrite=True,",
            ")",
            "ssm.put_parameter(",
            "    Name='/anycompany/agentcore/kb_provider', Value='bedrock',",
            "    Type='String', Overwrite=True,",
            ")",
            "print('Saved /anycompany/agentcore/kb_id and /kb_provider to SSM.')",
        ])
    # test
    return "\n".join([
        "# \u2500\u2500 Unstructured data \u00b7 Bedrock KB \u00b7 Test retrieval \u2500\u2500",
        "from helpers import bedrock_kb as kb",
        "",
        "for q in [",
        "    'What is the gold-tier refund window?',",
        "    'Can I return damaged electronics?',",
        "    'How are refunds processed?',",
        "]:",
        "    print(f'\\nQuery: {q}')",
        "    for r in kb.retrieve(knowledge_base['kb_id'], q, k=3):",
        "        print(f\"  [{r['score']:.3f}] {r['text'][:300]}\")",
    ])


def _unstruct_couchbase(stage_id: str) -> str:
    if stage_id == "ingest":
        return "\n".join([
            "# \u2500\u2500 Unstructured data \u00b7 Couchbase \u00b7 Ingest the knowledge \u2500\u2500",
            "# kb.provision() deploys a Capella free-tier cluster + bucket + scopes +",
            "# collections on first run (3-5 min). Subsequent runs are no-ops.",
            "# Credentials come from the L0 Partner Selection UI (Capella Org ID,",
            "# Project ID, API Token) and are saved to SSM.",
            "from helpers import couchbase_kb as kb",
            "",
            "kb.provision()                 # one-time cluster + bucket + scopes + collections",
            "ctx = kb.connect()             # open SDK connection",
            "knowledge_base = kb.ingest_policy_docs(ctx, POLICY_DOCS)",
            "print(f\"Ingested: {knowledge_base['indexed']} chunks into {knowledge_base['chunks_keyspace']}\")",
            "",
            "# Publish KB details to SSM so downstream notebooks (L3 Refund Agent) can retrieve.",
            "kb.publish_ssm(ctx, knowledge_base)",
        ])
    # test
    return "\n".join([
        "# \u2500\u2500 Unstructured data \u00b7 Couchbase \u00b7 Test retrieval \u2500\u2500",
        "from helpers import couchbase_kb as kb",
        "",
        "for q in [",
        "    'What is the gold-tier refund window?',",
        "    'Can I return damaged electronics?',",
        "    'How are refunds processed?',",
        "]:",
        "    print(f'\\nQuery: {q}')",
        "    for r in kb.retrieve(ctx, q, k=3):",
        "        print(f\"  [{r['score']:.3f}] {r['text'][:300]}\")",
    ])


def unstructured_snippet(provider: str, stage_id: str) -> str:
    """Return the per-provider copy/paste block for an unstructured-data stage."""
    if provider.startswith("Bedrock"):
        return _unstruct_bedrock(stage_id)
    if provider == "Couchbase":
        return _unstruct_couchbase(stage_id)
    return ""


# ---------------------------------------------------------------------------
# Decision layer (L5): ONE notebook (l5-application/1_decision_models.ipynb),
# swappable provider code pasted into its "Connect" cell.
#
# The notebook owns everything provider-neutral (question builders, the noul /
# choice / score demos, the chat-model contrast, the scope gate). The only
# provider-specific code is the connect block below, which produces a `decider`
# object with the same .decide(state, questions) call for both providers
# (see l5-application/decision_models.py). Running it also publishes the choice
# to /anycompany/agentcore/decision_provider.
# ---------------------------------------------------------------------------

DECISION_PROVIDERS = {
    "Strands Decider (AWS)": {
        "logo": "Strands Decider (AWS)",
        "needs_key": False,
        "key_param": None,
        "tag": "AWS SERVICE",
        "tagline": "Open source decider on SageMaker",
        "description": "<b>A small open source decision model, running in your own account.</b> "
                       "Strands Decider picks between options and rates on a scale faster than an "
                       "LLM, with a calibrated confidence on every answer. The workshop stack "
                       "<b>pre-provisions</b> it on an Amazon SageMaker endpoint: GPU instances "
                       "first (ml.g6, g5, g6e), with a CPU fallback (ml.m5) if no GPU capacity or "
                       "quota is available. Calls use IAM, so there's no API key."
                       + _doc("https://github.com/strands-labs/strands-decider",
                              "Strands Decider on GitHub"),
    },
    "Perplexity Decisions": {
        "logo": "Perplexity Decisions",
        "needs_key": True,
        "key_param": PERPLEXITY_KEY_PARAM,   # shared with Tools > Perplexity Search
        "tag": "PARTNER SOLUTION",
        "tagline": "Hosted Decisions API",
        "description": "<b>Hosted decisions, with no infrastructure to run.</b> The Perplexity "
                       "Decisions API answers with probabilities instead of text: yes or no, one of "
                       "your options, or a level on your rubric. The workshop calls "
                       "<code>pplx-decider-latest</code> over HTTPS with your API key, the same key "
                       "the Tools layer uses. Save it below before running the notebook."
                       + _doc("https://docs.perplexity.ai/docs/decisions/quickstart",
                              "Perplexity Decisions API documentation"),
    },
}

DECISION_STAGES = [
    {"id": "connect", "label": "1 · Connect to the decision provider",
     "help": "Paste into 'Step 2: Connect to your decision provider' in l5-application/1_decision_models.ipynb. "
             "Creates `decider`, used by every later cell."},
]


def _decision_snip_strands(stage_id: str) -> str:
    return "\n".join([
        "# ┌─ L5 Decision Models · Strands Decider on SageMaker (AWS) · Connect",
        "# │ Paste into the empty 'Step 2: Connect to your decision provider' cell and run.",
        "# │ Connects to the SageMaker endpoint the workshop stack already created.",
        "# │ No API key needed — calls use your AWS (IAM) credentials.",
        "# └────────────────────────────────────────────────────────────",
        "import decision_models as dm   # shared helper; same for every provider",
        "",
        "WAIT_MINUTES = 30   # wait this long if the endpoint is still being set up",
        "",
        "# Find the endpoint and check it is ready (stops with the reason if not).",
        "decider = dm.strands_from_ssm(REGION, wait_minutes=WAIT_MINUTES)",
        "",
        "dm.publish_provider(REGION, decider)   # lets the chatbot use the same provider",
        "print('Connected:', decider.describe())   # endpoint + GPU/CPU instance",
    ])


def _decision_snip_perplexity(stage_id: str) -> str:
    return "\n".join([
        "# ┌─ L5 Decision Models · Perplexity Decisions · Connect",
        "# │ Paste into the empty 'Step 2: Connect to your decision provider' cell and run.",
        "# │ Connects to the Perplexity Decisions API with the key you saved in the",
        "# │ Partner Selection UI. The key is held in memory only, never printed.",
        "# └────────────────────────────────────────────────────────────",
        "import decision_models as dm   # shared helper; same for every provider",
        "",
        f"# Read the API key from SSM ({PERPLEXITY_KEY_PARAM}, shared with Tools).",
        "# For a cheaper model, use dm.PPLX_FLASH_MODEL.",
        "decider = dm.perplexity_from_ssm(REGION, model=dm.PPLX_MODEL)",
        "",
        "dm.publish_provider(REGION, decider)   # lets the chatbot use the same provider",
        "print('Connected:', decider.describe())   # model + last 4 chars of the key",
    ])


def decision_snippet(provider: str, stage_id: str) -> str:
    """Return the per-provider copy/paste block for a decision-layer stage."""
    if provider.startswith("Strands"):
        return _decision_snip_strands(stage_id)
    if provider.startswith("Perplexity"):
        return _decision_snip_perplexity(stage_id)
    return ""


# ---------------------------------------------------------------------------
# Orchestration > Tools (web search): connected in ONE notebook
# (l3-orchestration/1_gateway_tools.ipynb, Step 4) and used by ONE research agent for all three
# providers. Each provider is a target on the same AgentCore Gateway, so the only
# provider-specific code is the Connect cell generated here, which calls one
# function in l3-orchestration/helpers/websearch.py. Running it also publishes
# the choice to /anycompany/agentcore/websearch_provider (read by the agent).
# ---------------------------------------------------------------------------

WEBSEARCH_PROVIDERS = {
    "AgentCore Web Search (AWS)": {
        "logo": "AgentCore Web Search (AWS)",
        "key_param": None,
        "tag": "AWS SERVICE",
        "tagline": "Managed web search on the Gateway",
        "description": "<b>Live web search with nothing to run and no key to manage.</b> A fully "
                       "managed AgentCore Gateway connector backed by an Amazon-operated web index "
                       "of tens of billions of documents. Queries are served entirely within AWS, "
                       "and the Gateway authenticates with its own IAM role. Results arrive as "
                       "relevant snippets with URLs, titles and publication dates, ready to cite."
                       + _doc("https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-connector-web-search-tool.html",
                              "AgentCore Web Search documentation"),
    },
    "Brave Search": {
        "logo": "Brave Search",
        "key_param": BRAVE_KEY_PARAM,
        "tag": "PARTNER SOLUTION",
        "tagline": "Open source MCP server on AgentCore Runtime",
        "description": "<b>Open source search, hosted in your account.</b> The official Brave "
                       "Search MCP server runs on its own AgentCore Runtime behind the Gateway, "
                       "which calls it with SigV4. The workshop stack <b>pre-builds</b> the "
                       "container image, and your Brave API key lives in Secrets Manager: the "
                       "container reads it at startup, so it never appears in the image or the "
                       "runtime config."
                       + _doc("https://github.com/brave/brave-search-mcp-server",
                              "Brave Search MCP server on GitHub"),
    },
    "Perplexity Search": {
        "logo": "Perplexity Search",
        "key_param": PERPLEXITY_KEY_PARAM,
        "tag": "PARTNER SOLUTION",
        "tagline": "Hosted MCP server: search + ask",
        "description": "<b>Search plus cited answers, fully hosted.</b> Perplexity's remote MCP "
                       "server gives the Research Agent two tools: search for ranked results, and "
                       "ask for a synthesized answer with citations. The Gateway attaches your key "
                       "through an AgentCore API-key credential provider, so there's nothing to "
                       "deploy. It shares the Perplexity key with Decision Models."
                       + _doc("https://docs.perplexity.ai/docs/getting-started/integrations/mcp-server",
                              "Perplexity MCP server documentation"),
    },
}

WEBSEARCH_STAGES = [
    {"id": "connect", "label": "1 · Connect the web-search tool",
     "help": "Paste into Step 4.1 ('4.1 Connect the provider you picked') in l3-orchestration/1_gateway_tools.ipynb. "
             "Adds the tool to the AgentCore Gateway and tells the Research Agent to use it."},
]

# provider label -> (helper call, one-line description of what it does)
_WEBSEARCH_CONNECT = {
    "AgentCore Web Search (AWS)": (
        "ws.connect_agentcore(REGION)",
        ["Adds the managed Web Search connector as a Gateway target.",
         "No API key needed - the Gateway's IAM role calls it."],
    ),
    "Brave Search": (
        "ws.connect_brave(REGION, wait_minutes=20)",
        ["Starts the pre-built Brave MCP server on AgentCore Runtime and adds it",
         "as a Gateway target. Your key goes to Secrets Manager, never the image.",
         "Waits for the image if the stack is still building it."],
    ),
    "Perplexity Search": (
        "ws.connect_perplexity(REGION)",
        ["Adds Perplexity's hosted MCP server as a Gateway target, with your key in",
         "an AgentCore API-key credential provider. Nothing to deploy."],
    ),
}


def websearch_snippet(provider: str, stage_id: str) -> str:
    """Return the per-provider Connect block for the Tools (web search) layer."""
    call, notes = _WEBSEARCH_CONNECT.get(provider, (None, []))
    if not call:
        return ""
    return "\n".join(
        [f"# ┌─ L3 Tools · {provider} · Connect",
         "# │ Paste into the empty 'Step 4.1' cell in 1_gateway_tools.ipynb and run."]
        + [f"# │ {n}" for n in notes]
        + ["# └────────────────────────────────────────────────────────────",
           "from helpers import websearch as ws   # shared helper; same for every provider",
           "",
           f"WEBSEARCH = {call}",
           "",
           "# The Research Agent reads this choice from SSM (websearch_provider).",
           "# To switch providers later, paste another Connect cell and re-run it."]
    )
