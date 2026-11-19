"""
L1 — Unstructured data · Bedrock Knowledge Base helpers.

Four public functions that abstract OpenSearch Serverless + Bedrock KB plumbing
so the participant notebook stays short and provider-neutral:

    create_vector_store(name) -> {arn, endpoint, id, index_name, vpce_id}
    create_knowledge_base(name, collection_arn, s3_bucket, s3_prefix,
                          role_arn, embedding_model) -> {kb_id, data_source_id}
    ingest(kb_id, data_source_id, wait=True) -> {scanned, indexed, failed, status}
    retrieve(kb_id, query, k=5) -> [{score, text, source}, ...]

Everything else — VPC discovery, VPC endpoint, AOSS security policies, vector
index creation with eventual-consistency waits, KB cleanup-if-exists, ingestion
polling, retry logic for IAM/role propagation — is private to this module.
"""

from __future__ import annotations

import json
import time
from typing import Any

import boto3

# ---------------------------------------------------------------------------
# Module-level clients. Resolved lazily so the module is cheap to import.
# ---------------------------------------------------------------------------

_REGION: str | None = None
_ACCOUNT_ID: str | None = None
_CALLER_ARN: str | None = None


def _region() -> str:
    global _REGION
    if _REGION is None:
        import os
        _REGION = os.environ.get("AWS_DEFAULT_REGION") or boto3.session.Session().region_name or "us-east-1"
    return _REGION


def _identity() -> tuple[str, str]:
    """Return (account_id, caller_arn) cached per-process."""
    global _ACCOUNT_ID, _CALLER_ARN
    if _ACCOUNT_ID is None:
        resp = boto3.client("sts", region_name=_region()).get_caller_identity()
        _ACCOUNT_ID = resp["Account"]
        _CALLER_ARN = resp["Arn"]
    return _ACCOUNT_ID, _CALLER_ARN


# ---------------------------------------------------------------------------
# VPC discovery + VPC endpoint for OpenSearch Serverless
#
# EC2 Describe/Create/Delete VPC-endpoint permissions are granted to the
# SageMaker exec role via the WorkshopExecutionPolicyModels managed policy in
# aws-reinvent/infra/stacks/platform-studio.yaml. If you see an AccessDenied on
# ec2:DescribeVpcEndpoints or similar here, the stack needs an update.
# ---------------------------------------------------------------------------

def _discover_vpc() -> tuple[str, list[str], list[str]]:
    """Return (vpc_id, subnet_ids, security_group_ids) using the default VPC."""
    ec2 = boto3.client("ec2", region_name=_region())
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])["Vpcs"]
    if not vpcs:
        vpcs = ec2.describe_vpcs()["Vpcs"]
    vpc_id = vpcs[0]["VpcId"]

    # Pick 2 subnets in different AZs
    all_subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
    seen_azs: set[str] = set()
    subnet_ids: list[str] = []
    for sn in all_subnets:
        if sn["AvailabilityZone"] not in seen_azs:
            subnet_ids.append(sn["SubnetId"])
            seen_azs.add(sn["AvailabilityZone"])
        if len(subnet_ids) >= 2:
            break

    sgs = ec2.describe_security_groups(Filters=[
        {"Name": "vpc-id", "Values": [vpc_id]},
        {"Name": "group-name", "Values": ["default"]},
    ])["SecurityGroups"]
    sg_ids = [sgs[0]["GroupId"]]

    return vpc_id, subnet_ids, sg_ids


def _ensure_vpce(aoss, vpce_name: str, vpc_id: str, subnet_ids: list[str], sg_ids: list[str]) -> str:
    """Create (or look up) the AOSS VPC endpoint. Returns its id."""
    try:
        resp = aoss.create_vpc_endpoint(
            name=vpce_name, vpcId=vpc_id,
            subnetIds=subnet_ids, securityGroupIds=sg_ids,
        )
        vpce_id = resp["createVpcEndpointDetail"]["id"]
        print(f"  Created VPC endpoint: {vpce_id}")
        return vpce_id
    except aoss.exceptions.ConflictException:
        for vpce in aoss.list_vpc_endpoints().get("vpcEndpointSummaries", []):
            if vpce["name"] == vpce_name:
                print(f"  VPC endpoint already exists: {vpce['id']}")
                return vpce["id"]
        raise RuntimeError(f"VPC endpoint '{vpce_name}' exists but could not be found in listing")


# ---------------------------------------------------------------------------
# AOSS security policies (encryption, network, data access)
# ---------------------------------------------------------------------------

def _ensure_encryption_policy(aoss, name: str, collection_name: str) -> None:
    body = json.dumps({
        "Rules": [{"ResourceType": "collection",
                   "Resource": [f"collection/{collection_name}"]}],
        "AWSOwnedKey": True,
    })
    try:
        aoss.create_security_policy(name=name, type="encryption", policy=body)
        print("  Created encryption policy")
    except aoss.exceptions.ConflictException:
        print("  Encryption policy already exists")


def _ensure_network_policy_public(aoss, name: str, collection_name: str) -> None:
    """Create or update the network policy to allow public access.

    Public access is needed up front so the notebook (running from SageMaker
    Studio) can create the vector index via the OpenSearch REST API. After
    the index is created we lock this down via _harden_network_policy().
    """
    body = [{
        "Rules": [
            {"ResourceType": "collection", "Resource": [f"collection/{collection_name}"]},
            {"ResourceType": "dashboard",  "Resource": [f"collection/{collection_name}"]},
        ],
        "AllowFromPublic": True,
    }]
    try:
        aoss.create_security_policy(name=name, type="network", policy=json.dumps(body))
        print("  Created network policy (public access for index creation)")
    except aoss.exceptions.ConflictException:
        existing = aoss.get_security_policy(name=name, type="network")
        try:
            aoss.update_security_policy(
                name=name, type="network",
                policyVersion=existing["securityPolicyDetail"]["policyVersion"],
                policy=json.dumps(body),
            )
            print("  Updated network policy to allow public access")
        except aoss.exceptions.ValidationException as e:
            if "no changes detected" in str(e).lower():
                print("  Network policy already open for public access")
            else:
                raise


def _harden_network_policy(aoss, name: str, collection_name: str, vpce_id: str) -> None:
    """Lock the network policy to the VPC endpoint + Bedrock service only."""
    body = [{
        "Rules": [
            {"ResourceType": "collection", "Resource": [f"collection/{collection_name}"]},
            {"ResourceType": "dashboard",  "Resource": [f"collection/{collection_name}"]},
        ],
        "AllowFromPublic": False,
        "SourceVPCEs": [vpce_id],
        "SourceServices": ["bedrock.amazonaws.com"],
    }]
    existing = aoss.get_security_policy(name=name, type="network")
    aoss.update_security_policy(
        name=name, type="network",
        policyVersion=existing["securityPolicyDetail"]["policyVersion"],
        policy=json.dumps(body),
    )
    print(f"  Network policy hardened: VPC endpoint {vpce_id} + Bedrock service only")


def _ensure_data_access_policy(
    aoss, name: str, collection_name: str, principals: list[str]
) -> None:
    body = json.dumps([{
        "Rules": [
            {
                "ResourceType": "index",
                "Resource": [f"index/{collection_name}/*"],
                "Permission": [
                    "aoss:CreateIndex", "aoss:DeleteIndex", "aoss:UpdateIndex",
                    "aoss:DescribeIndex", "aoss:ReadDocument", "aoss:WriteDocument",
                ],
            },
            {
                "ResourceType": "collection",
                "Resource": [f"collection/{collection_name}"],
                "Permission": [
                    "aoss:CreateCollectionItems", "aoss:DeleteCollectionItems",
                    "aoss:DescribeCollectionItems", "aoss:UpdateCollectionItems",
                ],
            },
        ],
        "Principal": principals,
    }])
    try:
        aoss.create_access_policy(name=name, type="data", policy=body)
        print("  Created data access policy")
    except aoss.exceptions.ConflictException:
        try:
            existing = aoss.get_access_policy(name=name, type="data")
            aoss.update_access_policy(
                name=name, type="data",
                policyVersion=existing["accessPolicyDetail"]["policyVersion"],
                policy=body,
            )
            print("  Updated data access policy")
        except aoss.exceptions.ValidationException:
            print("  Data access policy already up to date")


# ---------------------------------------------------------------------------
# AOSS collection + vector index
# ---------------------------------------------------------------------------

def _wait_collection_active(aoss, collection_name: str, timeout: int = 600) -> dict:
    """Poll until collection is ACTIVE. Returns the collection detail dict."""
    print("  Waiting for collection to become ACTIVE (typically 5-6 minutes)...")
    start = time.time()
    poll = 0
    while True:
        resp = aoss.batch_get_collection(names=[collection_name])
        detail = resp["collectionDetails"][0]
        status = detail["status"]
        elapsed = time.time() - start
        poll += 1
        if status == "ACTIVE":
            print(f"  Collection ACTIVE after {elapsed:.0f}s ({poll} poll(s))")
            return detail
        if elapsed > timeout:
            raise RuntimeError(f"Collection {collection_name} did not become ACTIVE in {timeout}s (last status={status})")
        print(f"  [{elapsed:5.0f}s] Status: {status}... (poll #{poll})")
        time.sleep(15)


def _create_vector_index(endpoint: str, index_name: str, dimension: int = 1024) -> None:
    """Create the kNN index via the OpenSearch REST API with SigV4."""
    from opensearchpy import OpenSearch, RequestsHttpConnection
    from requests_aws4auth import AWS4Auth

    creds = boto3.Session().get_credentials().get_frozen_credentials()
    awsauth = AWS4Auth(creds.access_key, creds.secret_key, _region(), "aoss",
                       session_token=creds.token)
    host = endpoint.replace("https://", "")
    client = OpenSearch(
        hosts=[{"host": host, "port": 443}],
        http_auth=awsauth, use_ssl=True, verify_certs=True,
        connection_class=RequestsHttpConnection,
    )

    index_body = {
        "settings": {"index": {"knn": True}},
        "mappings": {
            "properties": {
                "bedrock-knowledge-base-default-vector": {
                    "type": "knn_vector", "dimension": dimension,
                    "method": {"engine": "faiss", "name": "hnsw", "space_type": "l2"},
                },
                "AMAZON_BEDROCK_TEXT": {"type": "text"},
                "AMAZON_BEDROCK_METADATA": {"type": "text"},
            }
        },
    }

    # Retry — data access policy takes ~15-45s to propagate
    for attempt in range(5):
        try:
            client.indices.create(index=index_name, body=index_body)
            print(f"  Created vector index: {index_name} (dimension={dimension})")
            break
        except Exception as e:
            msg = str(e).lower()
            if "already_exists" in msg or "resource_already_exists" in msg:
                print(f"  Vector index already exists: {index_name}")
                break
            if "authorization_exception" in msg and attempt < 4:
                wait = 15 * (attempt + 1)
                print(f"  Access policy still propagating, retrying in {wait}s... (attempt {attempt + 1}/5)")
                time.sleep(wait)
            else:
                raise

    # Eventual consistency — index can take 30-60s after create before it's
    # queryable. Verify with a real search call.
    print("  Verifying index is queryable (OpenSearch Serverless eventual consistency)...")
    start = time.time()
    timeout = 180
    verified = False
    while time.time() - start < timeout:
        try:
            if client.indices.exists(index=index_name):
                client.search(index=index_name, body={"query": {"match_all": {}}, "size": 0})
                verified = True
                break
        except Exception as e:
            msg = str(e).lower()
            if "not_found" not in msg and "index_not_found" not in msg and "no such index" not in msg:
                print(f"  Unexpected error while verifying index: {e}")
        elapsed = int(time.time() - start)
        print(f"  [{elapsed:3d}s] Index not yet queryable, waiting...")
        time.sleep(10)

    if not verified:
        raise RuntimeError(
            f"Vector index '{index_name}' did not become queryable within {timeout}s"
        )

    # Extra cushion before Bedrock reads the index (control plane propagation)
    time.sleep(30)
    print(f"  Index queryable after {int(time.time() - start)}s — safe to create KB")


# ---------------------------------------------------------------------------
# Public API — create_vector_store
# ---------------------------------------------------------------------------

def create_vector_store(name: str = "composable-agents-collection") -> dict[str, Any]:
    """Create an OpenSearch Serverless collection with everything a Bedrock KB needs.

    Does all of this, idempotently:
      - Discovers default VPC + 2 subnets + default security group
      - Creates (or reuses) a VPC endpoint for AOSS
      - Creates (or reuses) encryption, network (public), and data-access policies
      - Creates (or reuses) the VECTORSEARCH collection; waits for ACTIVE
      - Creates (or reuses) the kNN FAISS/HNSW index with 1024-dim vectors
      - Hardens the network policy to VPC endpoint + Bedrock-service only

    Takes ~6 minutes on first run (collection ACTIVE wait dominates); seconds
    on subsequent runs. Returns a dict with the collection details and the
    index name that create_knowledge_base() needs.
    """
    print(f"Creating/reusing OpenSearch Serverless collection: {name}")
    aoss = boto3.client("opensearchserverless", region_name=_region())
    account_id, caller_arn = _identity()

    # 1. VPC + VPC endpoint (reused by notebooks on same account).
    #    EC2 perms for VPCE creation come from WorkshopExecutionPolicyModels (CFN).
    vpc_id, subnet_ids, sg_ids = _discover_vpc()
    print(f"  VPC: {vpc_id}  Subnets: {subnet_ids}  SG: {sg_ids}")
    vpce_id = _ensure_vpce(aoss, "workshop-aoss-vpce", vpc_id, subnet_ids, sg_ids)

    # 2. Encryption + network (public, temporarily) policies
    _ensure_encryption_policy(aoss, f"{name}-enc", name)
    _ensure_network_policy_public(aoss, f"{name}-net", name)

    # 3. Collection
    try:
        aoss.create_collection(name=name, type="VECTORSEARCH")
        print(f"  Creating collection: {name}")
    except aoss.exceptions.ConflictException:
        print(f"  Collection already exists: {name}")

    # 4. Data access policy — needs KB role (resolved from SSM-published param)
    #    plus the caller identity (SageMaker Studio) so this notebook can
    #    create the vector index.
    kb_role_arn = f"arn:aws:iam::{account_id}:role/composable-agents-kb-role"
    workshop_studio_arn = f"arn:aws:sts::{account_id}:assumed-role/WSParticipantRole/Participant"
    principals = [kb_role_arn, caller_arn, workshop_studio_arn]
    # Also add the IAM role behind the Studio session. caller_arn has the form
    # arn:aws:sts::<account>:assumed-role/<role-name>/<session-name>.
    if ":assumed-role/" in caller_arn:
        role_name = caller_arn.split(":assumed-role/", 1)[1].split("/", 1)[0]
        principals.append(f"arn:aws:iam::{account_id}:role/{role_name}")
    _ensure_data_access_policy(
        aoss, f"{name[:16]}-access", name,
        principals=principals,
    )

    # 5. Wait for collection ACTIVE
    detail = _wait_collection_active(aoss, name)
    collection_arn = detail["arn"]
    collection_endpoint = detail["collectionEndpoint"]
    collection_id = detail["id"]

    # 6. Create the vector index (requires public network access + data access policy)
    index_name = "bedrock-knowledge-base-default-index"
    _create_vector_index(collection_endpoint, index_name)

    # 7. Harden the network policy — public access no longer needed
    _harden_network_policy(aoss, f"{name}-net", name, vpce_id)

    return {
        "name": name,
        "arn": collection_arn,
        "endpoint": collection_endpoint,
        "id": collection_id,
        "index_name": index_name,
        "vpce_id": vpce_id,
    }


# ---------------------------------------------------------------------------
# Public API — create_knowledge_base
# ---------------------------------------------------------------------------

def _delete_kb_if_exists(bedrock_agent, kb_name: str, timeout: int = 600) -> None:
    """Delete any existing KB with the given name and block until it is gone."""
    old_kb_id: str | None = None
    try:
        for page in bedrock_agent.get_paginator("list_knowledge_bases").paginate():
            for kb in page["knowledgeBaseSummaries"]:
                if kb["name"] == kb_name:
                    old_kb_id = kb["knowledgeBaseId"]
                    break
            if old_kb_id:
                break
    except Exception as e:
        print(f"  Could not list knowledge bases: {e}")
        return

    if not old_kb_id:
        return

    print(f"  Existing KB '{kb_name}' found ({old_kb_id}) — deleting first")
    # Delete data sources first (children before parent)
    try:
        for ds in bedrock_agent.list_data_sources(knowledgeBaseId=old_kb_id)["dataSourceSummaries"]:
            bedrock_agent.delete_data_source(
                knowledgeBaseId=old_kb_id, dataSourceId=ds["dataSourceId"],
            )
            print(f"    Deleted old data source: {ds['name']}")
    except Exception as e:
        print(f"    Data source cleanup note: {e}")

    try:
        bedrock_agent.delete_knowledge_base(knowledgeBaseId=old_kb_id)
    except bedrock_agent.exceptions.ResourceNotFoundException:
        return

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status = bedrock_agent.get_knowledge_base(knowledgeBaseId=old_kb_id)["knowledgeBase"]["status"]
            print(f"    Old KB status: {status}...")
        except bedrock_agent.exceptions.ResourceNotFoundException:
            print(f"    Old KB {old_kb_id} fully deleted")
            return
        time.sleep(10)
    raise RuntimeError(f"Timed out waiting for old KB '{old_kb_id}' to delete")


def create_knowledge_base(
    name: str,
    collection_arn: str,
    s3_bucket: str,
    s3_prefix: str,
    role_arn: str,
    embedding_model: str = "amazon.titan-embed-text-v2:0",
) -> dict[str, Any]:
    """Create a Bedrock KB + Data Source pointing at the given S3 prefix.

    If a KB with the same name already exists, it (and its data sources) is
    deleted first. Returns {kb_id, data_source_id}.
    """
    print(f"Creating Bedrock Knowledge Base: {name}")
    bedrock_agent = boto3.client("bedrock-agent", region_name=_region())

    _delete_kb_if_exists(bedrock_agent, name)

    embedding_arn = f"arn:aws:bedrock:{_region()}::foundation-model/{embedding_model}"
    kb_config = {
        "type": "VECTOR",
        "vectorKnowledgeBaseConfiguration": {
            "embeddingModelArn": embedding_arn,
            "embeddingModelConfiguration": {
                "bedrockEmbeddingModelConfiguration": {"embeddingDataType": "FLOAT32"},
            },
        },
    }
    storage_config = {
        "type": "OPENSEARCH_SERVERLESS",
        "opensearchServerlessConfiguration": {
            "collectionArn": collection_arn,
            "vectorIndexName": "bedrock-knowledge-base-default-index",
            "fieldMapping": {
                "vectorField": "bedrock-knowledge-base-default-vector",
                "textField": "AMAZON_BEDROCK_TEXT",
                "metadataField": "AMAZON_BEDROCK_METADATA",
            },
        },
    }

    # Retry — IAM/trust policy + index visibility to Bedrock can lag up to ~60s
    kb_id: str | None = None
    for attempt in range(5):
        try:
            resp = bedrock_agent.create_knowledge_base(
                name=name, roleArn=role_arn,
                knowledgeBaseConfiguration=kb_config,
                storageConfiguration=storage_config,
            )
            kb_id = resp["knowledgeBase"]["knowledgeBaseId"]
            print(f"  Created Knowledge Base: {kb_id}")
            break
        except bedrock_agent.exceptions.ValidationException as e:
            msg = str(e).lower()
            retryable = any(s in msg for s in (
                "unable to assume", "assume the given role", "not authorized",
                "does not exist", "no such index", "index_not_found",
            ))
            if retryable and attempt < 4:
                wait = 20 * (attempt + 1)
                print(f"  IAM/index visibility still propagating, retrying in {wait}s... (attempt {attempt + 1}/5)")
                time.sleep(wait)
            else:
                raise

    if kb_id is None:
        raise RuntimeError("Knowledge Base creation failed after retries")

    ds_resp = bedrock_agent.create_data_source(
        knowledgeBaseId=kb_id,
        name="composable-agents-data",
        dataDeletionPolicy="RETAIN",
        dataSourceConfiguration={
            "type": "S3",
            "s3Configuration": {
                "bucketArn": f"arn:aws:s3:::{s3_bucket}",
                "inclusionPrefixes": [s3_prefix],
            },
        },
        # Small fixed-size chunks so each policy section (return windows,
        # refund percentages, conditions) is retrieved on its own. The
        # default (~300 tokens) splits the ~2 KB policy file into only 2 chunks.
        vectorIngestionConfiguration={
            "chunkingConfiguration": {
                "chunkingStrategy": "FIXED_SIZE",
                "fixedSizeChunkingConfiguration": {
                    "maxTokens": 80,
                    "overlapPercentage": 20,
                },
            },
        },
    )
    data_source_id = ds_resp["dataSource"]["dataSourceId"]
    print(f"  Created data source: {data_source_id}")

    return {"kb_id": kb_id, "data_source_id": data_source_id}


# ---------------------------------------------------------------------------
# Public API — ingest
# ---------------------------------------------------------------------------

def _wait_kb_ready(bedrock_agent, kb_id: str, timeout: int = 300) -> None:
    elapsed = 0
    while elapsed < timeout:
        status = bedrock_agent.get_knowledge_base(knowledgeBaseId=kb_id)["knowledgeBase"]["status"]
        if status == "ACTIVE":
            return
        if status in ("FAILED", "DELETE_IN_PROGRESS"):
            raise RuntimeError(f"KB {kb_id} is in {status} state")
        print(f"  KB status: {status}, waiting...")
        time.sleep(15)
        elapsed += 15
    raise RuntimeError(f"Timed out after {timeout}s waiting for KB {kb_id} to become ACTIVE")


def _wait_ds_ready(bedrock_agent, kb_id: str, ds_id: str, timeout: int = 300) -> None:
    elapsed = 0
    while elapsed < timeout:
        status = bedrock_agent.get_data_source(
            knowledgeBaseId=kb_id, dataSourceId=ds_id,
        )["dataSource"]["status"]
        if status == "AVAILABLE":
            return
        if status in ("FAILED", "DELETE_IN_PROGRESS"):
            raise RuntimeError(f"Data source {ds_id} is in {status} state")
        print(f"  Data source status: {status}, waiting...")
        time.sleep(15)
        elapsed += 15
    raise RuntimeError(f"Timed out after {timeout}s waiting for data source {ds_id}")


def ingest(kb_id: str, data_source_id: str, wait: bool = True) -> dict[str, Any]:
    """Start a Bedrock KB ingestion job. Blocks until completion if wait=True.

    Bedrock does the chunking + embedding + indexing internally. Returns
    {scanned, indexed, failed, status} on completion.
    """
    print(f"Ingesting data source {data_source_id} into KB {kb_id}")
    bedrock_agent = boto3.client("bedrock-agent", region_name=_region())

    _wait_kb_ready(bedrock_agent, kb_id)
    _wait_ds_ready(bedrock_agent, kb_id, data_source_id)

    resp = bedrock_agent.start_ingestion_job(
        knowledgeBaseId=kb_id, dataSourceId=data_source_id,
    )
    job_id = resp["ingestionJob"]["ingestionJobId"]
    print(f"  Ingestion job started: {job_id}")

    if not wait:
        return {"status": "IN_PROGRESS", "job_id": job_id,
                "scanned": None, "indexed": None, "failed": None}

    start = time.time()
    poll = 0
    while True:
        job = bedrock_agent.get_ingestion_job(
            knowledgeBaseId=kb_id, dataSourceId=data_source_id,
            ingestionJobId=job_id,
        )
        status = job["ingestionJob"]["status"]
        elapsed = time.time() - start
        poll += 1
        if status in ("COMPLETE", "FAILED", "STOPPED"):
            break
        print(f"  [{elapsed:5.0f}s] Status: {status}... (poll #{poll})")
        time.sleep(15)

    stats = job["ingestionJob"].get("statistics", {})
    indexed = stats.get("numberOfNewDocumentsIndexed", 0) + stats.get("numberOfModifiedDocumentsIndexed", 0)
    out = {
        "status": status,
        "scanned": stats.get("numberOfDocumentsScanned", 0),
        "indexed": indexed,
        "failed": stats.get("numberOfDocumentsFailed", 0),
    }
    print(f"  Completed in {time.time() - start:.0f}s "
          f"— scanned={out['scanned']} indexed={out['indexed']} failed={out['failed']}")
    return out


# ---------------------------------------------------------------------------
# Public API — retrieve
# ---------------------------------------------------------------------------

def retrieve(kb_id: str, query: str, k: int = 5) -> list[dict[str, Any]]:
    """Run a semantic retrieval against the KB and return the top k passages.

    Each result is a dict with: {score, text, source}.
    """
    runtime = boto3.client("bedrock-agent-runtime", region_name=_region())
    resp = runtime.retrieve(
        knowledgeBaseId=kb_id,
        retrievalQuery={"text": query},
        retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": k}},
    )
    out: list[dict[str, Any]] = []
    for r in resp.get("retrievalResults", []):
        content = r.get("content", {})
        text = content.get("text", "")
        source = r.get("location", {}).get("s3Location", {}).get("uri", "unknown")
        out.append({"score": r.get("score", 0.0), "text": text, "source": source})
    return out
