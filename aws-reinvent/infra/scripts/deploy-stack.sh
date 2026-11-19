#!/usr/bin/env bash
# ==============================================================================
# deploy-stack.sh — one-shot deploy for the aws-reinvent workshop CFN stack.
#
# Idempotent: first run creates the stack and all prerequisite assets, every
# subsequent run re-uploads assets and submits an update. Safe to re-run after
# editing notebooks, the template, or sample data.
#
# Pipeline:
#   1. Resolve config (profile, region, derived assets bucket, repo paths)
#   2. Create the assets S3 bucket if missing (SSE-S3 + public-access-block)
#   3. Rebuild and upload workshop.zip, plus infra/assets/sample-data
#   4. Upload agent-stack.yaml (parent) and stacks/*.yaml (one nested stack per
#      layer) to S3 — nested stacks are always loaded from S3
#   5. Validate the parent and every nested template
#   6. create-stack on first run, update-stack on subsequent runs
#   7. Verify: stack outputs, DynamoDB counts, SSM parameters, Studio URL
#
# Prereqs before running this script:
#   - Amazon Bedrock enables serverless models (Claude Sonnet 4.6, Nova, Titan
#     Embed) on first use in us-east-1. If the account has not used Anthropic
#     Claude before, submit the one-time Anthropic use-case form from the
#     Bedrock model catalog first.
#   - AWS CLI and zip on PATH.
#
# workshop.zip handling: this script always rebuilds it by invoking
# scripts/build-workshop-zip.sh, which replaces any existing
# infra/assets/workshop.zip (including a pre-built one dropped by Workshop
# Studio). This guarantees the zip reflects the current workshop/ source
# rather than a stale cached copy.
#
# Usage:
#   export AWS_PROFILE=<target-account-profile>
#   ./scripts/deploy-stack.sh
#
# Env var overrides:
#   AWS_REGION      (default: us-east-1)
#   STACK_NAME      (default: agent-stack)
#   ASSETS_BUCKET   (default: aapcf-reinvent-assets-<account-id>)
#   ASSETS_PREFIX   (default: latest)
# ==============================================================================
set -euo pipefail

# ---- 1. Config --------------------------------------------------------------
# AWS_PROFILE is required and must be set by the caller — refusing to default
# prevents accidental deploys to the wrong account.

: "${AWS_PROFILE:?Set AWS_PROFILE before running — e.g. 'export AWS_PROFILE=my-profile'}"
AWS_REGION="${AWS_REGION:-us-east-1}"
STACK_NAME="${STACK_NAME:-agent-stack}"
ASSETS_PREFIX="${ASSETS_PREFIX:-latest}"
# S3 key prefix without a trailing slash; the stack parameter gets one appended
# (the parent template builds child URLs as ${AssetsBucketPrefix}stacks/...,
# matching Workshop Studio, whose injected prefix ends in '/').
ASSETS_PREFIX="${ASSETS_PREFIX%/}"

# Repo-relative paths, resolved from this script's own location so the script
# works regardless of where the repo is cloned. All deployment inputs live in
# aws-reinvent/infra/. This script itself reads only from infra/, and invokes
# build-workshop-zip.sh (which reads the sibling workshop/ folder) to produce
# workshop.zip.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATE_FILE="$CFN_DIR/agent-stack.yaml"
STACKS_DIR="$CFN_DIR/stacks"
ASSETS_DIR="$CFN_DIR/assets"
WORKSHOP_ZIP="$ASSETS_DIR/workshop.zip"
SAMPLE_DATA_SRC="$ASSETS_DIR/sample-data"

# Logging helpers — colored only when stdout is a TTY-ish terminal; CI logs
# will still show the raw sequences but they don't break anything.
log()  { printf "\033[0;36m==>\033[0m %s\n" "$*"; }
ok()   { printf "\033[0;32m✓\033[0m   %s\n" "$*"; }
warn() { printf "\033[0;33m!\033[0m   %s\n" "$*" >&2; }
die()  { printf "\033[0;31m✗\033[0m   %s\n" "$*" >&2; exit 1; }

# Fail fast if required CLIs are missing, so we don't fail halfway through.
for cmd in aws zip; do
  command -v "$cmd" >/dev/null || die "Missing required command: $cmd"
done

# Derive the caller's account + ARN from STS so the script works for any
# profile without hard-coding account IDs.
ACCOUNT_ID="$(aws sts get-caller-identity --profile "$AWS_PROFILE" --query Account --output text)"
CALLER_ARN="$(aws sts get-caller-identity --profile "$AWS_PROFILE" --query Arn --output text)"

# Bucket name is globally unique; account-id suffix makes it collision-free
# across accounts without requiring the caller to pick a name.
ASSETS_BUCKET="${ASSETS_BUCKET:-aapcf-reinvent-assets-${ACCOUNT_ID}}"

cat <<EOF
Configuration
  Profile  : $AWS_PROFILE
  Region   : $AWS_REGION
  Account  : $ACCOUNT_ID
  Caller   : $CALLER_ARN
  Stack    : $STACK_NAME
  Bucket   : $ASSETS_BUCKET
  Prefix   : $ASSETS_PREFIX
  Template : $TEMPLATE_FILE
  Assets   : $ASSETS_DIR

EOF

# Always rebuild workshop.zip. The build is cheap (a few seconds) and the
# zip must reflect the current state of aws-reinvent/workshop/ -- a stale
# cached zip has silently shipped old notebook/UI code before. This always
# replaces any pre-built workshop.zip in infra/assets/. If Workshop Studio
# automation supplies its own pre-built zip, skip this script and run the
# stack ops directly.
log "Rebuilding workshop.zip from current workshop content"
"$SCRIPT_DIR/build-workshop-zip.sh"

# Check current stack status up front. ROLLBACK_COMPLETE is a terminal failure
# state — CFN can't update out of it, so we bail with a pointer to teardown.
EXISTING_STATUS="$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region "$AWS_REGION" --profile "$AWS_PROFILE" \
  --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo NONE)"

if [[ "$EXISTING_STATUS" == "ROLLBACK_COMPLETE" ]]; then
  die "Stack '$STACK_NAME' is in ROLLBACK_COMPLETE. Run ./scripts/teardown.sh first, then rerun deploy."
fi

# The template is now a parent of per-layer nested stacks. A stack created from the
# old single-file template can't be updated into it: the nested stacks would create
# resources with the same fixed names (CustomerOrders, *-SageMakerExecutionRole, ...)
# before the old ones are removed, so the update fails and rolls back.
if [[ "$EXISTING_STATUS" != "NONE" ]] && ! aws cloudformation describe-stack-resource \
    --stack-name "$STACK_NAME" --logical-resource-id PlatformStack \
    --region "$AWS_REGION" --profile "$AWS_PROFILE" >/dev/null 2>&1; then
  die "Stack '$STACK_NAME' was created from the single-file template, which is now split into nested stacks. Run ./scripts/teardown-notebook-resources.sh and ./scripts/teardown.sh, then rerun deploy."
fi

# ---- 2. Ensure assets bucket ------------------------------------------------
# S3 API quirk: the us-east-1 region must NOT be passed via
# --create-bucket-configuration (the API rejects LocationConstraint=us-east-1).
# For every other region it's required.

log "Ensuring assets bucket: $ASSETS_BUCKET"
if aws s3api head-bucket --bucket "$ASSETS_BUCKET" --profile "$AWS_PROFILE" 2>/dev/null; then
  ok "Bucket already exists"
else
  log "Creating bucket in $AWS_REGION"
  if [[ "$AWS_REGION" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "$ASSETS_BUCKET" --profile "$AWS_PROFILE" >/dev/null
  else
    aws s3api create-bucket --bucket "$ASSETS_BUCKET" \
      --create-bucket-configuration "LocationConstraint=$AWS_REGION" \
      --region "$AWS_REGION" --profile "$AWS_PROFILE" >/dev/null
  fi
  # Lock the bucket down: no public ACL/policy ever, SSE-S3 enforced.
  aws s3api put-public-access-block --bucket "$ASSETS_BUCKET" \
    --public-access-block-configuration \
      "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" \
    --profile "$AWS_PROFILE" >/dev/null
  aws s3api put-bucket-encryption --bucket "$ASSETS_BUCKET" \
    --server-side-encryption-configuration \
      '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}' \
    --profile "$AWS_PROFILE" >/dev/null
  ok "Bucket created (encrypted, public-access-block)"
fi

# ---- 3. Upload workshop.zip + sample-data -----------------------------------
# workshop.zip is pulled by the Studio lifecycle config.
# sample-data/*.json is read by the CFN sample-data-loader Lambda at deploy time.

log "Uploading workshop.zip"
aws s3 cp "$WORKSHOP_ZIP" "s3://$ASSETS_BUCKET/$ASSETS_PREFIX/workshop.zip" \
  --profile "$AWS_PROFILE" --only-show-errors
ok "Uploaded workshop.zip"

if [[ -d "$SAMPLE_DATA_SRC" ]]; then
  log "Uploading sample-data"
  aws s3 cp "$SAMPLE_DATA_SRC/" "s3://$ASSETS_BUCKET/$ASSETS_PREFIX/sample-data/" \
    --recursive --profile "$AWS_PROFILE" --only-show-errors --exclude "*/.DS_Store"
  ok "Uploaded sample-data"
else
  warn "Sample-data folder not found: $SAMPLE_DATA_SRC"
fi

# ---- 4. Upload templates, derive URL ---------------------------------------
# agent-stack.yaml is a parent of one nested stack per layer (stacks/*.yaml).
# Nested stacks must be loaded from S3, so the child templates go to
# <prefix>/stacks/ — the TemplateURL the parent points each nested stack at.
# The parent is uploaded too and deployed with --template-url.

log "Uploading templates to S3"
aws s3 cp "$STACKS_DIR/" "s3://$ASSETS_BUCKET/$ASSETS_PREFIX/stacks/" \
  --recursive --exclude "*" --include "*.yaml" \
  --profile "$AWS_PROFILE" --only-show-errors
aws s3 cp "$TEMPLATE_FILE" "s3://$ASSETS_BUCKET/$ASSETS_PREFIX/agent-stack.yaml" \
  --profile "$AWS_PROFILE" --only-show-errors
TEMPLATE_URL="https://${ASSETS_BUCKET}.s3.${AWS_REGION}.amazonaws.com/${ASSETS_PREFIX}/agent-stack.yaml"
ok "Uploaded parent + $(ls "$STACKS_DIR"/*.yaml | wc -l | tr -d ' ') nested templates → $TEMPLATE_URL"

# ---- 5. Validate ------------------------------------------------------------
# Catches template syntax and basic reference errors before we attempt to
# create or update the stack. Each nested template is validated on its own,
# because validating the parent does not open its children.

log "Validating templates"
# Validate the copies just uploaded to S3 (--template-url), not the local files
# (--template-body): an inline body is capped at 51,200 bytes, and the L3
# template is larger. S3-hosted templates may be up to 1 MB, which is also what
# the parent's nested-stack TemplateURLs use.
S3_BASE="https://${ASSETS_BUCKET}.s3.${AWS_REGION}.amazonaws.com/${ASSETS_PREFIX}"
for tpl in "$TEMPLATE_FILE" "$STACKS_DIR"/*.yaml; do
  if [[ "$tpl" == "$TEMPLATE_FILE" ]]; then
    url="$S3_BASE/agent-stack.yaml"
  else
    url="$S3_BASE/stacks/$(basename "$tpl")"
  fi
  aws cloudformation validate-template \
    --template-url "$url" \
    --region "$AWS_REGION" --profile "$AWS_PROFILE" >/dev/null \
    || die "Template is invalid: $tpl"
done
ok "Templates are valid"

# ---- 6. Create or update ---------------------------------------------------
# First deploy: pass template params explicitly. The caller's ARN seeds
# ParticipantRoleArn so the deployer gets Studio access by default.
#
# Subsequent deploys: use UsePreviousValue to keep whatever the stack was
# created with. A no-op update returns "No updates are to be performed" which
# we treat as success, since the asset re-upload in step 4 is the real goal
# of the re-run (notebook or sample-data edits without template changes).

if [[ "$EXISTING_STATUS" == "NONE" ]]; then
  log "Creating stack: $STACK_NAME"
  aws cloudformation create-stack \
    --stack-name "$STACK_NAME" \
    --region "$AWS_REGION" --profile "$AWS_PROFILE" \
    --template-url "$TEMPLATE_URL" \
    --capabilities CAPABILITY_NAMED_IAM \
    --parameters \
        ParameterKey=ParticipantRoleArn,ParameterValue="$CALLER_ARN" \
        ParameterKey=AssetsBucketName,ParameterValue="$ASSETS_BUCKET" \
        ParameterKey=AssetsBucketPrefix,ParameterValue="${ASSETS_PREFIX%/}/" \
    --query 'StackId' --output text >/dev/null

  log "Waiting for stack-create-complete (15-20 minutes)…"
  echo "  Watch events: aws cloudformation describe-stack-events --stack-name $STACK_NAME --region $AWS_REGION --profile $AWS_PROFILE"
  aws cloudformation wait stack-create-complete \
    --stack-name "$STACK_NAME" \
    --region "$AWS_REGION" --profile "$AWS_PROFILE"
  ok "Stack created"
else
  log "Stack exists (status=$EXISTING_STATUS). Submitting update."
  # update-stack returns a non-zero exit code when CFN detects no diff. We
  # need to capture that case and treat it as success — otherwise set -e
  # would kill the script on a legitimate "nothing to change" result.
  set +e
  UPDATE_OUT="$(aws cloudformation update-stack \
    --stack-name "$STACK_NAME" \
    --region "$AWS_REGION" --profile "$AWS_PROFILE" \
    --template-url "$TEMPLATE_URL" \
    --capabilities CAPABILITY_NAMED_IAM \
    --parameters \
        ParameterKey=ParticipantRoleArn,UsePreviousValue=true \
        ParameterKey=AssetsBucketName,UsePreviousValue=true \
        ParameterKey=AssetsBucketPrefix,ParameterValue="${ASSETS_PREFIX%/}/" 2>&1)"
  UPDATE_EXIT=$?
  set -e

  if [[ $UPDATE_EXIT -ne 0 ]]; then
    if echo "$UPDATE_OUT" | grep -q "No updates are to be performed"; then
      warn "No template changes. Assets were re-uploaded; skipping stack update."
    else
      die "update-stack failed: $UPDATE_OUT"
    fi
  else
    log "Waiting for stack-update-complete…"
    aws cloudformation wait stack-update-complete \
      --stack-name "$STACK_NAME" \
      --region "$AWS_REGION" --profile "$AWS_PROFILE"
    ok "Stack updated"
  fi
fi

# ---- 7. Verify --------------------------------------------------------------
# Non-authoritative sanity checks against the deployed stack. Each check is
# best-effort — missing values become "N/A" rather than failing the script,
# since partial results are still useful post-deploy.

echo
log "Stack outputs"
aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$AWS_REGION" --profile "$AWS_PROFILE" \
  --query 'Stacks[0].{Status:StackStatus,Outputs:Outputs}' \
  --output table

# DynamoDB expected shape: ~17 orders seeded by the sample-data-loader Lambda,
# and 4 GSIs (customer_id, status, order_date, product).
log "DynamoDB CustomerOrders table"
ITEM_COUNT="$(aws dynamodb scan \
  --table-name CustomerOrders \
  --region "$AWS_REGION" --profile "$AWS_PROFILE" \
  --select COUNT --query 'Count' --output text 2>/dev/null || echo "N/A")"
GSI_COUNT="$(aws dynamodb describe-table \
  --table-name CustomerOrders \
  --region "$AWS_REGION" --profile "$AWS_PROFILE" \
  --query 'length(Table.GlobalSecondaryIndexes)' --output text 2>/dev/null || echo "N/A")"
echo "  Items : $ITEM_COUNT  (expected ~17)"
echo "  GSIs  : $GSI_COUNT  (expected 4)"

# SSM /anycompany/agentcore/* holds the handoff values the notebooks read on
# startup: Cognito ids, gateway IDs, registry name, harness model id, etc.
log "SSM parameters under /anycompany/agentcore/"
aws ssm describe-parameters \
  --parameter-filters "Key=Name,Option=BeginsWith,Values=/anycompany/agentcore/" \
  --region "$AWS_REGION" --profile "$AWS_PROFILE" \
  --query 'Parameters[].{Name:Name,Type:Type,Modified:LastModifiedDate}' \
  --output table

# Studio presigned URL — short-lived (5 min) link that opens JupyterLab for
# the workshopuser profile. May be empty if the JupyterLab app hasn't started
# yet; that's expected on first deploy and resolves within a few minutes.
log "Studio presigned URL (valid 5 min)"
DOMAIN_ID="$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region "$AWS_REGION" --profile "$AWS_PROFILE" \
  --query 'Stacks[0].Outputs[?OutputKey==`SageMakerDomainId`].OutputValue' \
  --output text 2>/dev/null)"
if [[ -n "$DOMAIN_ID" && "$DOMAIN_ID" != "None" ]]; then
  PRESIGNED_URL="$(aws sagemaker create-presigned-domain-url \
    --domain-id "$DOMAIN_ID" \
    --user-profile-name workshopuser \
    --space-name aapcf-workshop-space \
    --region "$AWS_REGION" --profile "$AWS_PROFILE" \
    --query AuthorizedUrl --output text 2>/dev/null || echo "")"
  if [[ -n "$PRESIGNED_URL" ]]; then
    echo "  $PRESIGNED_URL"
  else
    warn "Presigned URL unavailable yet (JupyterLab app may still be starting)"
  fi
else
  warn "SageMaker domain ID not found in stack outputs"
fi

echo
ok "Deploy complete"
