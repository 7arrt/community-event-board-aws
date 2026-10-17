#!/usr/bin/env bash
#
# Community Event Board — full infrastructure deploy.
#
# Deploys the CloudFormation stacks in dependency order and emits their outputs
# as a GitLab dotenv file.
#
# Every resource identifier is read from describe-stacks at run time. Never
# hardcode an identifier, account id, or region here — the region comes from
# $AWS_DEFAULT_REGION and credentials come from the environment only. CI has no
# ~/.aws/config and no local profile.
#
# WHY SIX DEPLOYMENT PASSES
#   The bucket's CORS must name the CloudFront distribution domain as the
#   browser origin, and that domain does not exist until the ALB/CloudFront
#   stack has been created. Storage is therefore deployed once with a
#   placeholder and updated in step 4 with the real domain. Omitting step 4
#   leaves the deploy looking successful while every browser request fails the
#   CORS preflight.

set -euo pipefail

REGION="${AWS_DEFAULT_REGION:?AWS_DEFAULT_REGION must be set}"

# --- helpers -----------------------------------------------------------------

stack_output() {
  # stack_output <stack> <output-key>
  aws cloudformation describe-stacks \
    --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" \
    --output text
}

# A stack left in ROLLBACK_COMPLETE by a failed create can never be updated or
# deployed over, so every subsequent run fails with "is in ROLLBACK_COMPLETE
# state and can not be updated". Clear it first, otherwise one bad deploy wedges
# the pipeline permanently.
clear_rollback() {
  local stack="$1" region="${2:-$REGION}" status
  status="$(aws cloudformation describe-stacks --region "$region" --stack-name "$stack" \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null || true)"
  if [ "$status" = "ROLLBACK_COMPLETE" ]; then
    echo "    $stack is in ROLLBACK_COMPLETE from a previous failed deploy; deleting it"
    aws cloudformation delete-stack --region "$region" --stack-name "$stack"
    aws cloudformation wait stack-delete-complete --region "$region" --stack-name "$stack" || true
  fi
}

require_output() {
  # require_output <value> <name> <stack>
  if [ -z "$1" ] || [ "$1" = "None" ]; then
    echo "ERROR: output '$2' is missing from stack '$3'." >&2
    echo "       Check that the stack completed successfully." >&2
    exit 1
  fi
}

# The CloudFront origin-facing managed prefix list id is region-scoped, so it is
# resolved at run time rather than hardcoded. Fall back to the eu-west-1 value if
# the query returns nothing, which keeps a working region from failing outright.
resolve_prefix_list() {
  local id
  id="$(aws ec2 describe-managed-prefix-lists \
        --filters "Name=prefix-list-name,Values=com.amazonaws.global.cloudfront.origin-facing" \
        --query "PrefixLists[0].PrefixListId" --output text 2>/dev/null || true)"
  if [ -z "$id" ] || [ "$id" = "None" ]; then
    echo "WARN: could not resolve the CloudFront origin-facing prefix list for" \
         "$REGION; falling back to pl-4fa04526 (eu-west-1)." >&2
    id="pl-4fa04526"
  fi
  echo "$id"
}

# --- step 1: storage ---------------------------------------------------------
# CloudFrontDomain is a placeholder; step 4 replaces it. The template declares
# the parameter with no default precisely so this ordering is visible.

clear_rollback community-board-storage

echo "==> [1/6] community-board-storage (placeholder CORS origin)"
aws cloudformation deploy \
  --region "$REGION" \
  --stack-name community-board-storage \
  --template-file infra/storage.yaml \
  --capabilities CAPABILITY_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
      ProjectName=community-board \
      ImagePrefix=images/ \
      CloudFrontDomain=pending.invalid

BUCKET_NAME="$(stack_output community-board-storage ImagesBucketName)"
require_output "$BUCKET_NAME" ImagesBucketName community-board-storage
echo "    images bucket: $BUCKET_NAME"

# --- step 2: compute --------------------------------------------------------
# Needs the role that storage created. CAPABILITY_IAM because it creates an
# IAM InstanceProfile.

echo "==> [2/6] community-board-compute"
APPROLE_ARN="$(stack_output community-board-storage AppRoleArn)"
require_output "$APPROLE_ARN" AppRoleArn community-board-storage

aws cloudformation deploy \
  --region "$REGION" \
  --stack-name community-board-compute \
  --template-file infra/compute.yaml \
  --capabilities CAPABILITY_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides "AppRoleArn=$APPROLE_ARN"

VPC_ID="$(stack_output community-board-compute VpcId)"
SUBNET_IDS="$(stack_output community-board-compute PublicSubnetIds)"
ALB_SG_ID="$(stack_output community-board-compute AlbSecurityGroupId)"
EC2_SG_ID="$(stack_output community-board-compute Ec2SecurityGroupId)"
INSTANCE_ID="$(stack_output community-board-compute InstanceId)"

for pair in "VPC_ID:$VPC_ID" "PublicSubnetIds:$SUBNET_IDS" \
            "AlbSecurityGroupId:$ALB_SG_ID" "Ec2SecurityGroupId:$EC2_SG_ID" \
            "InstanceId:$INSTANCE_ID"; do
  require_output "${pair#*:}" "${pair%%:*}" community-board-compute
done

# --- step 3: ALB and CloudFront ---------------------------------------------
# Consumes six identifiers, all read above.

echo "==> [3/6] community-board-alb-cloudfront"
clear_rollback community-board-alb-cloudfront
PREFIX_LIST_ID="$(resolve_prefix_list)"
echo "    CloudFront origin-facing prefix list: $PREFIX_LIST_ID"

aws cloudformation deploy \
  --region "$REGION" \
  --stack-name community-board-alb-cloudfront \
  --template-file infra/alb-cloudfront.yaml \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
      "VpcId=$VPC_ID" \
      "PublicSubnetIds=$SUBNET_IDS" \
      "AlbSecurityGroupId=$ALB_SG_ID" \
      "Ec2SecurityGroupId=$EC2_SG_ID" \
      "InstanceId=$INSTANCE_ID" \
      "ImagesBucketName=$BUCKET_NAME" \
      ImagePrefix=images/ \
      "CfOriginPrefixListId=$PREFIX_LIST_ID"

CF_DOMAIN="$(stack_output community-board-alb-cloudfront DistributionDomainName)"
DIST_ID="$(stack_output community-board-alb-cloudfront DistributionId)"
ALB_DNS="$(stack_output community-board-alb-cloudfront AlbDNSName)"
TG_ARN="$(stack_output community-board-alb-cloudfront TargetGroupArn)"

require_output "$CF_DOMAIN" DistributionDomainName community-board-alb-cloudfront
require_output "$TG_ARN" TargetGroupArn community-board-alb-cloudfront

# --- step 4: correct the CORS origin ----------------------------------------
# Only now does the distribution domain exist.

echo "==> [4/6] community-board-storage (real CORS origin: $CF_DOMAIN)"
aws cloudformation deploy \
  --region "$REGION" \
  --stack-name community-board-storage \
  --template-file infra/storage.yaml \
  --capabilities CAPABILITY_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
      ProjectName=community-board \
      ImagePrefix=images/ \
      "CloudFrontDomain=$CF_DOMAIN"

# --- step 5: monitoring ------------------------------------------------------
# Deploys last because it consumes outputs from both the ALB and storage stacks.
# Alarms exist but evaluate to no data until the traffic is flowing, so there is
# nothing to alarm about before this point.

echo "==> [5/6] community-board-monitoring"
clear_rollback community-board-monitoring
# Resolve the full ALB ARN by its DNS name. AWS generates the load balancer name,
# so it cannot be reconstructed from the stack outputs the way the other values
# are.
ALB_ARN="$(aws elbv2 describe-load-balancers \
  --query "LoadBalancers[?DNSName=='$ALB_DNS'].LoadBalancerArn | [0]" \
  --output text)"
require_output "$ALB_ARN" LoadBalancerArn community-board-alb-cloudfront
ALB_DIMENSION="${ALB_ARN#*:loadbalancer/}"
TG_DIMENSION="${TG_ARN##*:}"
require_output "$ALB_DIMENSION" LoadBalancerDimension community-board-alb-cloudfront
require_output "$TG_DIMENSION" TargetGroupDimension community-board-alb-cloudfront
EVENTS_TABLE_NAME="$(stack_output community-board-storage EventsTableName)"
require_output "$EVENTS_TABLE_NAME" EventsTableName community-board-storage

aws cloudformation deploy \
  --region "$REGION" \
  --stack-name community-board-monitoring \
  --template-file infra/monitoring.yaml \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
      "AlbDimensionValue=$ALB_DIMENSION" \
      "TargetGroupDimensionValue=$TG_DIMENSION" \
      "ImagesBucketName=$BUCKET_NAME" \
      "EventsTableName=$EVENTS_TABLE_NAME"

    # CloudFront metrics are global but CloudWatch exposes them only in us-east-1.
    # Keep this stack separate so it can never inherit the pipeline region.
    echo "==> [6/6] community-board-monitoring-cloudfront (us-east-1)"
    clear_rollback community-board-monitoring-cloudfront us-east-1
    aws cloudformation deploy \
      --region us-east-1 \
      --stack-name community-board-monitoring-cloudfront \
      --template-file infra/monitoring-cloudfront.yaml \
      --no-fail-on-empty-changeset \
      --parameter-overrides "DistributionId=$DIST_ID"

# --- emit dotenv -------------------------------------------------------------

cat > deploy.env <<EOF
IMAGES_BUCKET_NAME=$BUCKET_NAME
EVENTS_TABLE_NAME=$EVENTS_TABLE_NAME
VPC_ID=$VPC_ID
ALB_SG_ID=$ALB_SG_ID
EC2_SG_ID=$EC2_SG_ID
INSTANCE_ID=$INSTANCE_ID
CF_DOMAIN=$CF_DOMAIN
DISTRIBUTION_ID=$DIST_ID
ALB_DNS_NAME=$ALB_DNS
ALB_ARN=$ALB_ARN
TARGET_GROUP_ARN=$TG_ARN
MONITORING_STACK=community-board-monitoring
CLOUDFRONT_MONITORING_STACK=community-board-monitoring-cloudfront
EOF

echo "==> deploy complete. Viewer endpoint: https://$CF_DOMAIN"