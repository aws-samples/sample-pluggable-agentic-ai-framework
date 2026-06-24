"""Refund Agent — A2A Server for AgentCore Runtime.

Exposes the Refund Agent via the A2A protocol so it can be discovered
and invoked by other agents or orchestrators.

The Refund Agent calls tools via the AgentCore Gateway.
Gateway inbound auth: Cognito token (obtained at startup).
Gateway outbound auth: IAM role (Gateway invokes Lambda).
"""
import os
import logging
import json
import uuid

import boto3
import httpx
import uvicorn
from fastapi import FastAPI
from strands import Agent, tool
from strands.models.litellm import LiteLLMModel
from strands.multiagent.a2a import A2AServer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration from SSM Parameter Store
# ---------------------------------------------------------------------------
SSM_PREFIX = os.environ.get("SSM_PREFIX", "/anycompany/agentcore")

ssm_client = boto3.client("ssm")


def _get_ssm(name: str, default: str = None) -> str:
    try:
        return ssm_client.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
    except ssm_client.exceptions.ParameterNotFound:
        if default is not None:
            return default
        raise


AWS_REGION = boto3.session.Session().region_name or "us-east-1"
# LiteLLM uses AWS_REGION_NAME (not AWS_REGION) for Bedrock routing.
# Set it before any LiteLLMModel call so it picks the right region.
os.environ.setdefault("AWS_REGION_NAME", AWS_REGION)

MODEL_ID = _get_ssm(f"{SSM_PREFIX}/model_id")
GATEWAY_URL = _get_ssm(f"{SSM_PREFIX}/gateway_url")
COGNITO_USER_POOL_ID = _get_ssm(f"{SSM_PREFIX}/cognito_user_pool_id", default="")
COGNITO_CLIENT_ID = _get_ssm(f"{SSM_PREFIX}/cognito_client_id", default="")
# Workshop test-user password — stored in SSM as SecureString
# (encrypted at rest with KMS). The _get_ssm helper passes WithDecryption=True.
USER_PASSWORD = _get_ssm(f"{SSM_PREFIX}/user_password", default="")

runtime_url = os.environ.get("AGENTCORE_RUNTIME_URL", "http://127.0.0.1:9000/")
host, port = os.environ.get("AGENT_HOST", "127.0.0.1"), 9000  # nosec B104 - configurable; containers override via AGENT_HOST=0.0.0.0

logger.info(f"Config loaded — Model: {MODEL_ID}, Region: {AWS_REGION}")
logger.info(f"Gateway URL: {GATEWAY_URL}")


# ---------------------------------------------------------------------------
# Get a Cognito token at startup for Gateway auth
# ---------------------------------------------------------------------------
def _get_gateway_token() -> str:
    if not COGNITO_CLIENT_ID:
        logger.warning("Cognito not configured")
        return ""
    cognito = boto3.client("cognito-idp", region_name=AWS_REGION)
    resp = cognito.initiate_auth(
        ClientId=COGNITO_CLIENT_ID,
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": "gold_customer", "PASSWORD": USER_PASSWORD},
    )
    token = resp["AuthenticationResult"]["IdToken"]
    logger.info("Obtained Cognito token for Gateway auth")
    return token


ACCESS_TOKEN = _get_gateway_token()


# ---------------------------------------------------------------------------
# Gateway helper — calls tools via Gateway with the token
# ---------------------------------------------------------------------------
def _call_gateway_tool(tool_name: str, arguments: dict) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {ACCESS_TOKEN}",
    }

    mcp_request = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "id": str(uuid.uuid4()),
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }

    try:
        resp = httpx.post(GATEWAY_URL, headers=headers, json=mcp_request, timeout=30.0)
        resp.raise_for_status()
        result = resp.json()

        if "error" in result:
            return {"status": "error", "message": result["error"].get("message", str(result["error"]))}

        content = result.get("result", {}).get("content", [])
        for item in content:
            if item.get("type") == "text":
                try:
                    return json.loads(item["text"])
                except json.JSONDecodeError:
                    return {"status": "success", "result": item["text"]}

        return {"status": "success", "raw": result.get("result", result)}

    except httpx.HTTPStatusError as e:
        logger.error(f"Gateway error: {e.response.status_code}")
        return {"status": "error", "message": f"Gateway returned {e.response.status_code}"}
    except Exception as e:
        logger.error(f"Gateway call failed: {e}")
        return {"status": "error", "message": str(e)}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@tool
def refund_policy(question: str) -> dict:
    """Retrieve refund policy information from the knowledge base.

    Args:
        question: The customer's question about refund policy.
    """
    return _call_gateway_tool("refund-tools___refund_policy", {"question": question})


@tool
def check_eligible(
    order_id: str,
    order_status: str,
    item: str,
    reason: str,
    damage_level: str = "",
) -> dict:
    """Check whether an order is eligible for a refund.

    Args:
        order_id: The order identifier (e.g. ORD-12345).
        order_status: Current order status (e.g. delivered, shipped).
        item: Name of the product.
        reason: Reason for the refund request.
        damage_level: Severity of damage if applicable.
    """
    args = {"order_id": order_id, "order_status": order_status, "item": item, "reason": reason}
    if damage_level:
        args["damage_level"] = damage_level
    return _call_gateway_tool("refund-tools___check_eligible", args)


@tool
def process_refund(
    order_id: str,
    refund_amount: float,
    reason: str,
    payment_method: str = "original_payment",
) -> dict:
    """Process an approved refund for an order.

    Args:
        order_id: The order identifier.
        refund_amount: Dollar amount to refund.
        reason: Reason for the refund.
        payment_method: Where to send the refund.
    """
    return _call_gateway_tool("refund-tools___process_refund", {
        "order_id": order_id,
        "refund_amount": refund_amount,
        "reason": reason,
        "payment_method": payment_method,
    })


# ---------------------------------------------------------------------------
# Agent + A2A Server
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are the Refund Agent for a retail customer-support system.

Your responsibilities:
1. Answer questions about refund policies using the refund_policy tool.
2. Evaluate whether an order qualifies for a refund using the check_eligible tool.
3. Process approved refunds using the process_refund tool.

Rules:
- Always check the refund policy before determining eligibility.
- Never process a refund without first confirming eligibility.
- If photo evidence is required, indicate that to the caller and do NOT process the refund yet.
- Return structured, factual information.
- Be concise and helpful.
"""

bedrock_model = LiteLLMModel(model_id=MODEL_ID)

agent = Agent(
    model=bedrock_model,
    tools=[refund_policy, check_eligible, process_refund],
    system_prompt=SYSTEM_PROMPT,
    name="Refund Agent",
    description="Handles refund policy queries, eligibility evaluation, and refund processing.",
)

a2a_server = A2AServer(
    agent=agent,
    http_url=runtime_url,
    serve_at_root=True,
)

app = FastAPI()


@app.get("/ping")
def ping():
    return {"status": "healthy"}


app.mount("/", a2a_server.to_fastapi_app())

if __name__ == "__main__":
    uvicorn.run(app, host=host, port=port)
