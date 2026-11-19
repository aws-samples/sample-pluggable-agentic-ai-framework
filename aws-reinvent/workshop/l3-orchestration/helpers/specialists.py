"""Deploy, test, and register the L3 specialist agents (2_specialist_agents.ipynb).

Every specialist agent (Order, Refund, Research) goes through the same three
steps. Only a few settings differ per agent; the notebook keeps those in its
AGENTS table and passes one entry to each function here:

    agent = {
        "label":            "Order Agent",
        "agent_name":       "order_agent_a2a",                  # AgentCore Runtime name
        "entrypoint":       "agents/order_agent_a2a.py",
        "requirements":     "agents/requirements_order_a2a.txt",
        "role_param":       "order_agent_runtime_role_arn",     # SSM key of the runtime role
        "service_name":     "anycompany-order-agent",           # Datadog service name
        "record_name":      "order_agent_a2a_record",           # AgentCore Registry record
        "display_name":     "Order Agent (A2A)",
        "description":      "...",
        "test_prompt":      "What is the status of order ORD-10001?",
        "test_as_customer": True,                               # send a Cognito token
    }

Public functions:
    deploy_agent(agent, region, obs_provider)      -> runtime ARN
    test_agent(agent, agent_arn, region)           -> answer text
    register_agent(agent, agent_arn, region)       -> registry record id

deploy_agent uses the AgentCore starter toolkit, which writes its config and a
Dockerfile into the current directory and resolves the entrypoint relative to
it. Call it from the l3-orchestration folder (the notebook's own folder).
"""
from __future__ import annotations

import json
import os
import time
import uuid
from urllib.parse import quote

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from .observability import datadog_otel_env, mask_secret

SSM_PREFIX = "/anycompany/agentcore"


def _get_ssm(ssm, name: str) -> str:
    return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]


def _runtime_url(region: str, agent_arn: str, path: str = "invocations") -> str:
    return (f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/"
            f"{quote(agent_arn, safe='')}/{path}")


def _signed(region: str, method: str, url: str, headers: dict, body: str | None = None) -> dict:
    """Headers with a SigV4 signature for the AgentCore Runtime endpoint."""
    creds = boto3.Session(region_name=region).get_credentials().get_frozen_credentials()
    req = AWSRequest(method=method, url=url, data=body, headers=headers)
    SigV4Auth(creds, "bedrock-agentcore", region).add_auth(req)
    return dict(req.headers)


# ---------------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------------
def deploy_agent(agent: dict, region: str, obs_provider: str, *, ssm_prefix: str = SSM_PREFIX,
                 wait_minutes: int = 10) -> str:
    """Configure the agent as an A2A server on AgentCore Runtime, launch it, and wait.

    The runtime role is pre-provisioned by CloudFormation and read from SSM.
    With obs_provider == "datadog", managed ADOT is turned off and the
    OTEL-direct environment variables are passed, so the agent's own exporter
    (agents/dd_otel.py) sends spans straight to Datadog. Logs always stay in
    CloudWatch. Returns the runtime ARN; raises if the deployment fails.
    """
    from bedrock_agentcore_starter_toolkit import Runtime

    ssm = boto3.client("ssm", region_name=region)

    # The starter toolkit writes its config and Dockerfile into this folder.
    # Clear them so one agent's settings never leak into the next deploy.
    for leftover in (".bedrock_agentcore.yaml", "Dockerfile"):
        if os.path.exists(leftover):
            os.remove(leftover)

    role_arn = _get_ssm(ssm, f"{ssm_prefix}/{agent['role_param']}")
    print(f"Deploying {agent['label']}  (runtime role: {role_arn.split('/')[-1]})")

    runtime = Runtime()
    configure_kwargs = dict(
        entrypoint=agent["entrypoint"],
        execution_role=role_arn,
        auto_create_ecr=True,
        requirements_file=agent["requirements"],
        region=region,
        agent_name=agent["agent_name"],
        protocol="A2A",
    )
    if obs_provider == "datadog":
        configure_kwargs["disable_otel"] = True
    runtime.configure(**configure_kwargs)

    if obs_provider == "datadog":
        otel_env = datadog_otel_env(ssm, agent["service_name"], ssm_prefix)
        # Managed ADOT ignores configure(disable_otel=True) in CodeBuild mode;
        # DISABLE_ADOT_OBSERVABILITY=true is the documented switch that turns it off,
        # leaving the app-owned OTLP exporter as the only trace destination.
        otel_env["DISABLE_ADOT_OBSERVABILITY"] = "true"
        key = otel_env["OTEL_EXPORTER_OTLP_TRACES_HEADERS"].split("dd-api-key=", 1)[-1]
        print(f"  Datadog OTEL-direct: service={otel_env['OTEL_SERVICE_NAME']}  dd-api-key={mask_secret(key)}")
        launch_result = runtime.launch(auto_update_on_conflict=True, env_vars=otel_env)
    else:
        launch_result = runtime.launch(auto_update_on_conflict=True)

    print("  Waiting for deployment", end="")
    status = None
    for _ in range(wait_minutes * 6):
        status = runtime.status().endpoint["status"]
        if status in ("ACTIVE", "READY", "FAILED"):
            break
        print(".", end="", flush=True)
        time.sleep(10)
    print(f"\n  Status: {status}")
    if status == "FAILED":
        raise RuntimeError(f"{agent['label']} deployment failed. Check the AgentCore console.")
    if status not in ("ACTIVE", "READY"):
        raise TimeoutError(f"{agent['label']} not ready after {wait_minutes} min (status {status}). "
                           "Check the AgentCore console, then re-run this cell.")
    print(f"  ARN: {launch_result.agent_arn}")
    return launch_result.agent_arn


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------
_customer_tokens: dict[str, str] = {}


def customer_token(region: str, *, ssm_prefix: str = SSM_PREFIX, username: str = "gold_customer") -> str:
    """Cognito ID token for a workshop test user, cached per user.

    WORKSHOP ONLY: disposable Cognito test user; the password is read from SSM.
    PRODUCTION REQUIREMENT: replace USER_PASSWORD_AUTH with federated identity (SAML/OIDC).
    See: https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-identity-federation.html
    """
    if username not in _customer_tokens:
        ssm = boto3.client("ssm", region_name=region)
        resp = boto3.client("cognito-idp", region_name=region).initiate_auth(
            ClientId=_get_ssm(ssm, f"{ssm_prefix}/cognito_client_id"),
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": username,
                            "PASSWORD": _get_ssm(ssm, f"{ssm_prefix}/user_password")},
        )
        _customer_tokens[username] = resp["AuthenticationResult"]["IdToken"]
    return _customer_tokens[username]


def _answer_text(resp_text: str) -> str:
    """Text parts of an A2A reply: one JSON-RPC object, or newline-delimited events."""
    try:
        docs = [json.loads(resp_text)]
    except json.JSONDecodeError:
        docs = []
        for line in resp_text.splitlines():
            try:
                docs.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    texts = []
    for doc in docs:
        if "error" in doc:
            return "❌ Error: " + json.dumps(doc["error"], indent=2)
        for artifact in doc.get("result", {}).get("artifacts", []):
            texts += [p["text"] for p in artifact.get("parts", []) if p.get("kind") == "text"]
    return "\n".join(texts)


def test_agent(agent: dict, agent_arn: str, region: str, prompt: str | None = None, *,
               ssm_prefix: str = SSM_PREFIX, timeout: int = 180) -> str:
    """Send one A2A message/send to the agent's runtime (SigV4-signed) and print the answer.

    Agents that act on a customer's behalf (agent["test_as_customer"]) also get
    a Cognito token for the gold_customer test user, as the Harness would pass.
    """
    prompt = prompt or agent["test_prompt"]
    url = _runtime_url(region, agent_arn)
    body = json.dumps({
        "jsonrpc": "2.0",
        "method": "message/send",
        "id": str(uuid.uuid4()),
        "params": {"message": {"role": "user", "messageId": str(uuid.uuid4()),
                               "parts": [{"kind": "text", "text": prompt}]}},
    })
    headers = {"Content-Type": "application/json",
               "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": str(uuid.uuid4())}
    if agent.get("test_as_customer"):
        headers["Authorization"] = f"Bearer {customer_token(region, ssm_prefix=ssm_prefix)}"

    print(f"{agent['label']} ← {prompt}\n")
    resp = requests.post(url, data=body, headers=_signed(region, "POST", url, headers, body), timeout=timeout)
    if resp.status_code != 200:
        message = f"❌ Error {resp.status_code}: {resp.text[:1000]}"
        print(message)
        return message
    # NOTE: PII in tool responses is masked by the L4 guardrail interceptor
    # (l4-security/1_sensitive_data_masking.ipynb). Production code should also redact before logging.
    answer = _answer_text(resp.text)
    print(answer[:2000])
    return answer


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------
def register_agent(agent: dict, agent_arn: str, region: str, *, ssm_prefix: str = SSM_PREFIX) -> str:
    """Create the agent's AgentCore Registry record from its A2A agent card and approve it.

    Fetches the card from the runtime, creates the record with the card inline
    (or finds the existing one), waits for it to settle, and submits it for
    approval if it isn't already approved. Returns the record id.
    """
    ssm = boto3.client("ssm", region_name=region)
    registry = boto3.client("agent-registry-control", region_name=region)
    registry_id = _get_ssm(ssm, f"{ssm_prefix}/registry_id")

    card_url = _runtime_url(region, agent_arn, "invocations/.well-known/agent-card.json")
    headers = {"X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": str(uuid.uuid4())}
    card_resp = requests.get(card_url, headers=_signed(region, "GET", card_url, headers), timeout=60)
    card_resp.raise_for_status()
    agent_card = card_resp.json()
    print(f"Agent card: {agent_card.get('name', 'unknown')}")

    record_id = None
    try:
        resp = registry.create_registry_record(
            registryId=registry_id,
            name=agent["record_name"],
            displayName=agent["display_name"],
            description=agent["description"],
            recordType="AGENT",
            recordVersion="1.0",
            descriptors={"a2aAgentCard": {"data": json.dumps(agent_card)}},
        )
        record_id = resp["recordArn"].rsplit("/", 1)[-1]
        print(f"Created registry record: {agent['record_name']} ({record_id})")
    except registry.exceptions.ConflictException:
        for page in registry.get_paginator("list_registry_records").paginate(registryId=registry_id):
            for rec in page.get("registryRecords", []):
                if rec.get("name") == agent["record_name"]:
                    record_id = rec["recordId"]
            if record_id:
                break
        if not record_id:
            raise RuntimeError(f"Registry record {agent['record_name']!r} exists but could not be found.")
        print(f"Registry record already exists: {agent['record_name']} ({record_id})")

    for _ in range(20):
        rec = registry.get_registry_record(registryId=registry_id, recordId=record_id)
        if rec["status"] not in ("CREATING", "UPDATING"):
            break
        time.sleep(2)
    if rec["status"] not in ("APPROVED", "PENDING_APPROVAL", "SUBMITTED"):
        registry.submit_registry_record_for_approval(registryId=registry_id, recordId=record_id)
    print(f"Record status: {rec['status']}")
    return record_id
