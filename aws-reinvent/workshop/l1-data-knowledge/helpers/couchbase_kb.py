"""
L1 — Unstructured data · Couchbase helpers.

Four public functions mirroring the Bedrock KB helper surface:

    connect() -> ctx                                # reads SSM creds, returns {cluster, collection, keyspace, ...}
    ingest_policy_docs(ctx, policy_docs) -> kb      # chunks + embeds + indexes + waits until queryable
    publish_ssm(ctx, kb) -> None                    # writes /anycompany/agentcore/kb_* for L3
    retrieve(ctx, query, k=5) -> [{score, text, ...}]

All waits (vector index build) are inside ``ingest_policy_docs``. By the time
it returns, ``retrieve`` is guaranteed to return rows.

Prerequisite: ``c0_couchbase_setup_free_tier.ipynb`` has been run in this
account + region. It writes ``/anycompany/agentcore/couchbase/*`` to SSM and
creates the ``retail.knowledge.policy_chunks`` collection in Capella.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import timedelta
from typing import Any

import boto3

# ---------------------------------------------------------------------------
# Config — matched to the Bedrock KB side so retrieval quality is comparable.
# ---------------------------------------------------------------------------

EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"
EMBEDDING_DIMENSIONS = 1024
SIMILARITY = "cosine"

CHUNK_MAX_CHARS = 600
CHUNK_OVERLAP_CHARS = 100

VECTOR_INDEX = "idx_policy_chunks_embedding"

SSM_COUCHBASE_PREFIX = "/anycompany/agentcore/couchbase"
SSM_KB_PREFIX = "/anycompany/agentcore"

# Capella free-tier provisioning config
CAPELLA_API_BASE   = "https://cloudapi.cloud.couchbase.com/v4"
CAPELLA_REGION     = "us-east-2"                       # only US region offered for free tier
CLUSTER_NAME       = f"agentic-workshop-free-{CAPELLA_REGION}"
BUCKET_NAME        = "retail"
BUCKET_MEMORY_MB   = 512
SUPPORT_SCOPE      = "support"
ORDERS_COLLECTION  = "orders"
KNOWLEDGE_SCOPE    = "knowledge"
DOCS_COLLECTION    = "policy_docs"
CHUNKS_COLLECTION  = "policy_chunks"
CB_USERNAME        = "workshop_app"
ALLOWLIST_HOURS    = 72


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def provision(verbose: bool = True) -> dict:
    """
    Idempotent Capella free-tier provisioning.

    First run (3-5 min): deploys the cluster, allowlists the Studio IP,
    creates the DB credential, bucket, scopes, and collections, then saves
    every derived value to SSM. Subsequent runs are no-ops (seconds).

    Reads from SSM (set via the L0 Partner Selection UI):
        /anycompany/agentcore/couchbase/org_id
        /anycompany/agentcore/couchbase/project_id
        /anycompany/agentcore/couchbase/api_token

    Writes to SSM:
        connection_string, username, password, bucket, support_scope,
        orders_collection, knowledge_scope, chunks_collection

    Any step that the Capella Management API refuses raises a RuntimeError
    with instructions for the Capella UI equivalent. Re-running after the
    manual step is safe because every step is idempotent.
    """
    import secrets
    import string

    region = (
        os.environ.get("AWS_DEFAULT_REGION")
        or boto3.session.Session().region_name
        or "us-east-1"
    )
    ssm = boto3.client("ssm", region_name=region)

    def _say(msg: str) -> None:
        if verbose:
            print(msg)

    # ---- Step 1: credentials ----
    _say("[1/7] Reading Capella credentials from SSM...")
    org_id      = _ssm_require(ssm, "org_id")
    project_id  = _ssm_require(ssm, "project_id")
    api_token   = _ssm_require(ssm, "api_token", decrypt=True)
    project_path = f"/organizations/{org_id}/projects/{project_id}"

    # ---- Step 2: verify API token ----
    _say("[2/7] Verifying API token against Capella...")
    try:
        proj = _api_call("GET", project_path, api_token)
    except _CapellaAPIError as e:
        if e.status == 401:
            raise RuntimeError(
                "Capella rejected the API token (401 Unauthorized). "
                "Regenerate it in the Capella UI (project -> Settings -> API Keys) "
                "with role 'Project Owner' and save it in the L0 UI again."
            ) from e
        if e.status == 404:
            raise RuntimeError(
                f"Capella could not find the project {org_id}/{project_id} (404). "
                "Check the Org ID and Project ID in the L0 UI."
            ) from e
        raise
    _say(f"       Project: {proj.get('name', project_id)}")

    # ---- Step 3: deploy cluster (or reuse existing) ----
    _say(f"[3/7] Deploying/reusing free-tier cluster '{CLUSTER_NAME}'...")
    cluster_id = _ensure_cluster(project_path, api_token, CLUSTER_NAME, _say)
    cluster_path = f"{project_path}/clusters/{cluster_id}"

    # ---- Step 4: wait for cluster to become healthy ----
    cluster_info = _wait_cluster_healthy(cluster_path, api_token, _say)
    conn_str = cluster_info["connectionString"]
    if not conn_str.startswith("couchbase"):
        conn_str = f"couchbases://{conn_str}"
    ssm.put_parameter(
        Name=f"{SSM_COUCHBASE_PREFIX}/connection_string",
        Value=conn_str, Type="String", Overwrite=True,
    )

    # ---- Step 5: allowlist this notebook's egress IP ----
    _say("[5/7] Adding this notebook's egress IP to the allowlist...")
    _ensure_allowlist(cluster_path, api_token, CLUSTER_NAME, _say)

    # ---- Step 6: DB credential + bucket + scopes + collections ----
    _say(f"[6/7] Creating credential, bucket, scopes, collections...")
    password = _ensure_db_credential(ssm, cluster_path, api_token, _say)
    bucket_id = _ensure_bucket(cluster_path, api_token, _say)
    _ensure_scopes_and_collections(cluster_path, api_token, bucket_id, _say)

    # ---- Step 7: persist derived values to SSM ----
    _say("[7/7] Saving derived values to SSM...")
    derived = {
        "username":           (CB_USERNAME, "String"),
        "password":           (password, "SecureString"),
        "bucket":             (BUCKET_NAME, "String"),
        "support_scope":      (SUPPORT_SCOPE, "String"),
        "orders_collection":  (ORDERS_COLLECTION, "String"),
        "knowledge_scope":    (KNOWLEDGE_SCOPE, "String"),
        "chunks_collection":  (CHUNKS_COLLECTION, "String"),
        "cluster_id":         (cluster_id, "String"),
    }
    for key, (val, kind) in derived.items():
        ssm.put_parameter(
            Name=f"{SSM_COUCHBASE_PREFIX}/{key}",
            Value=val, Type=kind, Overwrite=True,
        )

    _say("\nProvisioning complete.")
    return {
        "cluster_name":    CLUSTER_NAME,
        "cluster_id":      cluster_id,
        "connection":      conn_str,
        "bucket":          BUCKET_NAME,
        "chunks_keyspace": f"`{BUCKET_NAME}`.`{KNOWLEDGE_SCOPE}`.`{CHUNKS_COLLECTION}`",
    }




def connect() -> dict:
    """
    Connect to the Capella cluster whose settings were saved by c0.

    Returns a context dict used by all other functions. Called once in the
    ingest cell and once in the test cell — subsequent calls reuse TCP.
    """
    # Imports deferred so the module is cheap to import for Bedrock pickers
    # who never run this code path and may not have couchbase installed.
    from couchbase.auth import PasswordAuthenticator
    from couchbase.cluster import Cluster
    from couchbase.options import ClusterOptions

    region = (
        os.environ.get("AWS_DEFAULT_REGION")
        or boto3.session.Session().region_name
        or "us-east-1"
    )
    ssm = boto3.client("ssm", region_name=region)

    def ssm_get(key: str, decrypt: bool = False, required: bool = True, default: Any = None):
        try:
            return ssm.get_parameter(
                Name=f"{SSM_COUCHBASE_PREFIX}/{key}", WithDecryption=decrypt,
            )["Parameter"]["Value"]
        except ssm.exceptions.ParameterNotFound:
            if required:
                raise RuntimeError(
                    f"Missing SSM parameter {SSM_COUCHBASE_PREFIX}/{key}. "
                    "Run c0_couchbase_setup_free_tier.ipynb in this account + region first."
                )
            return default

    conn_str  = ssm_get("connection_string")
    username  = ssm_get("username")
    password  = ssm_get("password", decrypt=True)
    bucket    = ssm_get("bucket",            required=False, default="retail")
    scope     = ssm_get("knowledge_scope",   required=False, default="knowledge")
    coll_name = ssm_get("chunks_collection", required=False, default="policy_chunks")

    opts = ClusterOptions(PasswordAuthenticator(username, password))
    opts.apply_profile("wan_development")  # longer timeouts for internet connections
    cluster = Cluster(conn_str, opts)
    cluster.wait_until_ready(timedelta(seconds=30))

    chunks_coll = cluster.bucket(bucket).scope(scope).collection(coll_name)
    # Probe collection existence up front so a missing collection throws here,
    # not partway through ingest.
    try:
        chunks_coll.exists("__probe__")
    except Exception as e:
        raise RuntimeError(
            f"Cannot reach `{bucket}`.`{scope}`.`{coll_name}` on the cluster. "
            "Run c0_couchbase_setup_free_tier.ipynb in this account + region first.\n"
            f"Underlying error: {e}"
        )

    chunks_keyspace = f"`{bucket}`.`{scope}`.`{coll_name}`"
    print(f"  Connected: {conn_str}")
    print(f"  Chunks keyspace: {chunks_keyspace}")

    return {
        "cluster": cluster,
        "chunks_coll": chunks_coll,
        "chunks_keyspace": chunks_keyspace,
        "bucket": bucket,
        "scope": scope,
        "collection": coll_name,
        "region": region,
    }


def ingest_policy_docs(ctx: dict, policy_docs: dict[str, str]) -> dict:
    """
    Chunk, embed, upsert chunks, create vector index, and wait until the index
    serves results. By the time this returns, ``retrieve`` is guaranteed to work.

    policy_docs: {filename: text}
    Returns: {indexed, chunks_keyspace, vector_index, embedding_model,
              embedding_dimensions, similarity}
    """
    from couchbase.exceptions import CouchbaseException
    from couchbase.n1ql import QueryScanConsistency
    from couchbase.options import QueryOptions

    cluster = ctx["cluster"]
    chunks_coll = ctx["chunks_coll"]
    chunks_keyspace = ctx["chunks_keyspace"]
    region = ctx["region"]

    bedrock_runtime = boto3.client("bedrock-runtime", region_name=region)

    # 1. Chunk + embed + upsert (idempotent per source doc)
    total_chunks = 0
    for filename, text in policy_docs.items():
        chunks = _split_into_chunks(text)
        source_key = f"policydoc::{filename}"
        # Remove any prior chunks for this source doc so re-runs don't accumulate.
        cluster.query(
            f"DELETE FROM {chunks_keyspace} WHERE source_doc_key = $k",
            QueryOptions(
                named_parameters={"k": source_key},
                scan_consistency=QueryScanConsistency.REQUEST_PLUS,
            ),
        ).execute()
        for i, chunk_text in enumerate(chunks):
            chunks_coll.upsert(
                f"{source_key}::chunk::{i:03d}",
                {
                    "type": "policy_chunk",
                    "source_doc_key": source_key,
                    "title": filename.replace("-", " ").replace("_", " ").title(),
                    "source": filename,
                    "chunk_index": i,
                    "text": chunk_text,
                    "char_count": len(chunk_text),
                    "embedding": _embed(bedrock_runtime, chunk_text),
                    "embedding_model": EMBEDDING_MODEL,
                },
            )
        print(f"  + {filename}: {len(chunks)} chunks embedded and saved")
        total_chunks += len(chunks)

    # 2. Create the vector index
    try:
        cluster.query(
            f'''CREATE VECTOR INDEX IF NOT EXISTS {VECTOR_INDEX}
                ON {chunks_keyspace}(embedding VECTOR)
                WITH {{"dimension": {EMBEDDING_DIMENSIONS}, "similarity": "{SIMILARITY.upper()}", "description": "IVF,SQ8"}}'''
        ).execute()
        print(f"  Vector index ready: {VECTOR_INDEX}")
    except CouchbaseException as e:
        raise RuntimeError(
            "Vector index creation failed. Capella free tier needs Couchbase "
            "Server 8.0+ with the Index service enabled.\n"
            f"Underlying error: {e}"
        ) from e

    # 3. Probe the index with a real vector search until it returns rows.
    #    Any wait belongs here so the test cell runs clean, no retries on screen.
    print("  Waiting for vector index to become queryable...")
    probe_vec = _embed(bedrock_runtime, "return policy")
    probe_sql = f"""
        SELECT APPROX_VECTOR_DISTANCE(c.embedding, $qvec, "{SIMILARITY}") AS d
        FROM {chunks_keyspace} AS c
        ORDER BY APPROX_VECTOR_DISTANCE(c.embedding, $qvec, "{SIMILARITY}")
        LIMIT 1
    """
    for attempt in range(12):  # 60 s total
        try:
            rows = list(cluster.query(
                probe_sql,
                QueryOptions(named_parameters={"qvec": probe_vec}),
            ))
            if rows:
                print(f"  Index queryable after {attempt * 5}s")
                break
        except Exception:
            pass  # transient during index build; keep polling
        time.sleep(5)
    else:
        raise RuntimeError(
            "Vector index did not become queryable within 60 s. "
            "Re-run this cell or check the Capella UI for the index status."
        )

    return {
        "indexed": total_chunks,
        "chunks_keyspace": chunks_keyspace,
        "vector_index": VECTOR_INDEX,
        "embedding_model": EMBEDDING_MODEL,
        "embedding_dimensions": EMBEDDING_DIMENSIONS,
        "similarity": SIMILARITY,
    }


def publish_ssm(ctx: dict, kb: dict) -> None:
    """Save KB details to SSM so the L3 Refund Agent can locate them."""
    ssm = boto3.client("ssm", region_name=ctx["region"])
    params = {
        "kb_provider":            "couchbase",
        "kb_chunks_keyspace":     kb["chunks_keyspace"],
        "kb_vector_index":        kb["vector_index"],
        "kb_embedding_model":     kb["embedding_model"],
        "kb_embedding_dimensions": str(kb["embedding_dimensions"]),
        "kb_similarity":          kb["similarity"],
    }
    for key, val in params.items():
        ssm.put_parameter(
            Name=f"{SSM_KB_PREFIX}/{key}", Value=val,
            Type="String", Overwrite=True,
        )
        print(f"  {SSM_KB_PREFIX}/{key} = {val}")


def retrieve(ctx: dict, query: str, k: int = 5) -> list[dict]:
    """Embed the query, run SQL++ vector search, return the top-k scored rows."""
    from couchbase.options import QueryOptions

    cluster = ctx["cluster"]
    chunks_keyspace = ctx["chunks_keyspace"]
    bedrock_runtime = boto3.client("bedrock-runtime", region_name=ctx["region"])
    qvec = _embed(bedrock_runtime, query)

    sql = f"""
        SELECT c.title, c.source, c.chunk_index, c.text,
               APPROX_VECTOR_DISTANCE(c.embedding, $qvec, "{SIMILARITY}") AS distance
        FROM {chunks_keyspace} AS c
        ORDER BY APPROX_VECTOR_DISTANCE(c.embedding, $qvec, "{SIMILARITY}")
        LIMIT {k}
    """
    rows = list(cluster.query(sql, QueryOptions(named_parameters={"qvec": qvec})))
    # Cosine distance is in [0, 2]; convert to a 0-1 score similar to the
    # Bedrock KB side (where larger = more relevant).
    return [
        {
            "score": max(0.0, 1.0 - float(r["distance"])),
            "text": r["text"],
            "source": r.get("source", ""),
            "title": r.get("title", ""),
            "chunk_index": r.get("chunk_index", 0),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _split_into_chunks(text: str, max_chars: int = CHUNK_MAX_CHARS,
                       overlap: int = CHUNK_OVERLAP_CHARS) -> list[str]:
    """Paragraph-then-sentence chunking with word-boundary overlap.

    Matches the sample in couchbase-samplecode/c1_data_layer.ipynb Step 8.
    """
    # 1. Paragraphs, with long paragraphs split on sentence boundaries.
    pieces: list[str] = []
    for para in [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]:
        if len(para) <= max_chars:
            pieces.append(para)
            continue
        buf = ""
        for sentence in re.split(r"(?<=[.!?])\s+", para):
            if buf and len(buf) + 1 + len(sentence) > max_chars:
                pieces.append(buf)
                buf = sentence
            else:
                buf = f"{buf} {sentence}".strip()
        if buf:
            pieces.append(buf)

    # 2. Cut anything still over the limit by length.
    sized: list[str] = []
    for piece in pieces:
        while len(piece) > max_chars:
            sized.append(piece[:max_chars])
            piece = piece[max_chars - overlap:]
        sized.append(piece)

    # 3. Pack into chunks with word-boundary overlap between consecutive chunks.
    chunks: list[str] = []
    current = ""
    for piece in sized:
        if current and len(current) + 2 + len(piece) > max_chars:
            chunks.append(current)
            tail = current[-overlap:]
            # Start the overlap at a word boundary so chunks don't begin mid-word.
            tail = tail[tail.find(" ") + 1:] if " " in tail else tail
            current = (f"{tail}\n\n{piece}"
                       if len(tail) + 2 + len(piece) <= max_chars else piece)
        else:
            current = f"{current}\n\n{piece}" if current else piece
    if current:
        chunks.append(current)
    return chunks


def _embed(bedrock_runtime, text: str) -> list[float]:
    """Create one Titan embedding (1024-dim, normalized)."""
    resp = bedrock_runtime.invoke_model(
        modelId=EMBEDDING_MODEL,
        body=json.dumps({
            "inputText": text,
            "dimensions": EMBEDDING_DIMENSIONS,
            "normalize": True,
        }),
        contentType="application/json",
        accept="application/json",
    )
    return json.loads(resp["body"].read())["embedding"]


# ---------------------------------------------------------------------------
# Capella Management API helpers (used only by provision())
# ---------------------------------------------------------------------------

class _CapellaAPIError(RuntimeError):
    """Preserves the HTTP status + body so provision() can branch on them."""
    def __init__(self, method, path, status, body):
        self.status = status
        self.body = body
        super().__init__(f"{method} {path} -> {status}\n{body[:1500]}")


def _api_call(method: str, path: str, api_token: str, retries: int = 4, **kwargs):
    """Call the Capella Management API; retries on 429 and 5xx responses."""
    import requests
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    delay = 3
    for attempt in range(retries):
        resp = requests.request(
            method, f"{CAPELLA_API_BASE}{path}",
            headers=headers, timeout=90, **kwargs,
        )
        if (resp.status_code == 429 or 500 <= resp.status_code < 600) and attempt < retries - 1:
            time.sleep(delay)
            delay *= 2
            continue
        if not resp.ok:
            raise _CapellaAPIError(method, path, resp.status_code, resp.text)
        return resp.json() if resp.text else {}


def _ssm_require(ssm, key: str, decrypt: bool = False) -> str:
    """Read a required Couchbase SSM parameter or raise with instructions."""
    try:
        return ssm.get_parameter(
            Name=f"{SSM_COUCHBASE_PREFIX}/{key}", WithDecryption=decrypt,
        )["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        raise RuntimeError(
            f"Missing SSM parameter {SSM_COUCHBASE_PREFIX}/{key}. "
            "Open the Partner Selection UI (Data → Vector Store → Couchbase) "
            "and fill in Org ID, Project ID, and API Token first."
        )


def _random_cidr() -> str:
    """Generate a random /23 CIDR for the free-tier cluster deploy."""
    import random
    return f"10.{random.randint(10, 250)}.{random.randint(0, 250) & 0xFE}.0/23"


def _ensure_cluster(project_path: str, api_token: str, name: str, say) -> str:
    """Deploy the free-tier cluster if missing; return its id."""
    for cl in _api_call("GET", f"{project_path}/clusters", api_token).get("data", []):
        if cl.get("name") == name:
            say(f"       Cluster already exists: {name} ({cl['id']})")
            return cl["id"]
    say("       No existing cluster found, deploying a new free-tier cluster...")
    body = {
        "name": name,
        "description": "Pluggable Agentic AI Framework workshop",
        "cloudProvider": {"type": "aws", "region": CAPELLA_REGION, "cidr": None},
    }
    for attempt in range(6):
        body["cloudProvider"]["cidr"] = _random_cidr()
        try:
            resp = _api_call(
                "POST", f"{project_path}/clusters/freeTier",
                api_token, json=body,
            )
            say(f"       Deploy started: {name} ({resp['id']}, cidr={body['cloudProvider']['cidr']})")
            return resp["id"]
        except _CapellaAPIError as e:
            text = (e.body or "").lower()
            if e.status in (409, 422) and "cidr" in text:
                continue  # retry with a new CIDR
            if e.status == 403 and "already exists" in text:
                raise RuntimeError(
                    "This Capella organization already has a free-tier cluster "
                    "with a different name. Delete it in the Capella UI or set "
                    "its name to 'agentic-workshop-free-us-east-2' to reuse it."
                ) from e
            raise
    raise RuntimeError("Could not find an unused CIDR after 6 attempts.")


def _wait_cluster_healthy(cluster_path: str, api_token: str, say) -> dict:
    """Poll cluster status every 15s until 'healthy'. Prints progress."""
    say(f"[4/7] Waiting for cluster to become healthy (3-5 min on first deploy)...")
    TERMINAL_OK  = {"healthy"}
    TERMINAL_BAD = {"deploymentFailed", "destroyFailed", "offline"}
    turned_on = False
    start = time.time()
    last_state = None
    while True:
        info = _api_call("GET", cluster_path, api_token)
        state = info.get("currentState")
        elapsed = int(time.time() - start)
        if state != last_state:
            say(f"       [{elapsed // 60:02d}:{elapsed % 60:02d}] {state}")
            last_state = state
        elif elapsed % 60 < 15:
            say(f"       [{elapsed // 60:02d}:{elapsed % 60:02d}] still {state}...")
        if state in TERMINAL_OK:
            return info
        if state == "turnedOff" and not turned_on:
            _api_call("POST", f"{cluster_path}/activationState", api_token)
            turned_on = True
        elif state in TERMINAL_BAD:
            raise RuntimeError(
                f"Cluster state is '{state}'. Delete it in the Capella UI "
                "and re-run this cell."
            )
        if elapsed > 900:  # 15 min safety cap
            raise RuntimeError(
                f"Cluster not healthy after 15 min (last state={state}). "
                "Check the Capella UI."
            )
        time.sleep(15)


def _ensure_allowlist(cluster_path: str, api_token: str, cluster_name: str, say) -> None:
    """Allowlist this notebook's egress IP and the L3 Refund Lambda's egress IP (/32 each, 72h).

    The Lambda IP is the workshop stack's NAT gateway Elastic IP
    (/anycompany/agentcore/lambda_egress_ip); the Refund tools reach Capella through it.
    """
    import requests
    from datetime import datetime, timedelta as _td, timezone as _tz
    try:
        egress_ip = requests.get("https://checkip.amazonaws.com", timeout=15).text.strip()
    except Exception as e:
        raise RuntimeError(f"Could not detect egress IP: {e}") from e
    targets = [(f"{egress_ip}/32", "SageMaker Studio egress")]
    try:
        lambda_ip = boto3.client("ssm").get_parameter(
            Name=f"{SSM_KB_PREFIX}/lambda_egress_ip")["Parameter"]["Value"]
        targets.append((f"{lambda_ip}/32", "L3 Refund tools Lambda (NAT gateway)"))
    except Exception:
        say("       [WARN] /anycompany/agentcore/lambda_egress_ip not found; the L3 Refund "
            "tools will not reach Capella until the stack's egress IP is allowlisted.")
    now = datetime.now(_tz.utc)
    expires = (now + _td(hours=ALLOWLIST_HOURS)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    try:
        existing_cidrs = set()
        for c in _api_call("GET", f"{cluster_path}/allowedcidrs", api_token).get("data", []):
            exp = c.get("expiresAt")
            if exp and datetime.fromisoformat(exp.replace("Z", "+00:00")) <= now:
                _api_call("DELETE", f"{cluster_path}/allowedcidrs/{c['id']}", api_token)
                say(f"       Removed expired entry: {c['cidr']}")
            else:
                existing_cidrs.add(c["cidr"])
        for target_cidr, comment in targets:
            if target_cidr in existing_cidrs:
                say(f"       Already allowlisted: {target_cidr}  ({comment})")
                continue
            try:
                _api_call(
                    "POST", f"{cluster_path}/allowedcidrs", api_token,
                    json={"cidr": target_cidr, "comment": comment, "expiresAt": expires},
                )
                say(f"       Allowlisted: {target_cidr}  ({comment}, expires {expires})")
            except _CapellaAPIError as e:
                if e.status == 409:
                    say(f"       Already allowlisted: {target_cidr}  ({comment})")
                else:
                    raise
    except _CapellaAPIError as e:
        if e.status in (400, 403, 404, 405, 422):
            cidrs = ", ".join(c for c, _ in targets)
            raise RuntimeError(
                f"Capella Management API refused the allowlist step ({e.status}). "
                f"In the Capella UI, open cluster '{cluster_name}' -> Settings -> "
                f"Allowed IP Addresses -> Add Allowed IP: {cidrs} "
                f"(expiring {ALLOWLIST_HOURS}h). Then re-run this cell."
            ) from e
        raise


def _ensure_db_credential(ssm, cluster_path: str, api_token: str, say) -> str:
    """
    Ensure the DB credential (username + password) exists.
    Returns the password (newly generated or read from SSM).
    """
    import secrets, string

    try:
        current_password = ssm.get_parameter(
            Name=f"{SSM_COUCHBASE_PREFIX}/password", WithDecryption=True,
        )["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        current_password = None

    try:
        existing_users = {
            u["name"]: u["id"]
            for u in _api_call("GET", f"{cluster_path}/users", api_token).get("data", [])
        }
    except _CapellaAPIError as e:
        if e.status in (400, 403, 404, 405, 422):
            raise RuntimeError(
                "Capella Management API refused the credential list. "
                "In the Capella UI, go to the cluster -> Settings -> Cluster Access "
                f"and ensure a credential named '{CB_USERNAME}' with Read/Write access "
                "to all buckets exists, then re-run this cell."
            ) from e
        raise

    if CB_USERNAME in existing_users and current_password:
        say(f"       Credential already exists and SSM has its password: {CB_USERNAME}")
        return current_password

    # Either the credential is missing, or its password isn't in SSM. Recreate.
    if CB_USERNAME in existing_users:
        say(f"       Credential '{CB_USERNAME}' exists but password not in SSM. Recreating.")
        _api_call("DELETE", f"{cluster_path}/users/{existing_users[CB_USERNAME]}", api_token)
        time.sleep(3)

    # Generate a 20-char password mixing upper/lower/digit/safe-special.
    specials = "!@#$"
    alphabet = string.ascii_letters + string.digits + specials
    pw_chars = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice(specials),
    ]
    pw_chars += [secrets.choice(alphabet) for _ in range(16)]
    secrets.SystemRandom().shuffle(pw_chars)
    password = "".join(pw_chars)

    _api_call(
        "POST", f"{cluster_path}/users", api_token,
        json={
            "name": CB_USERNAME, "password": password,
            "access": [{"privileges": ["data_reader", "data_writer"]}],
        },
    )
    say(f"       Created credential: {CB_USERNAME}")
    return password


def _ensure_bucket(cluster_path: str, api_token: str, say) -> str:
    """Create the retail bucket on free tier if missing. Returns its id."""
    try:
        existing = {
            b["name"]: b
            for b in _api_call("GET", f"{cluster_path}/buckets/freeTier", api_token).get("data", [])
        }
        if BUCKET_NAME in existing:
            say(f"       Bucket already exists: {BUCKET_NAME}")
            return existing[BUCKET_NAME]["id"]
        resp = _api_call(
            "POST", f"{cluster_path}/buckets/freeTier", api_token,
            json={"name": BUCKET_NAME, "memoryAllocationInMb": BUCKET_MEMORY_MB},
        )
        say(f"       Created bucket: {BUCKET_NAME} ({BUCKET_MEMORY_MB} MB)")
        time.sleep(5)  # wait for bucket to be ready before adding scopes
        return resp["id"]
    except _CapellaAPIError as e:
        if e.status in (400, 403, 404, 405, 422):
            raise RuntimeError(
                f"Capella Management API refused the bucket step ({e.status}). "
                f"In the Capella UI, go to the cluster -> Data Tools -> Buckets -> "
                f"Create Bucket. Name: {BUCKET_NAME}, memory: {BUCKET_MEMORY_MB} MB. "
                "Then re-run this cell."
            ) from e
        raise


def _ensure_scopes_and_collections(cluster_path: str, api_token: str, bucket_id: str, say) -> None:
    """Create the required scopes + collections if missing."""
    plan = {
        SUPPORT_SCOPE:   [ORDERS_COLLECTION],
        KNOWLEDGE_SCOPE: [DOCS_COLLECTION, CHUNKS_COLLECTION],
    }

    def existing_scopes():
        resp = _api_call("GET", f"{cluster_path}/buckets/{bucket_id}/scopes", api_token)
        return {
            s["name"]: {c["name"] for c in s.get("collections", [])}
            for s in resp.get("scopes", []) if not s["name"].startswith("_")
        }

    try:
        current = existing_scopes()
        for scope_name, collections in plan.items():
            if scope_name not in current:
                _api_call(
                    "POST", f"{cluster_path}/buckets/{bucket_id}/scopes",
                    api_token, json={"name": scope_name},
                )
                say(f"       Created scope: {scope_name}")
                time.sleep(2)
                current[scope_name] = set()
            else:
                say(f"       Scope already exists: {scope_name}")
            for coll in collections:
                if coll in current.get(scope_name, set()):
                    say(f"         Collection already exists: {scope_name}.{coll}")
                    continue
                _api_call(
                    "POST", f"{cluster_path}/buckets/{bucket_id}/scopes/{scope_name}/collections",
                    api_token, json={"name": coll},
                )
                say(f"         Created collection: {scope_name}.{coll}")
                time.sleep(2)
    except _CapellaAPIError as e:
        if e.status in (400, 403, 404, 405, 422):
            raise RuntimeError(
                f"Capella Management API refused the scopes/collections step ({e.status}). "
                f"In the Capella UI, open the bucket '{BUCKET_NAME}' and create: "
                + ", ".join(f"{s}.{c}" for s, cs in plan.items() for c in cs)
                + ". Then re-run this cell."
            ) from e
        raise
