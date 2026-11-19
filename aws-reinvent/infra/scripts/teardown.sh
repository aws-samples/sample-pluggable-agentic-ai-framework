#!/usr/bin/env bash
# ------------------------------------------------------------------------------
# teardown.sh — delete the stack AND the assets S3 bucket.
#
# Stops the JupyterLab app first so the SageMaker Space/Domain can delete
# cleanly, then calls DeleteStack and waits for completion, then empties and
# deletes the assets bucket.
#
# Full account wipe: pair with ./scripts/teardown-notebook-resources.sh for
# resources that notebooks create via API calls (AgentCore, Bedrock KB,
# Omni, etc.) which live outside the stack.
#
# Usage:
#   export AWS_PROFILE=<account-profile>
#   ./scripts/teardown.sh
# ------------------------------------------------------------------------------
set -euo pipefail

# ---- Config -----------------------------------------------------------------

: "${AWS_PROFILE:?Set AWS_PROFILE before running — e.g. 'export AWS_PROFILE=my-profile'}"
AWS_REGION="${AWS_REGION:-us-east-1}"
STACK_NAME="${STACK_NAME:-agent-stack}"

log()  { printf "\033[0;36m==>\033[0m %s\n" "$*"; }
ok()   { printf "\033[0;32m✓\033[0m   %s\n" "$*"; }
warn() { printf "\033[0;33m!\033[0m   %s\n" "$*" >&2; }
die()  { printf "\033[0;31m✗\033[0m   %s\n" "$*" >&2; exit 1; }

command -v aws >/dev/null || die "aws CLI not on PATH"

ACCOUNT_ID="$(aws sts get-caller-identity --profile "$AWS_PROFILE" --query Account --output text)"
ASSETS_BUCKET="${ASSETS_BUCKET:-aapcf-reinvent-assets-${ACCOUNT_ID}}"

cat <<EOF
Configuration
  Profile : $AWS_PROFILE
  Region  : $AWS_REGION
  Account : $ACCOUNT_ID
  Stack   : $STACK_NAME
  Bucket  : $ASSETS_BUCKET  (will be deleted)

EOF

# ---- Stack delete -----------------------------------------------------------

STATUS="$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region "$AWS_REGION" --profile "$AWS_PROFILE" \
  --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo NONE)"

if [[ "$STATUS" == "NONE" ]]; then
  warn "Stack '$STACK_NAME' does not exist — skipping stack delete"
else
  log "Stack $STACK_NAME is in state: $STATUS"

  # Stop the JupyterLab app first so the Space/Domain can delete cleanly.
  DOMAIN_ID="$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" --region "$AWS_REGION" --profile "$AWS_PROFILE" \
    --query 'Stacks[0].Outputs[?OutputKey==`SageMakerDomainId`].OutputValue' \
    --output text 2>/dev/null || echo "")"

  if [[ -n "$DOMAIN_ID" && "$DOMAIN_ID" != "None" ]]; then
    log "Stopping JupyterLab app (if running)"
    APP_STATUS="$(aws sagemaker describe-app \
      --domain-id "$DOMAIN_ID" --space-name aapcf-workshop-space \
      --app-type JupyterLab --app-name default \
      --region "$AWS_REGION" --profile "$AWS_PROFILE" \
      --query 'Status' --output text 2>/dev/null || echo "ResourceNotFound")"

    if [[ "$APP_STATUS" == "InService" || "$APP_STATUS" == "Pending" ]]; then
      aws sagemaker delete-app \
        --domain-id "$DOMAIN_ID" --space-name aapcf-workshop-space \
        --app-type JupyterLab --app-name default \
        --region "$AWS_REGION" --profile "$AWS_PROFILE" >/dev/null
      log "Waiting for app to delete…"
      for _ in $(seq 1 60); do
        sleep 10
        CURRENT="$(aws sagemaker describe-app \
          --domain-id "$DOMAIN_ID" --space-name aapcf-workshop-space \
          --app-type JupyterLab --app-name default \
          --region "$AWS_REGION" --profile "$AWS_PROFILE" \
          --query 'Status' --output text 2>/dev/null || echo "ResourceNotFound")"
        [[ "$CURRENT" == "Deleted" || "$CURRENT" == "ResourceNotFound" ]] && break
      done
      ok "App stopped"
    else
      ok "App already stopped (status=$APP_STATUS)"
    fi
  fi

  # Couchbase option: the Refund tools Lambda runs in the stack's egress subnet.
  # Its network interfaces block deleting that subnet and security group, so move
  # the function out of the VPC (if it still exists) and wait until AWS releases
  # them. Lambda can take up to ~20 minutes to delete unused interfaces.
  REFUND_FN="anycompany_refund_tools"
  if aws lambda get-function-configuration --function-name "$REFUND_FN" \
       --region "$AWS_REGION" --profile "$AWS_PROFILE" \
       --query 'VpcConfig.SubnetIds[0]' --output text 2>/dev/null | grep -q '^subnet-'; then
    log "Moving $REFUND_FN out of the Couchbase egress subnet"
    aws lambda update-function-configuration --function-name "$REFUND_FN" \
      --vpc-config SubnetIds=[],SecurityGroupIds=[] \
      --region "$AWS_REGION" --profile "$AWS_PROFILE" >/dev/null
  fi
  eni_count() {
    aws ec2 describe-network-interfaces --region "$AWS_REGION" --profile "$AWS_PROFILE" \
      --filters "Name=description,Values=AWS Lambda VPC ENI-${REFUND_FN}*" \
      --query 'length(NetworkInterfaces)' --output text
  }
  if [[ "$(eni_count)" != "0" ]]; then
    log "Waiting for the Refund Lambda's network interfaces to be released (up to 30 min)…"
    for _ in $(seq 1 60); do
      [[ "$(eni_count)" == "0" ]] && break
      # Detached ("available") interfaces are not released by Lambda; delete them.
      for eni in $(aws ec2 describe-network-interfaces --region "$AWS_REGION" --profile "$AWS_PROFILE" \
          --filters "Name=description,Values=AWS Lambda VPC ENI-${REFUND_FN}*" "Name=status,Values=available" \
          --query 'NetworkInterfaces[].NetworkInterfaceId' --output text); do
        aws ec2 delete-network-interface --network-interface-id "$eni" \
          --region "$AWS_REGION" --profile "$AWS_PROFILE" >/dev/null 2>&1 || true
      done
      sleep 30
    done
    if [[ "$(eni_count)" == "0" ]]; then ok "Lambda network interfaces released"
    else warn "Lambda network interfaces still present; the stack delete may fail on the egress subnet. Re-run this script."; fi
  fi

  log "Deleting stack $STACK_NAME"
  aws cloudformation delete-stack \
    --stack-name "$STACK_NAME" \
    --region "$AWS_REGION" --profile "$AWS_PROFILE"

  log "Waiting for stack-delete-complete…"
  aws cloudformation wait stack-delete-complete \
    --stack-name "$STACK_NAME" \
    --region "$AWS_REGION" --profile "$AWS_PROFILE"
  ok "Stack deleted"
fi

# ---- Orphaned resource-policy sweep -----------------------------------------
# The L6 ObservabilityStack creates a CloudWatch Logs resource policy named
# aapcf-xray-spans-<stack>. A failed/rolled-back deploy can leave it behind
# (CFN only deletes policies it created successfully), and the orphan then
# blocks every future deploy with a name-conflict. Delete it defensively.

POLICY_NAME="aapcf-xray-spans-${STACK_NAME}"
if aws logs describe-resource-policies --region "$AWS_REGION" --profile "$AWS_PROFILE" \
     --query "resourcePolicies[?policyName=='${POLICY_NAME}'] | length(@)" \
     --output text 2>/dev/null | grep -q '^1$'; then
  log "Deleting leftover resource policy: $POLICY_NAME"
  if aws logs delete-resource-policy --policy-name "$POLICY_NAME" \
       --region "$AWS_REGION" --profile "$AWS_PROFILE" >/dev/null 2>&1; then
    ok "Resource policy deleted"
  else
    warn "Could not delete resource policy $POLICY_NAME (delete it manually if a redeploy conflicts)"
  fi
else
  ok "No leftover resource policy ($POLICY_NAME)"
fi

# ---- Stack log groups ---------------------------------------------------------
# Lambda, CodeBuild and SageMaker endpoint log groups are created by the services
# at runtime, not by CloudFormation, so deleting the stack leaves them behind.
# They run until the stack is gone (custom resources log during delete), so
# sweep them here, after the delete. Only groups named after this stack.
log "Deleting log groups left by stack $STACK_NAME"
LG_COUNT=0
for prefix in "/aws/lambda/${STACK_NAME}-" "/aws/codebuild/${STACK_NAME}-" "/aws/sagemaker/Endpoints/${STACK_NAME}-"; do
  for lg in $(aws logs describe-log-groups --log-group-name-prefix "$prefix" \
      --region "$AWS_REGION" --profile "$AWS_PROFILE" \
      --query 'logGroups[].logGroupName' --output text 2>/dev/null || true); do
    [[ -z "$lg" || "$lg" == "None" ]] && continue
    aws logs delete-log-group --log-group-name "$lg" \
      --region "$AWS_REGION" --profile "$AWS_PROFILE" >/dev/null 2>&1 \
      && LG_COUNT=$((LG_COUNT + 1)) \
      || warn "Could not delete log group $lg"
  done
done
ok "Deleted $LG_COUNT stack log group(s)"
# ---- Bucket cleanup ---------------------------------------------------------

if aws s3api head-bucket --bucket "$ASSETS_BUCKET" --profile "$AWS_PROFILE" 2>/dev/null; then
  log "Emptying + deleting bucket $ASSETS_BUCKET"
  aws s3 rb "s3://$ASSETS_BUCKET" --force --profile "$AWS_PROFILE" >/dev/null
  ok "Bucket deleted"
else
  warn "Bucket $ASSETS_BUCKET does not exist — skipping"
fi

echo
ok "Teardown complete"
