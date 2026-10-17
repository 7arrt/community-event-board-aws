#!/usr/bin/env bash
#
# Graceful cleanup: deletes every stack this project creates, in reverse
# dependency order.
#
# MUST NOT run on a push to main. Deploys target the same account, so a cleanup
# on every push would destroy the environment being verified. This is wired as
# a manual job in .gitlab-ci.yml and a manual workflow in
# .github/workflows/cleanup.yml.
#
# CloudFormation deletes each stack's own resources. Anything the stacks do not
# own is listed at the end for manual deletion.

set -euo pipefail

REGION="${AWS_DEFAULT_REGION:?AWS_DEFAULT_REGION must be set}"

# The CloudFront monitoring stack is in us-east-1 and is independent of the
# regional stack. Delete both monitoring stacks before their monitored origins.
STACKS=(
  community-board-monitoring-cloudfront
  community-board-monitoring
  community-board-alb-cloudfront
  community-board-compute
  community-board-storage
)

stack_region() {
  if [ "$1" = "community-board-monitoring-cloudfront" ]; then
    echo "us-east-1"
  else
    echo "$REGION"
  fi
}

# Buckets are not emptied by CloudFormation when they still hold objects.
# Empty any that survive so the stack delete does not fail.
echo "==> emptying image buckets where present"
for stack in community-board-storage community-board-alb-cloudfront; do
  bucket="$(aws cloudformation describe-stacks \
    --stack-name "$stack" \
    --query "Stacks[0].Outputs[?OutputKey=='ImagesBucketName'].OutputValue" \
    --output text 2>/dev/null || true)"
  [ -z "$bucket" ] || [ "$bucket" = "None" ] && continue
  aws s3 rm "s3://$bucket" --recursive --only-show-errors 2>/dev/null || true
  echo "    emptied $bucket"
done

echo "==> deleting stacks in reverse dependency order"
for stack in "${STACKS[@]}"; do
  region="$(stack_region "$stack")"
  if aws cloudformation describe-stacks --region "$region" --stack-name "$stack" >/dev/null 2>&1; then
    echo "    deleting $stack"
    aws cloudformation delete-stack --region "$region" --stack-name "$stack" || true
  else
    echo "    $stack does not exist, skipping"
  fi
done

echo "==> waiting for deletions to settle"
for stack in "${STACKS[@]}"; do
  region="$(stack_region "$stack")"
  aws cloudformation wait stack-delete-complete --region "$region" --stack-name "$stack" 2>/dev/null \
    || echo "    $stack is not present or did not fully delete; continuing"
done

# --- report what may remain ---------------------------------------------------
echo
echo "==> remaining stack-like resources"
aws cloudformation list-stacks --stack-status-filter DELETE_COMPLETE 2>/dev/null \
  | head -n 5 || true

vpcs="$(aws ec2 describe-vpcs --filters Name=isDefault,Values=false \
  --query 'Vpcs[].VpcId' --output json 2>/dev/null || echo '[]')"
if [ "$vpcs" != "[]" ]; then
  echo "    non-default VPCs still present: $vpcs"
  echo "    delete with: aws ec2 delete-vpc --vpc-id <id>  # requires no dependent resources"
fi

eips="$(aws ec2 describe-addresses --query 'Addresses[].PublicIp' --output text 2>/dev/null || true)"
if [ -n "$eips" ] && [ "$eips" != "None" ]; then
  echo "    Elastic IPs still allocated: $(echo "$eips" | tr '\t' ' ')"
  echo "    release with: aws ec2 release-address --allocation-id <id>"
fi

echo
echo "==> cleanup complete"