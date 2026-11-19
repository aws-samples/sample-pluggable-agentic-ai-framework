import json, os, time, boto3, logging
from datetime import datetime, timedelta

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = os.environ.get("AGENT_REGION", "us-east-1")
# Knowledge store chosen in L1: "bedrock" (Bedrock Knowledge Base) or "couchbase" (Capella).
KB_PROVIDER = os.environ.get("KB_PROVIDER", "bedrock")
KB_ID = os.environ.get("BEDROCK_KB_ID", "")
kb_client = boto3.client("bedrock-agent-runtime", region_name=REGION) if KB_ID else None
_cb_cluster = None


def _couchbase():
    """Connect to Capella once per container, with the credentials L1 saved in SSM."""
    global _cb_cluster
    if _cb_cluster is None:
        from couchbase.auth import PasswordAuthenticator
        from couchbase.cluster import Cluster
        from couchbase.options import ClusterOptions
        ssm = boto3.client("ssm", region_name=REGION)
        prefix = os.environ["CB_SSM_PREFIX"]

        def get(key, decrypt=False):
            return ssm.get_parameter(Name=f"{prefix}/{key}", WithDecryption=decrypt)["Parameter"]["Value"]

        opts = ClusterOptions(PasswordAuthenticator(get("username"), get("password", True)))
        opts.apply_profile("wan_development")
        _cb_cluster = Cluster(get("connection_string"), opts)
        _cb_cluster.wait_until_ready(timedelta(seconds=20))
    return _cb_cluster


def query_couchbase(query, max_results=3):
    """Same retrieval as L1's couchbase_kb.retrieve(): Titan embedding + SQL++ vector search."""
    from couchbase.options import QueryOptions
    emb = boto3.client("bedrock-runtime", region_name=REGION).invoke_model(
        modelId="amazon.titan-embed-text-v2:0", contentType="application/json", accept="application/json",
        body=json.dumps({"inputText": query, "dimensions": 1024, "normalize": True}))
    qvec = json.loads(emb["body"].read())["embedding"]
    sql = (f'SELECT c.text FROM {os.environ["CB_KEYSPACE"]} AS c '
           'ORDER BY APPROX_VECTOR_DISTANCE(c.embedding, $qvec, "cosine") '
           f'LIMIT {int(max_results)}')
    rows = _couchbase().query(sql, QueryOptions(named_parameters={"qvec": qvec}))
    passages = [r["text"] for r in rows if r.get("text")]
    return "\n---\n".join(passages) if passages else "No relevant policy found."


def query_kb(query, max_results=3):
    if KB_PROVIDER == "couchbase":
        return query_couchbase(query, max_results)
    if not KB_ID or not kb_client:
        return "Knowledge Base not configured."
    resp = kb_client.retrieve(
        knowledgeBaseId=KB_ID,
        retrievalQuery={"text": query},
        retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": max_results}},
    )
    passages = [r.get("content", {}).get("text", "") for r in resp.get("retrievalResults", []) if r.get("content", {}).get("text")]
    return "\n---\n".join(passages) if passages else "No relevant policy found."


def handle_refund_policy(params):
    return {"status": "success", "policy_info": query_kb(params.get("question", ""))}


def handle_check_eligible(params):
    order_id = params.get("order_id", "")
    order_status = params.get("order_status", "")
    reason = params.get("reason", "")
    damage_level = params.get("damage_level", "")
    eligible_statuses = {"delivered", "shipped", "return_requested"}
    if order_status.lower() not in eligible_statuses:
        return {"eligible": "no", "order_id": order_id, "message": f"Status '{order_status}' not eligible."}
    if reason.lower() in ["damaged on arrival", "defective product"] and not damage_level:
        return {"eligible": "pending_evidence", "order_id": order_id, "message": "Photo evidence required."}
    return {"eligible": "yes", "order_id": order_id, "message": f"Order {order_id} is eligible for refund."}


def handle_process_refund(params):
    order_id = params.get("order_id", "")
    amount = float(params.get("refund_amount", 0))
    reason = params.get("reason", "")
    prefix = order_id.replace("ORD-", "")
    refund_id = f"REF-{prefix}-{int(time.time())}"
    eta = (datetime.utcnow() + timedelta(days=5)).strftime("%Y-%m-%d")
    return {"refund_approved": True, "refund_id": refund_id, "order_id": order_id, "amount": f"${amount:.2f}", "eta": eta, "reason": reason}


def lambda_handler(event, context):
    """Handle tool calls from Gateway.
    
    Gateway passes tool arguments directly as the event payload.
    We detect which tool based on the parameters present.
    """
    logger.info(f"Tool invoked - request_id: {context.aws_request_id}")
    
    if "question" in event:
        result = handle_refund_policy(event)
    elif "refund_amount" in event:
        result = handle_process_refund(event)
    elif "order_id" in event:
        result = handle_check_eligible(event)
    else:
        result = {"error": f"Cannot determine tool from event keys: {list(event.keys())}"}
    
    logger.info(f"Tool completed - status: {result.get('status', 'unknown')}")
    return result
