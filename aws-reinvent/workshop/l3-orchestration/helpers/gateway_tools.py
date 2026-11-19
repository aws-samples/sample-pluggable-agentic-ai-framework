"""Create the Lambda-backed Order and Refund tools on the AgentCore Gateway (1_gateway_tools.ipynb).

Each tool is a Lambda function exposed through the Gateway as an MCP target.
The notebook keeps the per-tool settings in its TOOLS table and passes one
entry to each function here:

    tool = {
        "label":       "Order tool",
        "lambda_name": "anycompany_order_tools",
        "source":      "tools/order_tools.py",          # Lambda handler code
        "schema":      "tools/order_tools_schema.json",  # MCP tool definitions
        "target_name": "order-tools",                    # Gateway target name
        "description": "...",
        "role_arn":    "arn:aws:iam::...:role/...",      # pre-provisioned by CloudFormation
        "env":         {"ORDERS_TABLE": "...", "AGENT_REGION": "us-east-1"},
        # Optional:
        "packages":    ["couchbase==4.3.5"],   # pip packages bundled into the zip
        "vpc_config":  {"SubnetIds": [...], "SecurityGroupIds": [...]},  # run in a VPC
    }

Public functions:
    refund_kb_settings(region)                       -> Refund tools settings for the L1 knowledge store
    deploy_tool_lambda(tool, region)                 -> Lambda ARN
    add_gateway_target(tool, lambda_arn, gateway_id, region) -> Gateway target ID

Paths are relative to the l3-orchestration folder (the notebook's own folder).
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import boto3

COUCHBASE_SDK = "couchbase==4.3.5"   # same version as the L1 notebook


def refund_kb_settings(region: str, ssm_prefix: str = "/anycompany/agentcore") -> dict:
    """Return the Refund tools' settings for the knowledge store chosen in L1.

    L1 writes kb_provider to SSM: "bedrock" (Bedrock Knowledge Base, kb_id) or
    "couchbase" (Capella, kb_chunks_keyspace). For Couchbase the Lambda also
    bundles the Couchbase SDK and runs in the stack's egress subnet, whose NAT
    gateway IP is on the Capella allowlist. Returns {"provider", "env",
    "packages", "vpc_config"}.
    """
    ssm = boto3.client("ssm", region_name=region)

    def get(name: str) -> str | None:
        try:
            return ssm.get_parameter(Name=f"{ssm_prefix}/{name}")["Parameter"]["Value"]
        except ssm.exceptions.ParameterNotFound:
            return None

    provider = get("kb_provider") or "bedrock"
    if provider == "couchbase":
        keyspace = get("kb_chunks_keyspace")
        if not keyspace:
            raise RuntimeError("Couchbase is selected but no Couchbase knowledge base is published "
                               "(kb_chunks_keyspace). Run l1-data-knowledge/1_knowledge_base.ipynb "
                               "with the Couchbase snippets first, then re-run this cell.")
        subnet, sg = get("lambda_subnet_id"), get("lambda_sg_id")
        if not (subnet and sg):
            raise RuntimeError("The Couchbase egress subnet is missing from SSM (lambda_subnet_id, "
                               "lambda_sg_id). Redeploy the workshop stack, then re-run this cell.")
        return {
            "provider": "couchbase",
            "env": {"KB_PROVIDER": "couchbase", "CB_KEYSPACE": keyspace,
                    "CB_SSM_PREFIX": f"{ssm_prefix}/couchbase"},
            "packages": [COUCHBASE_SDK],
            "vpc_config": {"SubnetIds": [subnet], "SecurityGroupIds": [sg]},
        }
    kb_id = get("kb_id")
    if not kb_id:
        # Stop here rather than deploy a Refund tool that can't answer policy questions.
        raise RuntimeError("No Knowledge Base found at /anycompany/agentcore/kb_id. "
                           "Run l1-data-knowledge/1_knowledge_base.ipynb first, then re-run this cell.")
    return {"provider": "bedrock", "env": {"KB_PROVIDER": "bedrock", "BEDROCK_KB_ID": kb_id},
            "packages": [], "vpc_config": None}


def _build_zip(tool: dict) -> bytes:
    """Zip the handler as lambda_function.py, plus any pip packages (Linux x86_64 wheels)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("lambda_function.py", Path(tool["source"]).read_text())
        if tool.get("packages"):
            with tempfile.TemporaryDirectory() as target:
                print(f"  Bundling {', '.join(tool['packages'])}…")
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "--quiet", "--target", target,
                     "--platform", "manylinux2014_x86_64", "--implementation", "cp",
                     "--python-version", "3.12", "--only-binary=:all:", *tool["packages"]],
                    check=True)
                for f in Path(target).rglob("*"):
                    if f.is_file() and "__pycache__" not in f.parts:
                        zf.write(f, f.relative_to(target).as_posix())
    return buf.getvalue()


def deploy_tool_lambda(tool: dict, region: str) -> str:
    """Create (or update) the tool's Lambda function and let the Gateway invoke it.

    Packages tool["source"] as lambda_function.py (plus tool["packages"]), uses
    the pre-provisioned execution role, and sets tool["env"] as environment
    variables. tool["vpc_config"] places it in a VPC; without it the function
    runs outside any VPC. If the function already exists, its code,
    environment and VPC placement are updated, so a re-run picks up changes
    (for example switching the knowledge store between Bedrock and Couchbase).
    Returns the Lambda ARN.
    """
    lambda_client = boto3.client("lambda", region_name=region)
    zip_bytes = _build_zip(tool)
    name = tool["lambda_name"]
    vpc_config = tool.get("vpc_config")

    try:
        resp = lambda_client.create_function(
            FunctionName=name,
            Runtime="python3.12",
            Role=tool["role_arn"],
            Handler="lambda_function.lambda_handler",
            Code={"ZipFile": zip_bytes},
            Timeout=60,
            MemorySize=512 if tool.get("packages") else 256,
            Environment={"Variables": tool["env"]},
            **({"VpcConfig": vpc_config} if vpc_config else {}),
        )
        arn = resp["FunctionArn"]
        print(f"Created Lambda {name}" + (" in the Couchbase egress subnet" if vpc_config else ""))
    except lambda_client.exceptions.ResourceConflictException:
        arn = lambda_client.get_function(FunctionName=name)["Configuration"]["FunctionArn"]
        lambda_client.update_function_code(FunctionName=name, ZipFile=zip_bytes)
        # Wait for the code update to settle, then refresh the environment variables.
        lambda_client.get_waiter("function_updated_v2").wait(FunctionName=name)
        # An empty VpcConfig moves the function out of the VPC (Couchbase -> Bedrock).
        lambda_client.update_function_configuration(
            FunctionName=name,
            Environment={"Variables": tool["env"]},
            MemorySize=512 if tool.get("packages") else 256,
            VpcConfig=vpc_config or {"SubnetIds": [], "SecurityGroupIds": []},
        )
        print(f"Updated Lambda {name}" + (" in the Couchbase egress subnet" if vpc_config else ""))

    try:
        lambda_client.add_permission(
            FunctionName=name,
            StatementId="AllowGatewayInvoke",
            Action="lambda:InvokeFunction",
            Principal="bedrock-agentcore.amazonaws.com",
        )
        print("  Gateway may now invoke it.")
    except lambda_client.exceptions.ResourceConflictException:
        print("  Gateway invoke permission already in place.")
    return arn


def _wait_for_lambda(lambda_client, name: str, timeout_s: int = 300) -> None:
    # A function in a VPC stays Pending while its network interfaces are created.
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        fn = lambda_client.get_function_configuration(FunctionName=name)
        if fn.get("State") == "Active" and fn.get("LastUpdateStatus", "Successful") in ("Successful", None):
            return
        time.sleep(5)
    raise TimeoutError(f"Lambda {name} did not become active within {timeout_s}s.")


def _find_target(control, gateway_id: str, target_name: str) -> str | None:
    resp = control.list_gateway_targets(gatewayIdentifier=gateway_id)
    for t in resp.get("items", resp.get("targets", [])):
        if t.get("name") == target_name:
            return t["targetId"]
    return None


def add_gateway_target(tool: dict, lambda_arn: str, gateway_id: str, region: str,
                       timeout_s: int = 100) -> str:
    """Expose the tool's Lambda as an MCP target on the Gateway and wait until it's READY.

    The tool definitions (names, descriptions, input schemas) come from
    tool["schema"]. The Gateway calls the Lambda with its own IAM role. If the
    target already exists, it's reused. Returns the Gateway target ID.
    """
    control = boto3.client("bedrock-agentcore-control", region_name=region)
    _wait_for_lambda(boto3.client("lambda", region_name=region), tool["lambda_name"])

    schema = json.loads(Path(tool["schema"]).read_text())
    try:
        resp = control.create_gateway_target(
            gatewayIdentifier=gateway_id,
            name=tool["target_name"],
            description=tool["description"],
            targetConfiguration={"mcp": {"lambda": {
                "lambdaArn": lambda_arn,
                "toolSchema": {"inlinePayload": schema},
            }}},
            credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
        )
        target_id = resp["targetId"]
        print(f"Created Gateway target {tool['target_name']}")
    except control.exceptions.ConflictException:
        target_id = _find_target(control, gateway_id, tool["target_name"])
        if not target_id:
            raise RuntimeError(f"Gateway target {tool['target_name']!r} exists but could not be found.")
        print(f"Gateway target {tool['target_name']} already exists")

    deadline = time.time() + timeout_s
    status = None
    while time.time() < deadline:
        status = control.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)["status"]
        if status == "READY":
            break
        time.sleep(5)
    if status != "READY":
        raise TimeoutError(f"Gateway target {tool['target_name']} is {status} after {timeout_s}s.")

    names = ", ".join(t["name"] for t in schema)
    print(f"  READY. Tools on the Gateway: {names}")
    return target_id
