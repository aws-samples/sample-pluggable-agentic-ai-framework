"""Order Agent — A2A Server for AgentCore Runtime.

Exposes the Order Agent via the A2A protocol so it can be discovered
and invoked by other agents or orchestrators.

The Order Agent calls tools via the AgentCore Gateway.
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
def check_order_details(
    order_id: str = "",
    customer_id: str = "",
) -> dict:
    """Look up order details or customer orders from the database.

    Provide at least one parameter:
    - order_id: Look up a specific order (e.g. ORD-10001)
    - customer_id: Get all orders for a customer (e.g. CUST-789)

    Args:
        order_id: The order identifier (e.g. ORD-10001).
        customer_id: The customer identifier (e.g. CUST-789).
    """
    args = {}
    if order_id:
        args["order_id"] = order_id
    if customer_id:
        args["customer_id"] = customer_id

    if not args:
        return {"status": "error", "message": "Provide at least one of: order_id or customer_id."}

    return _call_gateway_tool("order-tools___check_order_details", args)


# ---------------------------------------------------------------------------
# Agent + A2A Server
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are the Order Agent for a retail customer-support system.

Your responsibilities:
1. Look up order details by order ID (e.g. ORD-10001).
2. Retrieve all orders for a given customer ID (e.g. CUST-789).

Rules:
- Use the check_order_details tool to answer all queries.
- If the user asks about an order, pass the order_id parameter.
- If the user asks about their orders or a customer's orders, pass the customer_id parameter.
- Return structured, factual information from the database.
- Do not make up order data — only return what the database provides.
- Be concise and helpful.
"""

model = LiteLLMModel(model_id=MODEL_ID)

agent = Agent(
    model=model,
    tools=[check_order_details],
    system_prompt=SYSTEM_PROMPT,
    name="Order Agent",
    description="Handles order lookups and customer order history.",
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
