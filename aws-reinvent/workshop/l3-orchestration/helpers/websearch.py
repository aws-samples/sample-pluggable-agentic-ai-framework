"""Pluggable web-search tools for the L3 Research Agent.

One research agent, three interchangeable search providers. Each provider is
wired in as a target on the existing Cognito-JWT ``anycompany-agent-gateway``,
so the agent calls whichever one is active with the same MCP ``tools/call``:

    =============  =========================================  =================  =========
    provider       gateway target                             outbound auth      API key
    =============  =========================================  =================  =========
    agentcore      AWS-managed Web Search connector           Gateway IAM role   none
    perplexity     Perplexity hosted MCP (api.perplexity.ai)  API-key provider   Perplexity
    brave          Brave Search MCP server on AgentCore Runtime Gateway IAM role   Brave
    =============  =========================================  =================  =========

The L0 Partner Selection UI (Orchestration -> Tools) generates a one-line
Connect cell for Step 4 of ``1_gateway_tools.ipynb`` that calls exactly one of:

    ws.connect_agentcore(REGION)
    ws.connect_perplexity(REGION)
    ws.connect_brave(REGION)

Each one creates (or updates) its Gateway target, waits for it to be READY,
and publishes ``websearch_provider`` / ``websearch_target`` to SSM — the only
thing the Research Agent reads to know which provider to use. Re-running with a
different provider switches the agent within a minute; no redeploy needed.

What the CloudFormation stack already provides (so this module never touches IAM):
  - Gateway role permissions for all three outbound paths.
  - The Brave MCP server container image, pre-built in the background
    (``brave_mcp_build_status`` / ``brave_mcp_image_uri`` in SSM).
  - The Brave MCP runtime role (``brave_mcp_runtime_role_arn``).

API keys are entered once in the L0 UI and stored as SSM SecureStrings. The
Perplexity key is shared with the L5 Decision Models layer. Keys are read into
memory only for the duration of a connect call and never printed.
"""

import json
import time
import uuid
from typing import Any, Dict, Optional
from urllib.parse import quote

import boto3
import requests
from botocore.exceptions import ClientError

SSM_PREFIX = "/anycompany/agentcore"
CONFIG_PREFIX = f"{SSM_PREFIX}/config"

# Where the L0 UI stores the partner keys. The Perplexity key is shared with the
# L5 Decision Models layer (one key for both).
PERPLEXITY_KEY_PARAM = f"{CONFIG_PREFIX}/perplexity/api_key"
BRAVE_KEY_PARAM = f"{CONFIG_PREFIX}/brave/api_key"

# What the Research Agent reads.
PROVIDER_PARAM = f"{SSM_PREFIX}/websearch_provider"
TARGET_PARAM = f"{SSM_PREFIX}/websearch_target"

# Gateway target name per provider. Tools are namespaced "<target>___<tool>".
TARGETS = {"agentcore": "web-search", "brave": "brave", "perplexity": "perplexity"}

# provider -> (raw search tool, args builder). Kept in sync with the agent.
SEARCH_TOOLS = {
    "agentcore": ("WebSearch", lambda q, n: {"query": q[:200], "maxResults": n}),
    "brave": ("brave_web_search", lambda q, n: {"query": q, "count": n}),
    "perplexity": ("perplexity_search", lambda q, n: {"query": q}),
}

LABELS = {"agentcore": "AgentCore Web Search (AWS)", "brave": "Brave Search",
          "perplexity": "Perplexity Search"}

# AgentCore Web Search connector — regions where it is available.
AGENTCORE_WEBSEARCH_REGIONS = {"us-east-1", "us-east-2", "us-west-2", "eu-west-1",
                               "ap-southeast-2", "ap-northeast-1", "us-gov-west-1"}

PERPLEXITY_MCP_ENDPOINT = "https://api.perplexity.ai/mcp"
PERPLEXITY_CRED_PROVIDER = "anycompany-perplexity-api-key"

BRAVE_SECRET_NAME = "anycompany/brave-search-api-key"
BRAVE_RUNTIME_NAME = "brave_mcp_server"
BRAVE_BUILD_STATUS_PARAM = f"{SSM_PREFIX}/brave_mcp_build_status"
BRAVE_IMAGE_URI_PARAM = f"{SSM_PREFIX}/brave_mcp_image_uri"
BRAVE_ROLE_PARAM = f"{SSM_PREFIX}/brave_mcp_runtime_role_arn"
BRAVE_RUNTIME_ARN_PARAM = f"{SSM_PREFIX}/brave_mcp_runtime_arn"


class WebSearchSetupError(RuntimeError):
    """A connect step failed with an actionable message."""


# ---------------------------------------------------------------------------
# Small AWS helpers
# ---------------------------------------------------------------------------
def _ssm(region: str):
    return boto3.client("ssm", region_name=region)


def _ctl(region: str):
    return boto3.client("bedrock-agentcore-control", region_name=region)


def _get_param(region: str, name: str, *, decrypt: bool = False, default: Optional[str] = None) -> Optional[str]:
    try:
        return _ssm(region).get_parameter(Name=name, WithDecryption=decrypt)["Parameter"]["Value"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "ParameterNotFound":
            return default
        raise


def _put_param(region: str, name: str, value: str, *, secure: bool = False) -> None:
    _ssm(region).put_parameter(Name=name, Value=value, Type="SecureString" if secure else "String",
                               Overwrite=True)


def _require_key(region: str, param: str, label: str) -> str:
    key = (_get_param(region, param, decrypt=True) or "").strip()
    if not key:
        raise WebSearchSetupError(
            f"No {label} API key found at {param}. Save it in the Partner Selection UI "
            f"(Orchestration -> Tools -> {label}) and run this cell again.")
    return key


def _gateway_id(region: str) -> str:
    gw = _get_param(region, f"{SSM_PREFIX}/gateway_id")
    if not gw:
        raise WebSearchSetupError(f"{SSM_PREFIX}/gateway_id not found. Is the workshop stack deployed?")
    return gw


def _find_target(ctl, gateway_id: str, name: str) -> Optional[str]:
    token = None
    while True:
        kw = {"gatewayIdentifier": gateway_id, "maxResults": 100}
        if token:
            kw["nextToken"] = token
        resp = ctl.list_gateway_targets(**kw)
        for t in resp.get("items", []):
            if t.get("name") == name:
                return t["targetId"]
        token = resp.get("nextToken")
        if not token:
            return None


def _upsert_target(region: str, name: str, description: str,
                   target_cfg: Dict[str, Any], cred_cfg: list) -> str:
    """Create the Gateway target, or update it in place if it already exists."""
    ctl, gw = _ctl(region), _gateway_id(region)
    target_id = _find_target(ctl, gw, name)
    if target_id:
        ctl.update_gateway_target(gatewayIdentifier=gw, targetId=target_id, name=name,
                                  description=description, targetConfiguration=target_cfg,
                                  credentialProviderConfigurations=cred_cfg)
        print(f"  updated gateway target '{name}' ({target_id})")
    else:
        target_id = ctl.create_gateway_target(
            gatewayIdentifier=gw, name=name, description=description,
            targetConfiguration=target_cfg, credentialProviderConfigurations=cred_cfg)["targetId"]
        print(f"  created gateway target '{name}' ({target_id})")
    _wait_target_ready(ctl, gw, target_id, name)
    return target_id


def _wait_target_ready(ctl, gateway_id: str, target_id: str, name: str, timeout_s: int = 400) -> None:
    deadline = time.time() + timeout_s
    status = "?"
    while time.time() < deadline:
        t = ctl.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
        status = t.get("status", "?")
        if status == "READY":
            print(f"  gateway target '{name}' is READY")
            return
        if status in ("FAILED", "UPDATE_UNSUCCESSFUL", "SYNCHRONIZE_UNSUCCESSFUL"):
            reasons = "; ".join(t.get("statusReasons", []) or []) or "no reason returned"
            raise WebSearchSetupError(f"Gateway target '{name}' is {status}: {reasons}")
        time.sleep(10)
    raise WebSearchSetupError(f"Gateway target '{name}' not READY after {timeout_s}s (last: {status}).")


def _publish(region: str, provider: str, target_id: str) -> Dict[str, str]:
    _put_param(region, PROVIDER_PARAM, provider)
    _put_param(region, TARGET_PARAM, TARGETS[provider])
    info = {"provider": provider, "label": LABELS[provider], "target": TARGETS[provider],
            "target_id": target_id,
            "tool": f"{TARGETS[provider]}___{SEARCH_TOOLS[provider][0]}"}
    print(f"Connected: {info['label']}  (gateway tool {info['tool']})")
    return info


# ---------------------------------------------------------------------------
# 1. AgentCore Web Search (AWS) — managed connector, no key
# ---------------------------------------------------------------------------
def connect_agentcore(region: str) -> Dict[str, str]:
    """Add the AWS-managed Web Search connector as a Gateway target."""
    if region not in AGENTCORE_WEBSEARCH_REGIONS:
        raise WebSearchSetupError(
            f"AgentCore Web Search is not available in {region}. Use one of: "
            f"{', '.join(sorted(AGENTCORE_WEBSEARCH_REGIONS))} — or pick Brave / Perplexity.")
    print("Connecting AgentCore Web Search…")
    target_id = _upsert_target(
        region, TARGETS["agentcore"],
        "AWS-managed Web Search connector (Amazon web index, queries stay in AWS)",
        {"mcp": {"connector": {"source": {"connectorId": "web-search"},
                               "configurations": [{"name": "WebSearch", "parameterValues": {}}]}}},
        [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
    )
    return _publish(region, "agentcore", target_id)


# ---------------------------------------------------------------------------
# 2. Perplexity Search — hosted remote MCP server + API-key credential provider
# ---------------------------------------------------------------------------
def connect_perplexity(region: str) -> Dict[str, str]:
    """Register Perplexity's hosted MCP server, with the key in an AgentCore
    API-key credential provider (the Gateway adds it as a Bearer header)."""
    print("Connecting Perplexity Search…")
    key = _require_key(region, PERPLEXITY_KEY_PARAM, "Perplexity")
    ctl = _ctl(region)
    try:
        ctl.create_api_key_credential_provider(name=PERPLEXITY_CRED_PROVIDER, apiKey=key)
        print(f"  created credential provider '{PERPLEXITY_CRED_PROVIDER}'")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        exists = code == "ConflictException" or (
            code == "ValidationException" and "already exists" in str(e).lower())
        if not exists:
            raise
        # Re-running (or rotating the key): update the existing provider in place.
        ctl.update_api_key_credential_provider(name=PERPLEXITY_CRED_PROVIDER, apiKey=key)
        print(f"  updated credential provider '{PERPLEXITY_CRED_PROVIDER}'")
    finally:
        del key
    provider_arn = ctl.get_api_key_credential_provider(name=PERPLEXITY_CRED_PROVIDER)["credentialProviderArn"]
    target_id = _upsert_target(
        region, TARGETS["perplexity"],
        "Perplexity hosted MCP server (search / ask)",
        {"mcp": {"mcpServer": {"endpoint": PERPLEXITY_MCP_ENDPOINT}}},
        [{"credentialProviderType": "API_KEY",
          "credentialProvider": {"apiKeyCredentialProvider": {
              "providerArn": provider_arn, "credentialLocation": "HEADER",
              "credentialParameterName": "Authorization", "credentialPrefix": "Bearer"}}}],
    )
    return _publish(region, "perplexity", target_id)


# ---------------------------------------------------------------------------
# 3. Brave Search — open-source MCP server on its own AgentCore Runtime
# ---------------------------------------------------------------------------
def _wait_brave_image(region: str, wait_minutes: int) -> str:
    """The stack pre-builds the Brave MCP image in the background. Wait for it."""
    deadline = time.time() + wait_minutes * 60
    while True:
        status = _get_param(region, BRAVE_BUILD_STATUS_PARAM, default="")
        if status == "READY":
            return _get_param(region, BRAVE_IMAGE_URI_PARAM)
        if status.startswith("FAILED"):
            raise WebSearchSetupError(
                f"The Brave MCP image build failed ({status}). Check the "
                f"'agent-stack-brave-mcp-build' CodeBuild project logs.")
        if not status:
            raise WebSearchSetupError(
                f"{BRAVE_BUILD_STATUS_PARAM} not found — the workshop stack may predate the Brave "
                "image build. Redeploy the stack, or pick AgentCore Web Search / Perplexity.")
        if time.time() > deadline:
            raise WebSearchSetupError(
                f"Brave MCP image still {status} after {wait_minutes} min. Re-run this cell to keep waiting.")
        print(f"  Brave MCP image: {status} — waiting…")
        time.sleep(20)


def _upsert_brave_secret(region: str, key: str) -> str:
    """Store the Brave key in Secrets Manager (the container reads it at startup)."""
    sm = boto3.client("secretsmanager", region_name=region)
    try:
        return sm.create_secret(Name=BRAVE_SECRET_NAME, SecretString=key,
                                Description="Brave Search API key for the workshop Brave MCP server")["ARN"]
    except ClientError as e:
        code, msg = e.response["Error"]["Code"], str(e)
        if code == "InvalidRequestException" and "scheduled for deletion" in msg:
            sm.restore_secret(SecretId=BRAVE_SECRET_NAME)  # left over from a previous teardown
        elif code != "ResourceExistsException":
            raise
    sm.put_secret_value(SecretId=BRAVE_SECRET_NAME, SecretString=key)
    return sm.describe_secret(SecretId=BRAVE_SECRET_NAME)["ARN"]


def _find_runtime(ctl, name: str) -> Optional[Dict[str, Any]]:
    token = None
    while True:
        resp = ctl.list_agent_runtimes(**({"nextToken": token} if token else {}))
        for r in resp.get("agentRuntimes", []):
            if r.get("agentRuntimeName") == name:
                return r
        token = resp.get("nextToken")
        if not token:
            return None


def connect_brave(region: str, wait_minutes: int = 20) -> Dict[str, str]:
    """Run the pre-built Brave MCP server on AgentCore Runtime and register it."""
    print("Connecting Brave Search…")
    image_uri = _wait_brave_image(region, wait_minutes)
    role_arn = _get_param(region, BRAVE_ROLE_PARAM)
    if not role_arn:
        raise WebSearchSetupError(f"{BRAVE_ROLE_PARAM} not found — redeploy the workshop stack.")

    key = _require_key(region, BRAVE_KEY_PARAM, "Brave")
    try:
        secret_arn = _upsert_brave_secret(region, key)
    finally:
        del key
    print(f"  Brave key stored in Secrets Manager ({BRAVE_SECRET_NAME})")

    ctl = _ctl(region)
    runtime_args = dict(
        agentRuntimeArtifact={"containerConfiguration": {"containerUri": image_uri}},
        roleArn=role_arn,
        networkConfiguration={"networkMode": "PUBLIC"},
        protocolConfiguration={"serverProtocol": "MCP"},
        # Only the secret's ARN is passed; the key itself never leaves Secrets Manager.
        environmentVariables={"BRAVE_SECRET_ID": secret_arn, "AWS_REGION": region},
    )
    existing = _find_runtime(ctl, BRAVE_RUNTIME_NAME)
    if existing:
        runtime_id = existing["agentRuntimeId"]
        ctl.update_agent_runtime(agentRuntimeId=runtime_id, **runtime_args)
        print(f"  updated runtime '{BRAVE_RUNTIME_NAME}'")
    else:
        runtime_id = ctl.create_agent_runtime(agentRuntimeName=BRAVE_RUNTIME_NAME, **runtime_args)["agentRuntimeId"]
        print(f"  created runtime '{BRAVE_RUNTIME_NAME}'")

    deadline = time.time() + 600
    while True:
        rt = ctl.get_agent_runtime(agentRuntimeId=runtime_id)
        if rt["status"] == "READY":
            break
        if "FAILED" in rt["status"]:
            raise WebSearchSetupError(f"Brave runtime {rt['status']}: {rt.get('failureReason', '')}")
        if time.time() > deadline:
            raise WebSearchSetupError(f"Brave runtime not READY after 10 min (last: {rt['status']}).")
        time.sleep(10)
    runtime_arn = rt["agentRuntimeArn"]
    _put_param(region, BRAVE_RUNTIME_ARN_PARAM, runtime_arn)
    print(f"  runtime '{BRAVE_RUNTIME_NAME}' is READY")

    endpoint = (f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/"
                f"{quote(runtime_arn, safe='')}/invocations")
    target_id = _upsert_target(
        region, TARGETS["brave"],
        "Brave Search MCP server on AgentCore Runtime",
        {"mcp": {"mcpServer": {"endpoint": endpoint}}},
        [{"credentialProviderType": "GATEWAY_IAM_ROLE",
          "credentialProvider": {"iamCredentialProvider": {"service": "bedrock-agentcore"}}}],
    )
    info = _publish(region, "brave", target_id)
    info["runtime_arn"] = runtime_arn
    return info


# ---------------------------------------------------------------------------
# Inspect, test and clean up
# ---------------------------------------------------------------------------
def active(region: str) -> Dict[str, Optional[str]]:
    """The provider and Gateway target the Research Agent currently uses."""
    provider = _get_param(region, PROVIDER_PARAM)
    return {"provider": provider, "label": LABELS.get(provider or ""),
            "target": _get_param(region, TARGET_PARAM)}


def _gateway_token(region: str) -> str:
    client_id = _get_param(region, f"{SSM_PREFIX}/cognito_client_id")
    password = _get_param(region, f"{SSM_PREFIX}/user_password", decrypt=True)
    # WORKSHOP ONLY: shared test-user password via USER_PASSWORD_AUTH.
    resp = boto3.client("cognito-idp", region_name=region).initiate_auth(
        ClientId=client_id, AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": "gold_customer", "PASSWORD": password})
    return resp["AuthenticationResult"]["IdToken"]


def _mcp(region: str, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    url = _get_param(region, f"{SSM_PREFIX}/gateway_url")
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {_gateway_token(region)}"}
    body = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
    resp = requests.post(url, headers=headers, json=body, timeout=90)
    resp.raise_for_status()
    return resp.json()


def smoke_test(region: str, query: str = "Amazon Bedrock AgentCore Web Search", max_results: int = 3) -> Any:
    """List the active provider's tools on the Gateway, then run one search
    through it — the exact path the Research Agent will use."""
    info = active(region)
    if not info["provider"]:
        raise WebSearchSetupError("No web-search provider connected yet — run the Connect cell first.")
    prefix = f"{info['target']}___"
    listed = _mcp(region, "tools/list", {}).get("result", {}).get("tools", [])
    mine = sorted(t["name"] for t in listed if t["name"].startswith(prefix))
    print(f"Provider: {info['label']}  |  tools on the gateway: {mine or 'none found'}")
    raw_tool, build_args = SEARCH_TOOLS[info["provider"]]
    resp = _mcp(region, "tools/call", {"name": prefix + raw_tool, "arguments": build_args(query, max_results)})
    if "error" in resp:
        raise WebSearchSetupError(f"Search failed: {resp['error']}")
    result = resp.get("result", {})
    if result.get("isError"):
        raise WebSearchSetupError(f"Search tool returned an error: {json.dumps(result)[:500]}")
    texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
    print(f"Search OK — {len(texts)} content block(s). First 600 chars:\n")
    print((texts[0] if texts else "")[:600])
    return result


def disconnect(region: str) -> None:
    """Remove every web-search target, the Brave runtime, the Perplexity
    credential provider, the Brave secret and the related SSM parameters.
    The Brave image and IAM roles belong to the stack and are left alone."""
    ctl = _ctl(region)
    gw = _get_param(region, f"{SSM_PREFIX}/gateway_id")
    if gw:
        for name in TARGETS.values():
            tid = _find_target(ctl, gw, name)
            if tid:
                ctl.delete_gateway_target(gatewayIdentifier=gw, targetId=tid)
                print(f"deleted gateway target '{name}'")
    rt = _find_runtime(ctl, BRAVE_RUNTIME_NAME)
    if rt:
        ctl.delete_agent_runtime(agentRuntimeId=rt["agentRuntimeId"])
        print(f"deleted runtime '{BRAVE_RUNTIME_NAME}'")
    try:
        ctl.delete_api_key_credential_provider(name=PERPLEXITY_CRED_PROVIDER)
        print(f"deleted credential provider '{PERPLEXITY_CRED_PROVIDER}'")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
    try:
        boto3.client("secretsmanager", region_name=region).delete_secret(
            SecretId=BRAVE_SECRET_NAME, ForceDeleteWithoutRecovery=True)
        print(f"deleted secret '{BRAVE_SECRET_NAME}'")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
    names = [PROVIDER_PARAM, TARGET_PARAM, BRAVE_RUNTIME_ARN_PARAM, f"{SSM_PREFIX}/websearch_gateway_target_id"]
    _ssm(region).delete_parameters(Names=names)
    print("deleted web-search SSM parameters")
