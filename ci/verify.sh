#!/usr/bin/env bash
#
# Verifies the deployment is genuinely serving, then enforces the security
# boundary. Exits non-zero on any failure so the pipeline blocks.
#
# Why this gate exists: the pipeline deploys into a sandbox nobody can inspect,
# and a non-zero exit is what marks a deployment as failed. A pipeline that
# exits 0 on a half-deployed stack is worse than one that fails loudly.

set -euo pipefail

REGION="${AWS_DEFAULT_REGION:?AWS_DEFAULT_REGION must be set}"
CF_DOMAIN="${CF_DOMAIN:?CF_DOMAIN must be set from deploy.env}"
TG_ARN="${TARGET_GROUP_ARN:?TARGET_GROUP_ARN must be set from deploy.env}"

HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-600}"
START_WAIT_SECONDS="${START_WAIT_SECONDS:-60}"

fail() { echo "FAIL: $*" >&2; exit 1; }

echo "==> waiting ${START_WAIT_SECONDS}s for the instance to finish booting"
sleep "$START_WAIT_SECONDS"

# --- 1. target group must reach healthy --------------------------------------
# Health checks run every 30s and need 5 consecutive successes, so a fresh
# target can legitimately take ~3 minutes. Poll rather than assume.

echo "==> waiting for target group to report healthy (timeout ${HEALTH_TIMEOUT_SECONDS}s)"
deadline=$(( $(date +%s) + HEALTH_TIMEOUT_SECONDS ))
last_state=""

while [ "$(date +%s)" -lt "$deadline" ]; do
  state="$(aws elbv2 describe-target-health \
    --target-group-arn "$TG_ARN" \
    --query 'TargetHealthDescriptions[0].TargetHealth.State' \
    --output text 2>/dev/null || echo "unknown")"

  if [ "$state" != "$last_state" ]; then
    echo "    target state: $state"
    last_state="$state"
  fi

  [ "$state" = "healthy" ] && break
  sleep 15
done

if [ "$state" != "healthy" ]; then
  aws elbv2 describe-target-health --target-group-arn "$TG_ARN" \
    --query 'TargetHealthDescriptions[0].TargetHealth' --output json >&2 || true
  fail "target never became healthy within ${HEALTH_TIMEOUT_SECONDS}s (last state: $state)"
fi
echo "    target is healthy"

# --- 2. the viewer endpoint must serve the app -------------------------------

echo "==> checking the CloudFront viewer endpoint"
health_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "https://$CF_DOMAIN/health" || true)"
[ "$health_code" = "200" ] || fail "GET /health returned $health_code, expected 200"
echo "    GET /health -> 200"

events_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "https://$CF_DOMAIN/events" || true)"
[ "$events_code" = "200" ] || fail "GET /events returned $events_code, expected 200"
echo "    GET /events -> 200"

# Viewer HTTP must redirect to HTTPS; a 200 here would mean the redirect
# policy is not applied and plaintext traffic is accepted.
redirect_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "http://$CF_DOMAIN/health" || true)"
[ "$redirect_code" = "301" ] || fail "plain HTTP returned $redirect_code, expected a 301 to HTTPS"
echo "    GET http://  -> 301 (redirects to HTTPS)"

# --- 3. the security boundary must actually hold ------------------------------
# These are the negative assertions for FR3/NFR1 and FR6/NFR1. A 200 on either
# means the boundary has been breached, so they are treated as failures rather
# than skipped.

echo "==> checking the origin-access boundary"
direct_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "http://$ALB_DNS_NAME/health" || true)"
if [[ "$direct_code" =~ ^[1-3][0-9]{2}$ ]]; then
  fail "the ALB answered a direct request with HTTP $direct_code — only CloudFront may reach the app tier"
fi
echo "    direct ALB request blocked (curl code $direct_code)"

s3_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
  "https://$IMAGES_BUCKET_NAME.s3.$REGION.amazonaws.com/images/probe-denied.jpg" || true)"
if [ "$s3_code" = "200" ]; then
  fail "the images bucket served a direct object request — the bucket is public"
fi
[ "$s3_code" = "403" ] || fail "direct S3 access returned $s3_code, expected 403"
echo "    direct S3 object access -> 403"

echo "==> all checks passed"