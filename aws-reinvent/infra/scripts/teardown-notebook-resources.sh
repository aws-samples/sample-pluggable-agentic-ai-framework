#!/usr/bin/env bash
# ==============================================================================
# teardown-notebook-resources.sh — delete notebook-created resources that
# teardown.sh skips.
#
# The CFN stack owns the Studio domain, DynamoDB table, Cognito pool, Lambdas,
# Agent Registry, etc. Everything the notebooks create via boto3 API calls
# lives outside the stack and must be deleted separately. This script sweeps
# all of them, in dependency order, and is safe to run before OR after
# ./scripts/teardown.sh.
#
# Scoped by name prefix: anycompany_*, anycompany-*, composable-agents-*,
# CloudWatchOmni*. It will NOT touch anything outside those prefixes.
#
# Idempotent: every delete is wrapped in "if present" discovery, so re-runs
# are harmless.
#
# What this does NOT delete:
#   - The CloudFormation stack itself (run ./scripts/teardown.sh for that)
#   - The assets S3 bucket (passed via teardown.sh --delete-bucket)
#   - Couchbase Capella free-tier cluster (delete via Capella console)
#   - SageMaker Studio notebook state (goes with the Studio domain)
#
# Usage:
#   export AWS_PROFILE=<account-profile>
#   ./scripts/teardown-notebook-resources.sh
# ==============================================================================
set -uo pipefail   # NOTE: no 'e' — individual deletes may fail idempotently

# ---- Config -----------------------------------------------------------------

: "${AWS_PROFILE:?Set AWS_PROFILE before running — e.g. 'export AWS_PROFILE=my-profile'}"
AWS_REGION="${AWS_REGION:-us-east-1}"

log()  { printf "\033[0;36m==>\033[0m %s\n" "$*"; }
ok()   { printf "\033[0;32m✓\033[0m   %s\n" "$*"; }
warn() { printf "\033[0;33m!\033[0m   %s\n" "$*" >&2; }
die()  { printf "\033[0;31m✗\033[0m   %s\n" "$*" >&2; exit 1; }

for cmd in aws python3; do command -v "$cmd" >/dev/null || die "Missing: $cmd"; done

# Several sections need boto3 services (agent-registry-control, cloudwatchomni)
# that aren't in the local AWS CLI yet. Bootstrap a modern boto3 into a temp
# dir once at startup; sections below PYTHONPATH-reference $BOTO3_DIR.
BOTO3_DIR="$(mktemp -d)"
trap 'rm -rf "$BOTO3_DIR"' EXIT
python3 -m pip install --quiet --upgrade "boto3>=1.43.101" -t "$BOTO3_DIR" 2>/dev/null

ACCOUNT_ID="$(aws sts get-caller-identity --profile "$AWS_PROFILE" --query Account --output text)"
cat <<EOF
Orphan cleanup
  Profile : $AWS_PROFILE
  Region  : $AWS_REGION
  Account : $ACCOUNT_ID

Deleting notebook-created resources matching:
  - anycompany_*, anycompany-* (AgentCore runtimes, gateways, memory, harness,
    credential providers, the Brave Search secret)
  - *_agent_a2a runtimes and their ECR repos, CodeBuild projects and log groups
  - composable-agents-* (Bedrock KB + AOSS)
  - CloudWatchOmni* (Omni domain, space, roles)

EOF

# ---- 0. L7 evaluations and optimization ------------------------------------
# 1_agent_evaluations.ipynb creates an online evaluation config that keeps
# scoring Harness traffic until deleted, plus two custom evaluators;
# 2_agent_optimization.ipynb creates a configuration bundle. All are named
# AnyCompany*/anycompany_* by the notebooks. The online config goes first
# because it references the evaluators.
log "AgentCore online evaluation configs, evaluators, configuration bundles"
OEC_IDS="$(aws bedrock-agentcore-control list-online-evaluation-configs \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --query 'onlineEvaluationConfigs[?starts_with(onlineEvaluationConfigName, `AnyCompany`)].onlineEvaluationConfigId' \
  --output text 2>/dev/null || true)"
for c in $OEC_IDS; do
  aws bedrock-agentcore-control delete-online-evaluation-config --online-evaluation-config-id "$c" \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 \
    && ok "deleted online evaluation config $c" \
    || warn "online evaluation config $c delete failed"
done
[[ -z "$OEC_IDS" ]] && ok "no online evaluation configs to delete"
[[ -n "$OEC_IDS" ]] && sleep 10   # let the config release its evaluators
EVAL_IDS="$(aws bedrock-agentcore-control list-evaluators \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --query 'evaluators[?starts_with(evaluatorName, `AnyCompany`)].evaluatorId' \
  --output text 2>/dev/null || true)"
for e in $EVAL_IDS; do
  aws bedrock-agentcore-control delete-evaluator --evaluator-id "$e" \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 \
    && ok "deleted evaluator $e" \
    || warn "evaluator $e delete failed"
done
[[ -z "$EVAL_IDS" ]] && ok "no custom evaluators to delete"
BUNDLE_IDS="$(aws bedrock-agentcore-control list-configuration-bundles \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --query 'bundles[?starts_with(bundleName, `anycompany_`)].bundleId' \
  --output text 2>/dev/null || true)"
for b in $BUNDLE_IDS; do
  aws bedrock-agentcore-control delete-configuration-bundle --bundle-id "$b" \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 \
    && ok "deleted configuration bundle $b" \
    || warn "configuration bundle $b delete failed"
done
[[ -z "$BUNDLE_IDS" ]] && ok "no configuration bundles to delete"
# ---- 1. AgentCore Harness ---------------------------------------------------
# Delete the harness first so its managed runtime is cleaned up automatically.
# The harness list API returns an opaque id; filter by name prefix.

log "AgentCore Harnesses"
HARNESS_IDS="$(aws bedrock-agentcore-control list-harnesses \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --query 'harnesses[?starts_with(harnessName, `anycompany_`)].harnessId' \
  --output text 2>/dev/null || true)"
for h in $HARNESS_IDS; do
  aws bedrock-agentcore-control delete-harness --harness-id "$h" \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 \
    && ok "deleted harness $h" \
    || warn "harness $h delete failed (may already be gone)"
done
[[ -z "$HARNESS_IDS" ]] && ok "no harnesses to delete"

# ---- 2. AgentCore Gateways (targets first, then gateways) -------------------

log "AgentCore Gateway targets + Gateways"
GATEWAY_IDS="$(aws bedrock-agentcore-control list-gateways \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --query 'items[?starts_with(name, `anycompany-`)].gatewayId' \
  --output text 2>/dev/null || true)"
for g in $GATEWAY_IDS; do
  # Each gateway may have multiple targets — delete them one at a time.
  aws bedrock-agentcore-control list-gateway-targets \
    --gateway-identifier "$g" --profile "$AWS_PROFILE" --region "$AWS_REGION" \
    --query 'items[].targetId' --output text 2>/dev/null | tr '\t' '\n' | while read -r t; do
    [[ -z "$t" ]] && continue
    aws bedrock-agentcore-control delete-gateway-target \
      --gateway-identifier "$g" --target-id "$t" \
      --profile "$AWS_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 \
      && ok "deleted target $t on $g" \
      || warn "target $t on $g delete failed"
  done
  aws bedrock-agentcore-control delete-gateway --gateway-identifier "$g" \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 \
    && ok "deleted gateway $g" \
    || warn "gateway $g delete failed"
done
[[ -z "$GATEWAY_IDS" ]] && ok "no gateways to delete"

# ---- 3. AgentCore standalone agent runtimes --------------------------------
# These are the A2A sub-agents (order_agent, refund_agent, research_agent) and
# the Brave Search MCP server runtime (brave_mcp_server, started in 1_gateway_tools).
# Harness-managed runtimes were already removed with the harness in step 1.

log "AgentCore Agent Runtimes"
RUNTIME_IDS="$(aws bedrock-agentcore-control list-agent-runtimes \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --query 'agentRuntimes[?starts_with(agentRuntimeName, `order_agent_`) || starts_with(agentRuntimeName, `refund_agent_`) || starts_with(agentRuntimeName, `research_agent_`) || starts_with(agentRuntimeName, `brave_mcp_server`)].agentRuntimeId' \
  --output text 2>/dev/null || true)"
for r in $RUNTIME_IDS; do
  aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id "$r" \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 \
    && ok "deleted runtime $r" \
    || warn "runtime $r delete failed (may be harness-managed or already gone)"
done
[[ -z "$RUNTIME_IDS" ]] && ok "no standalone runtimes to delete"

# ---- 4. AgentCore Memory ----------------------------------------------------

log "AgentCore Memory"
# NB: list_memories returns `name: None` on memories whose "name" field was not
# set at creation time, but the id is prefixed with the notebook-provided name
# (e.g. "anycompany_agent_memory-FqYMxQE87s"). Filtering on `name` silently
# skips those memories, so filter on `id`.
MEMORY_IDS="$(aws bedrock-agentcore-control list-memories \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --query 'memories[?starts_with(id, `anycompany_`)].id' \
  --output text 2>/dev/null || true)"
for m in $MEMORY_IDS; do
  aws bedrock-agentcore-control delete-memory --memory-id "$m" \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 \
    && ok "deleted memory $m" \
    || warn "memory $m delete failed"
done
[[ -z "$MEMORY_IDS" ]] && ok "no memories to delete"

# ---- 5. AgentCore OAuth2 credential providers (L4) --------------------------

log "AgentCore OAuth2 Credential Providers"
OAUTH_NAMES="$(aws bedrock-agentcore-control list-oauth2-credential-providers \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --query 'credentialProviders[?starts_with(name, `anycompany`)].name' \
  --output text 2>/dev/null || true)"
for p in $OAUTH_NAMES; do
  aws bedrock-agentcore-control delete-oauth2-credential-provider --name "$p" \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 \
    && ok "deleted oauth2 provider $p" \
    || warn "oauth2 provider $p delete failed"
done
[[ -z "$OAUTH_NAMES" ]] && ok "no oauth2 providers to delete"

# ---- 5b. AgentCore API-key credential providers (L3 research: Perplexity) ---

log "AgentCore API-key Credential Providers"
APIKEY_NAMES="$(aws bedrock-agentcore-control list-api-key-credential-providers \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --query 'credentialProviders[?starts_with(name, `anycompany`)].name' \
  --output text 2>/dev/null || true)"
for p in $APIKEY_NAMES; do
  aws bedrock-agentcore-control delete-api-key-credential-provider --name "$p" \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 \
    && ok "deleted api-key provider $p" \
    || warn "api-key provider $p delete failed"
done
[[ -z "$APIKEY_NAMES" ]] && ok "no api-key providers to delete"

# ---- 5c. Secrets Manager (L3 research: Brave Search API key) -----------------
# Force-delete (no recovery window) so a redeploy can recreate the same name.

log "Secrets Manager (Brave Search API key)"
if aws secretsmanager describe-secret --secret-id anycompany/brave-search-api-key \
     --profile "$AWS_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1; then
  aws secretsmanager delete-secret --secret-id anycompany/brave-search-api-key \
    --force-delete-without-recovery \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 \
    && ok "deleted secret anycompany/brave-search-api-key" \
    || warn "secret anycompany/brave-search-api-key delete failed"
else
  ok "no Brave secret to delete"
fi

# ---- 6. Agent Registry records ---------------------------------------------
# The stack owns the AWS::AgentRegistry::Registry resource, but L3 notebooks
# register agent records inside it (via the Registry Discovery Lambda +
# agent-registry-control:CreateRegistryRecord). CFN refuses to delete a
# registry that still has records, so clear them here before teardown.sh
# runs stack-delete.
#
# agent-registry-control isn't in the local AWS CLI yet, so bootstrap boto3
# into a temp dir and call the API from Python (same pattern as the Omni
# section below).

log "Agent Registry records (clear so CFN stack delete can remove the registry)"
PYTHONPATH="$BOTO3_DIR" python3 - "$AWS_PROFILE" "$AWS_REGION" <<'PY'
import sys
import boto3
profile, region = sys.argv[1], sys.argv[2]
s = boto3.Session(profile_name=profile, region_name=region)
try:
    rc = s.client("agent-registry-control")
except Exception as e:
    print(f"  !   agent-registry-control client unavailable: {e}", file=sys.stderr)
    sys.exit(0)
# Find all registries whose name starts with "anycompany-" (the stack-created one).
try:
    registries = rc.list_registries().get("registries", [])
except Exception as e:
    print(f"  !   list_registries failed: {e}", file=sys.stderr)
    sys.exit(0)
targets = [r for r in registries if r.get("name", "").startswith("anycompany-")]
if not targets:
    print("  ✓   no registries to clean")
    sys.exit(0)
for reg in targets:
    reg_id = reg["registryId"]
    try:
        recs = rc.list_registry_records(registryId=reg_id).get("registryRecords", [])
    except Exception as e:
        print(f"  !   list_registry_records({reg_id}) failed: {e}", file=sys.stderr)
        continue
    if not recs:
        print(f"  ✓   registry {reg_id} already empty")
        continue
    for r in recs:
        rec_id = r.get("recordId") or r.get("registryRecordId") or r.get("id")
        try:
            rc.delete_registry_record(registryId=reg_id, recordId=rec_id)
            print(f"  ✓   deleted record {rec_id} ({r.get('name', '')}) in {reg_id}")
        except Exception as e:
            print(f"  !   delete record {rec_id} failed: {e}", file=sys.stderr)
PY

# ---- 7. ECR repositories for agent runtime containers ----------------------

log "ECR repositories (agent runtime builds)"
ECR_REPOS="$(aws ecr describe-repositories \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --query 'repositories[?starts_with(repositoryName, `bedrock-agentcore-order_agent_`) || starts_with(repositoryName, `bedrock-agentcore-refund_agent_`) || starts_with(repositoryName, `bedrock-agentcore-research_agent_`) || starts_with(repositoryName, `bedrock-agentcore-harness_`)].repositoryName' \
  --output text 2>/dev/null || true)"
for r in $ECR_REPOS; do
  aws ecr delete-repository --repository-name "$r" --force \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 \
    && ok "deleted ECR repo $r" \
    || warn "ECR repo $r delete failed"
done
[[ -z "$ECR_REPOS" ]] && ok "no ECR repos to delete"

# ---- 7b. CodeBuild projects for agent runtime builds -----------------------
# The starter toolkit creates one per agent (bedrock-agentcore-<agent>-builder)
# to build its container image. Idle projects cost nothing, but they outlive
# the runtime, so sweep them for a clean account.

log "CodeBuild projects (agent runtime builds)"
CB_PROJECTS="$(aws codebuild list-projects \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --query 'projects[?starts_with(@, `bedrock-agentcore-order_agent_`) || starts_with(@, `bedrock-agentcore-refund_agent_`) || starts_with(@, `bedrock-agentcore-research_agent_`)]' \
  --output text 2>/dev/null || true)"
for p in $CB_PROJECTS; do
  aws codebuild delete-project --name "$p" \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 \
    && ok "deleted CodeBuild project $p" \
    || warn "CodeBuild project $p delete failed"
done
[[ -z "$CB_PROJECTS" ]] && ok "no CodeBuild projects to delete"

# ---- 7c. AgentCore runtime log groups --------------------------------------
# AgentCore writes each runtime's logs to /aws/bedrock-agentcore/runtimes/<name>-<id>-<endpoint>.
# Deleting the runtime leaves the log group behind. Only our runtimes' prefixes
# are swept (agents, Brave MCP server, the anycompany orchestrator harness).

log "AgentCore runtime log groups"
LG_DELETED=0
for prefix in order_agent_ refund_agent_ research_agent_ brave_mcp_server harness_anycompany_; do
  for lg in $(aws logs describe-log-groups \
      --log-group-name-prefix "/aws/bedrock-agentcore/runtimes/$prefix" \
      --profile "$AWS_PROFILE" --region "$AWS_REGION" \
      --query 'logGroups[].logGroupName' --output text 2>/dev/null || true); do
    [[ -z "$lg" || "$lg" == "None" ]] && continue
    aws logs delete-log-group --log-group-name "$lg" \
      --profile "$AWS_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 \
      && { ok "deleted log group $lg"; LG_DELETED=$((LG_DELETED + 1)); } \
      || warn "log group $lg delete failed"
  done
done
[[ "$LG_DELETED" -eq 0 ]] && ok "no runtime log groups to delete"
# ---- 7d. Other notebook log groups ------------------------------------------
# Vended AgentCore logs (runtime, memory, gateway), the starter toolkit's
# CodeBuild build logs, and L7 evaluation results. Only workshop name prefixes
# are swept; shared groups such as /aws/sagemaker/studio and the account's
# default batch-evaluation results group are left alone.
log "AgentCore vended, CodeBuild and evaluation log groups"
LG2_DELETED=0
LG_PREFIXES=""
for kind in runtime/APPLICATION_LOGS runtime/USAGE_LOGS memory/APPLICATION_LOGS gateway/APPLICATION_LOGS; do
  for name in order_agent_a2a refund_agent_a2a research_agent_a2a brave_mcp_server anycompany; do
    LG_PREFIXES="$LG_PREFIXES /aws/vendedlogs/bedrock-agentcore/$kind/$name"
  done
done
for name in order_agent_a2a refund_agent_a2a research_agent_a2a; do
  LG_PREFIXES="$LG_PREFIXES /aws/codebuild/bedrock-agentcore-${name}-builder"
done
LG_PREFIXES="$LG_PREFIXES /aws/bedrock-agentcore/evaluations/results/AnyCompany"
for prefix in $LG_PREFIXES; do
  for lg in $(aws logs describe-log-groups --log-group-name-prefix "$prefix" \
      --profile "$AWS_PROFILE" --region "$AWS_REGION" \
      --query 'logGroups[].logGroupName' --output text 2>/dev/null || true); do
    [[ -z "$lg" || "$lg" == "None" ]] && continue
    aws logs delete-log-group --log-group-name "$lg" \
      --profile "$AWS_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 \
      && { ok "deleted log group $lg"; LG2_DELETED=$((LG2_DELETED + 1)); } \
      || warn "log group $lg delete failed"
  done
done
[[ "$LG2_DELETED" -eq 0 ]] && ok "no vended, CodeBuild or evaluation log groups to delete"
# ---- 7e. Starter toolkit build-source objects --------------------------------
# The toolkit uploads <agent_name>/source.zip to an account-wide bucket it
# creates and may share with other toolkit projects. Delete only this
# workshop's objects, then remove the bucket only if nothing else is in it.
log "Starter toolkit build-source bucket"
for TK_BUCKET in "bedrock-agentcore-codebuild-sources-${ACCOUNT_ID}-${AWS_REGION}" \
                 "bedrock-agentcore-agent-${ACCOUNT_ID}-${AWS_REGION}"; do
  if ! aws s3api head-bucket --bucket "$TK_BUCKET" --profile "$AWS_PROFILE" 2>/dev/null; then
    ok "$TK_BUCKET not present"
    continue
  fi
  for name in order_agent_a2a refund_agent_a2a research_agent_a2a; do
    aws s3 rm "s3://$TK_BUCKET/$name/" --recursive --profile "$AWS_PROFILE" >/dev/null 2>&1 || true
  done
  REMAINING="$(aws s3api list-objects-v2 --bucket "$TK_BUCKET" --max-items 1 \
    --profile "$AWS_PROFILE" --query 'length(Contents || `[]`)' --output text 2>/dev/null || echo 1)"
  if [[ "$REMAINING" == "0" ]]; then
    aws s3api delete-bucket --bucket "$TK_BUCKET" --profile "$AWS_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 \
      && ok "deleted bucket $TK_BUCKET" \
      || warn "bucket $TK_BUCKET delete failed"
  else
    ok "removed workshop objects; kept $TK_BUCKET (other objects present)"
  fi
done

# ---- 8. Bedrock Guardrails --------------------------------------------------

log "Bedrock Guardrails"
GUARDRAIL_IDS="$(aws bedrock list-guardrails \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --query 'guardrails[?starts_with(name, `anycompany`)].id' \
  --output text 2>/dev/null || true)"
for g in $GUARDRAIL_IDS; do
  aws bedrock delete-guardrail --guardrail-identifier "$g" \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 \
    && ok "deleted guardrail $g" \
    || warn "guardrail $g delete failed"
done
[[ -z "$GUARDRAIL_IDS" ]] && ok "no guardrails to delete"

# ---- 9. KB source bucket contents ------------------------------------------
# The stack owns the bucket (KBSourceBucket), but the L1 ingest cell uploads
# the policy docs into it. CFN refuses to delete a non-empty bucket, so
# emptying it here prevents a DELETE_FAILED on the next teardown.sh run.

KB_BUCKET="composable-agents-kb-${ACCOUNT_ID}-${AWS_REGION}"
log "KB source bucket contents ($KB_BUCKET)"
if aws s3api head-bucket --bucket "$KB_BUCKET" --profile "$AWS_PROFILE" 2>/dev/null; then
  aws s3 rm "s3://$KB_BUCKET" --recursive --profile "$AWS_PROFILE" >/dev/null 2>&1 \
    && ok "emptied $KB_BUCKET (CFN deletes the bucket itself)" \
    || warn "could not empty $KB_BUCKET"
else
  ok "$KB_BUCKET not present"
fi

# ---- 10. Bedrock Knowledge Base + AOSS collection ---------------------------
# Order: KB first (it depends on AOSS), then AOSS collection, then AOSS
# policies (network/data-access/encryption), then the KB IAM role + policy.

log "Bedrock Knowledge Base"
KB_IDS="$(aws bedrock-agent list-knowledge-bases \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --query 'knowledgeBaseSummaries[?starts_with(name, `composable-agents-`)].knowledgeBaseId' \
  --output text 2>/dev/null || true)"
for kb in $KB_IDS; do
  # Delete data sources first, then the KB.
  aws bedrock-agent list-data-sources --knowledge-base-id "$kb" \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" \
    --query 'dataSourceSummaries[].dataSourceId' --output text 2>/dev/null | tr '\t' '\n' | while read -r ds; do
    [[ -z "$ds" ]] && continue
    aws bedrock-agent delete-data-source --knowledge-base-id "$kb" --data-source-id "$ds" \
      --profile "$AWS_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 && ok "deleted DS $ds"
  done
  aws bedrock-agent delete-knowledge-base --knowledge-base-id "$kb" \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 \
    && ok "deleted KB $kb" \
    || warn "KB $kb delete failed"
done
[[ -z "$KB_IDS" ]] && ok "no knowledge bases to delete"

log "AOSS collection"
AOSS_IDS="$(aws opensearchserverless list-collections \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --query 'collectionSummaries[?starts_with(name, `composable-agents-`)].id' \
  --output text 2>/dev/null || true)"
for c in $AOSS_IDS; do
  aws opensearchserverless delete-collection --id "$c" \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 \
    && ok "deleted collection $c" \
    || warn "collection $c delete failed"
done
[[ -z "$AOSS_IDS" ]] && ok "no AOSS collections to delete"

log "AOSS policies (network / data-access / encryption)"
for ptype in network data encryption; do
  POLICY_NAMES="$(aws opensearchserverless list-security-policies --type "$ptype" \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" \
    --query 'securityPolicySummaries[?starts_with(name, `composable-agents-`)].name' \
    --output text 2>/dev/null || true)"
  [[ "$ptype" == "data" ]] && POLICY_NAMES="$(aws opensearchserverless list-access-policies --type data \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" \
    --query 'accessPolicySummaries[?starts_with(name, `composable-agent`)].name' \
    --output text 2>/dev/null || true)"
  for p in $POLICY_NAMES; do
    if [[ "$ptype" == "data" ]]; then
      aws opensearchserverless delete-access-policy --name "$p" --type data \
        --profile "$AWS_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 \
        && ok "deleted $ptype policy $p"
    else
      aws opensearchserverless delete-security-policy --name "$p" --type "$ptype" \
        --profile "$AWS_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 \
        && ok "deleted $ptype policy $p"
    fi
  done
done

# ---- 10b. OpenSearch Serverless VPC endpoint (L1) ---------------------------
# The L1 KB helper creates workshop-aoss-vpce to keep the collection private.
# Deleting the collection does not remove it.
log "OpenSearch Serverless VPC endpoint (workshop-aoss-vpce)"
VPCE_IDS="$(aws opensearchserverless list-vpc-endpoints \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --query 'vpcEndpointSummaries[?name==`workshop-aoss-vpce`].id' \
  --output text 2>/dev/null || true)"
for v in $VPCE_IDS; do
  aws opensearchserverless delete-vpc-endpoint --id "$v" \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 \
    && ok "deleting VPC endpoint $v" \
    || warn "VPC endpoint $v delete failed (re-run if it was still being created or deleted)"
done
[[ -z "$VPCE_IDS" ]] && ok "no OpenSearch Serverless VPC endpoint to delete"
# ---- 11. CloudWatch Omni (domain, space, access grants) --------------------
# Local AWS CLI may not have cloudwatchomni yet; use the shared bootstrapped
# boto3 from the top of the script.

log "CloudWatch Omni (via boto3)"
# NB: Omni list_* APIs return {"items": [...]}, NOT {"domains": [...]} /
# {"spaces": [...]} / {"accessGrants": [...]}. Using the wrong key silently
# returns [] and leaves the orphan domain behind — which then blocks the
# next cycle's L6 notebook (create_access_grant needs the caller to have
# grants on the domain, and only the ORIGINAL creator role has them).
PYTHONPATH="$BOTO3_DIR" python3 - "$AWS_PROFILE" "$AWS_REGION" <<'PY'
import sys
import boto3
profile, region = sys.argv[1], sys.argv[2]
s = boto3.Session(profile_name=profile, region_name=region)
omni = s.client("cloudwatchomni")
domains = omni.list_domains().get("items", [])
if not domains:
    print("  ✓   no Omni domains to delete")
    sys.exit(0)
for d in domains:
    if not d.get("name", "").startswith("anycompany"):
        continue
    dom_id = d["domainId"]
    for sp in omni.list_spaces(domainId=dom_id).get("items", []):
        space_id = sp["spaceId"]
        try:
            for g in omni.list_access_grants(domainId=dom_id, spaceId=space_id).get("items", []):
                try:
                    omni.delete_access_grant(grantId=g["grantId"])
                    print(f"  ✓   deleted access grant {g['grantId']}")
                except Exception as e:
                    print(f"  !   grant {g['grantId']} delete failed: {e}", file=sys.stderr)
        except Exception as e:
            print(f"  !   list_access_grants failed on {space_id}: {e}", file=sys.stderr)
        try:
            omni.delete_space(spaceId=space_id)
            print(f"  ✓   deleted space {space_id}")
        except Exception as e:
            print(f"  !   space {space_id} delete failed: {e}", file=sys.stderr)
    try:
        omni.delete_domain(domainId=dom_id)
        print(f"  ✓   deleted domain {dom_id}")
    except Exception as e:
        print(f"  !   domain {dom_id} delete failed: {e}", file=sys.stderr)
PY

# ---- 11b. Notebook-created tool Lambdas (anycompany_order/refund_tools) ----
# L3's 1_gateway_tools.ipynb deploys the order/refund tool Lambdas and L4's
# 1_sensitive_data_masking.ipynb deploys anycompany-pii-interceptor. CFN does not
# own these, so stack teardown leaves them behind along with their /aws/lambda/*
# log groups. Delete them here so a fresh workspace is clean and the CFN-owned
# execution roles (in l3-orchestration.yaml / l4-security.yaml) have nothing
# still referencing them when teardown.sh runs.

log "Notebook-created tool Lambdas (anycompany_order/refund_tools)"
for FN in anycompany_order_tools anycompany_refund_tools anycompany-pii-interceptor agentcore-eval-pii-masking-check; do
  if aws lambda get-function --function-name "$FN" --profile "$AWS_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1; then
    aws lambda delete-function --function-name "$FN" --profile "$AWS_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 \
      && ok "deleted lambda $FN" \
      || warn "lambda $FN delete failed"
    aws logs delete-log-group --log-group-name "/aws/lambda/$FN" --profile "$AWS_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 \
      && ok "deleted log group /aws/lambda/$FN" \
      || true
  else
    ok "lambda $FN not present"
  fi
done

# ---- IAM ownership check (used by sections 12 and 12b) -----------------------
# IAM roles are global, but CloudFormation stacks are regional, and CloudFormation
# does NOT tag IAM roles with aws:cloudformation:* - so a tag check alone always
# reads "not stack-owned". Ask CloudFormation directly, in every supported region.
# Prints the owning stack name (empty if none).
cfn_owner_of_role() {
  local role="$1" reg owner
  for reg in "$AWS_REGION" us-east-1 us-west-2; do
    owner="$(aws cloudformation describe-stack-resources --physical-resource-id "$role" \
      --region "$reg" --profile "$AWS_PROFILE" \
      --query 'StackResources[0].StackName' --output text 2>/dev/null || true)"
    if [[ -n "$owner" && "$owner" != "None" ]]; then
      echo "$owner ($reg)"
      return
    fi
  done
}

# ---- 12. IAM roles (Omni + KB) ---------------------------------------------
# Notebook-created IAM roles that still live outside CloudFormation. The
# AgentCore*/AnyCompany* role names used by 1_gateway_tools.ipynb and the
# agent notebooks are now defined in l3-orchestration.yaml and deleted with
# the parent stack by teardown.sh — we deliberately do NOT delete those here
# because they are in use by the running agent runtimes until the stack goes.
#
# The Omni roles are notebook-owned. composable-agents-kb-role is created by the
# stack's DataStack today (older accounts may have a notebook-created one), so
# every role here is skipped when a CloudFormation stack owns it.

log "IAM roles (Omni + KB)"
for role in \
  CloudWatchOmniOperatorRole \
  CloudWatchOmniDatasetIntegrationRole \
  composable-agents-kb-role; do
  if aws iam get-role --role-name "$role" --profile "$AWS_PROFILE" >/dev/null 2>&1; then
    OWNER="$(cfn_owner_of_role "$role")"
    if [[ -n "$OWNER" ]]; then
      warn "role $role is CloudFormation-owned ($OWNER) — skipping (teardown.sh removes it with the stack)"
      continue
    fi
    # Detach all managed policies on the role
    aws iam list-attached-role-policies --role-name "$role" --profile "$AWS_PROFILE" \
      --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null | tr '\t' '\n' | while read -r pa; do
      [[ -z "$pa" ]] && continue
      aws iam detach-role-policy --role-name "$role" --policy-arn "$pa" --profile "$AWS_PROFILE" >/dev/null 2>&1
    done
    # Delete all inline policies on the role
    aws iam list-role-policies --role-name "$role" --profile "$AWS_PROFILE" \
      --query 'PolicyNames' --output text 2>/dev/null | tr '\t' '\n' | while read -r pn; do
      [[ -z "$pn" ]] && continue
      aws iam delete-role-policy --role-name "$role" --policy-name "$pn" --profile "$AWS_PROFILE" >/dev/null 2>&1
    done
    aws iam delete-role --role-name "$role" --profile "$AWS_PROFILE" >/dev/null 2>&1 \
      && ok "deleted role $role" \
      || warn "role $role delete failed"
  else
    ok "role $role not present"
  fi
done

# Customer-managed KB policy (separate from role)
KB_POLICY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/composable-agents-kb-policy"
if aws iam get-policy --policy-arn "$KB_POLICY_ARN" --profile "$AWS_PROFILE" >/dev/null 2>&1; then
  # Delete non-default versions first
  aws iam list-policy-versions --policy-arn "$KB_POLICY_ARN" --profile "$AWS_PROFILE" \
    --query 'Versions[?!IsDefaultVersion].VersionId' --output text 2>/dev/null | tr '\t' '\n' | while read -r v; do
    [[ -z "$v" ]] && continue
    aws iam delete-policy-version --policy-arn "$KB_POLICY_ARN" --version-id "$v" --profile "$AWS_PROFILE" >/dev/null 2>&1
  done
  aws iam delete-policy --policy-arn "$KB_POLICY_ARN" --profile "$AWS_PROFILE" >/dev/null 2>&1 \
    && ok "deleted policy composable-agents-kb-policy"
else
  ok "policy composable-agents-kb-policy not present"
fi

# ---- 12b. Orphaned L3 agent IAM roles (fixed-name, notebook-created) --------
# These role names are defined with FIXED names in stacks/l3-orchestration.yaml.
# When an account ran the OLD L3 notebooks (pre-CFN) they were created by boto3
# and are owned by NO stack; they then block a fresh CFN deploy with a
# "Resource name conflict". Delete them ONLY when they are orphans — i.e. not
# tagged as owned by a live CloudFormation stack — so the stack's own roles are
# never removed out from under a running stack (that stays teardown.sh's job).

log "Orphaned L3 agent IAM roles (delete only if NOT CloudFormation-owned)"
for role in \
  AnyCompanyOrderToolsLambdaRole \
  AnyCompanyRefundToolsLambdaRole \
  AgentCoreOrderAgentA2ARole \
  AgentCoreRefundAgentA2ARole \
  AnyCompanyAgentCoreGatewayRole \
  AnyCompanyAgentInvokerLambdaRole \
  AnyCompanyAgentCoreHarnessRole; do
  if ! aws iam get-role --role-name "$role" --profile "$AWS_PROFILE" >/dev/null 2>&1; then
    ok "role $role not present"
    continue
  fi
  OWNER="$(cfn_owner_of_role "$role")"
  CFN_TAGGED="$(aws iam list-role-tags --role-name "$role" --profile "$AWS_PROFILE" \
    --query "Tags[?starts_with(Key, 'aws:cloudformation:')] | length(@)" --output text 2>/dev/null || echo 0)"
  if [[ -n "$OWNER" || "$CFN_TAGGED" != "0" ]]; then
    warn "role $role is CloudFormation-owned (${OWNER:-tagged}) — skipping (teardown.sh removes it with the stack)"
    continue
  fi
  for pa in $(aws iam list-attached-role-policies --role-name "$role" --profile "$AWS_PROFILE" \
      --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null); do
    [[ -z "$pa" ]] && continue
    aws iam detach-role-policy --role-name "$role" --policy-arn "$pa" --profile "$AWS_PROFILE" >/dev/null 2>&1
  done
  for pn in $(aws iam list-role-policies --role-name "$role" --profile "$AWS_PROFILE" \
      --query 'PolicyNames' --output text 2>/dev/null | tr '\t' '\n'); do
    [[ -z "$pn" ]] && continue
    aws iam delete-role-policy --role-name "$role" --policy-name "$pn" --profile "$AWS_PROFILE" >/dev/null 2>&1
  done
  aws iam delete-role --role-name "$role" --profile "$AWS_PROFILE" >/dev/null 2>&1 \
    && ok "deleted orphan role $role" \
    || warn "role $role delete failed"
done

# ---- 12c. L7 and starter toolkit IAM roles -----------------------------------
# L7 creates AgentCoreEvaluationRole and agentcore-eval-lambda-role. The starter
# toolkit creates a CodeBuild role per agent named
# AmazonBedrockAgentCoreSDKCodeBuild-<region>-<sha256(agent_name)[:10]>, so the
# names are computed exactly; other toolkit projects' roles are never touched.
log "L7 and starter toolkit IAM roles"
NOTEBOOK_ROLES="AgentCoreEvaluationRole agentcore-eval-lambda-role"
for name in order_agent_a2a refund_agent_a2a research_agent_a2a; do
  # python3 is already required by this script; shasum/sha256sum differ by OS.
  SUFFIX="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:10])' "$name")"
  NOTEBOOK_ROLES="$NOTEBOOK_ROLES AmazonBedrockAgentCoreSDKCodeBuild-${AWS_REGION}-${SUFFIX} AmazonBedrockAgentCoreSDKRuntime-${AWS_REGION}-${SUFFIX}"
done
for role in $NOTEBOOK_ROLES; do
  if ! aws iam get-role --role-name "$role" --profile "$AWS_PROFILE" >/dev/null 2>&1; then
    ok "role $role not present"
    continue
  fi
  OWNER="$(cfn_owner_of_role "$role")"
  if [[ -n "$OWNER" ]]; then
    warn "role $role is CloudFormation-owned ($OWNER) — skipping"
    continue
  fi
  aws iam list-attached-role-policies --role-name "$role" --profile "$AWS_PROFILE" \
    --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null | tr '\t' '\n' | while read -r pa; do
    [[ -z "$pa" ]] && continue
    aws iam detach-role-policy --role-name "$role" --policy-arn "$pa" --profile "$AWS_PROFILE" >/dev/null 2>&1
  done
  aws iam list-role-policies --role-name "$role" --profile "$AWS_PROFILE" \
    --query 'PolicyNames' --output text 2>/dev/null | tr '\t' '\n' | while read -r pn; do
    [[ -z "$pn" ]] && continue
    aws iam delete-role-policy --role-name "$role" --policy-name "$pn" --profile "$AWS_PROFILE" >/dev/null 2>&1
  done
  aws iam delete-role --role-name "$role" --profile "$AWS_PROFILE" >/dev/null 2>&1 \
    && ok "deleted role $role" \
    || warn "role $role delete failed"
done
# ---- 13. SSM parameters ----------------------------------------------------
# Everything under /anycompany/agentcore/* (set by L2 and the L0 UI).

log "SSM parameters under /anycompany/agentcore/"
SSM_NAMES="$(aws ssm describe-parameters \
  --parameter-filters "Key=Name,Option=BeginsWith,Values=/anycompany/agentcore/" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --query 'Parameters[].Name' --output text 2>/dev/null || true)"
if [[ -n "$SSM_NAMES" ]]; then
  # delete-parameters takes up to 10 names per call; batch.
  echo "$SSM_NAMES" | tr '\t' '\n' | xargs -n 10 \
    aws ssm delete-parameters --profile "$AWS_PROFILE" --region "$AWS_REGION" --names >/dev/null 2>&1
  COUNT="$(echo "$SSM_NAMES" | tr '\t' '\n' | wc -l | tr -d ' ')"
  ok "deleted $COUNT SSM parameter(s)"
else
  ok "no SSM parameters to delete"
fi

echo
ok "Orphan cleanup complete"
echo
cat <<EOF
Not handled by this script (manual steps if applicable):
  - Couchbase Capella free-tier cluster: delete via cloud.couchbase.com
  - The CloudFormation stack + the assets S3 bucket:
      run ./scripts/teardown.sh
EOF
