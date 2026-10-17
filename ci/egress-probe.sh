#!/usr/bin/env bash
# Throwaway egress probe.
#
# The stub responder needs no network, so a healthy ALB target proves nothing
# about outbound connectivity. The real application needs boto3 to reach
# DynamoDB and S3, so if the instance has no egress, RSVP and presigned upload
# are dead regardless of what is installed.
#
# SSM is both the channel and the measurement: the SSM agent reaches out to the
# SSM endpoint, so "can SSM reach the instance" and "does the instance have egress"
# are the same question. A TCP timeout on the SSM endpoint means egress is blocked.
#
# Uses direct CLI calls rather than a nested template so nothing depends on
# path handling between git-bash and the Windows AWS CLI.

set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-eu-west-1}"
SUBNET="${PROBE_SUBNET:?PROBE_SUBNET must be set}"
AMI="${PROBE_AMI:?PROBE_AMI must be set}"
ROLE="egress-probe-role"
PROFILE="egress-probe-profile"
SG="egress-probe-sg"
ROLE_CREATED=0
POLICY_ATTACHED=0
PROFILE_CREATED=0
ROLE_ADDED=0

cleanup() {
  local exit_status=$?
  local cleanup_failed=0
  trap - EXIT
  if [ -n "${IID:-}" ]; then
    aws ec2 terminate-instances --instance-ids "$IID" >/dev/null 2>&1 || cleanup_failed=1
    aws ec2 wait instance-terminated --instance-ids "$IID" >/dev/null 2>&1 || cleanup_failed=1
  fi
  if [ -n "${SG_ID:-}" ]; then
    aws ec2 delete-security-group --group-id "$SG_ID" >/dev/null 2>&1 || cleanup_failed=1
  fi
  if [ "$ROLE_ADDED" -eq 1 ]; then
    aws iam remove-role-from-instance-profile --instance-profile-name "$PROFILE" \
      --role-name "$ROLE" >/dev/null 2>&1 || cleanup_failed=1
  fi
  if [ "$PROFILE_CREATED" -eq 1 ]; then
    aws iam delete-instance-profile --instance-profile-name "$PROFILE" >/dev/null 2>&1 || cleanup_failed=1
  fi
  if [ "$POLICY_ATTACHED" -eq 1 ]; then
    aws iam detach-role-policy --role-name "$ROLE" \
      --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore >/dev/null 2>&1 || cleanup_failed=1
  fi
  if [ "$ROLE_CREATED" -eq 1 ]; then
    aws iam delete-role --role-name "$ROLE" >/dev/null 2>&1 || cleanup_failed=1
  fi
  if [ "$cleanup_failed" -eq 0 ]; then
    echo "==> probe resources deleted"
  else
    echo "==> ERROR: probe resource cleanup was incomplete" >&2
    exit 5
  fi
  exit "$exit_status"
}
trap cleanup EXIT

echo "==> creating the probe IAM role, instance profile and security group"
aws iam create-role --role-name "$ROLE" \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
ROLE_CREATED=1
aws iam attach-role-policy --role-name "$ROLE" \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore >/dev/null
POLICY_ATTACHED=1
aws iam create-instance-profile --instance-profile-name "$PROFILE" >/dev/null
PROFILE_CREATED=1
aws iam add-role-to-instance-profile --instance-profile-name "$PROFILE" --role-name "$ROLE" >/dev/null
ROLE_ADDED=1

VPC_ID="$(aws ec2 describe-subnets --filters "Name=subnet-id,Values=$SUBNET" \
  --query 'Subnets[0].VpcId' --output text)"
# run-instances wants the group id, not the name.
SG_ID="$(aws ec2 create-security-group --group-name "$SG" \
  --description "egress probe" --vpc-id "$VPC_ID" \
  --query 'GroupId' --output text)"

echo "==> launching the probe instance (AMI $AMI)"
IID="$(aws ec2 run-instances \
  --image-id "$AMI" \
  --instance-type t3.micro \
  --subnet-id "$SUBNET" \
  --associate-public-ip-address \
  --security-group-ids "$SG_ID" \
  --iam-instance-profile "Name=$PROFILE" \
  --query 'Instances[0].InstanceId' --output text)"
[ -n "$IID" ] || { echo "probe instance failed to launch" >&2; exit 3; }
echo "    instance: $IID"

echo "==> waiting for the SSM agent to register (this is also the egress test)"
ping="None"
for i in $(seq 1 30); do
  ping="$(aws ssm describe-instance-information \
    --filters "Key=InstanceIds,Values=$IID" \
    --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null || echo None)"
  if [ "$ping" = "Online" ]; then
    echo "    SSM Online after ~$((i*15))s — the instance HAS outbound connectivity."
    break
  fi
  echo "    [$((i*15))s] SSM status: $ping"
  sleep 15
done

if [ "$ping" != "Online" ]; then
  echo
  echo "RESULT: EGRESS BLOCKED — the SSM agent never reached the SSM endpoint." >&2
  echo "Console evidence:" >&2
  aws ec2 get-console-output --instance-id "$IID" --query Output --output text 2>/dev/null \
    | grep -i "ssm\|timeout\|unable" | tail -n 5 >&2 || true
  exit 2
fi

echo "==> running probes through SSM Run Command"
COMMANDS='[
  "token=$(curl -sS -m 5 -X PUT -H \"X-aws-ec2-metadata-token-ttl-seconds: 60\" http://169.254.169.254/latest/api/token) && curl -sS -m 5 -H \"X-aws-ec2-metadata-token: $token\" -o /dev/null -w \"IMDS_HTTP:%{http_code}\" http://169.254.169.254/latest/meta-data/ || echo IMDS_FAILED",
  "curl -sS -m 8 -o /dev/null -w \"HTTP:%{http_code},TLS:%{ssl_verify_result}\" https://dynamodb.eu-west-1.amazonaws.com/ || echo DYNAMODB_TCP_TLS_FAILED",
  "curl -sS -m 8 -o /dev/null -w \"HTTP:%{http_code},TLS:%{ssl_verify_result}\" https://s3.eu-west-1.amazonaws.com/ || echo S3_TCP_TLS_FAILED",
  "curl -sS -m 8 -o /dev/null -w \"HTTP:%{http_code},TLS:%{ssl_verify_result}\" https://pypi.org/simple/ || echo PYPI_TCP_TLS_FAILED",
  "python3 -c \"import boto3; print(boto3.__version__)\" 2>&1 || echo BOTO3_NOT_INSTALLED"
]'

CMD_ID="$(aws ssm send-command --instance-ids "$IID" \
  --document-name AWS-RunShellScript \
  --parameters "commands=$COMMANDS" \
  --query 'Command.CommandId' --output text 2>/dev/null)"

if [ -z "$CMD_ID" ]; then
  echo "could not send the SSM command" >&2
  exit 3
fi

status="Pending"
for i in $(seq 1 20); do
  status="$(aws ssm list-command-invocations --command-id "$CMD_ID" \
    --query 'CommandInvocations[0].Status' --output text 2>/dev/null || echo Pending)"
  case "$status" in Success|Failed|TimedOut|Cancelled) break ;; esac
  sleep 6
done

echo "==> status: $status"
echo "==> probe output:"
aws ssm get-command-invocation --command-id "$CMD_ID" --instance-id "$IID" \
  --query 'StandardOutputContent' --output text 2>/dev/null || true
echo
if [ "$status" != "Success" ]; then
  echo "SSM connectivity command did not succeed (status: $status)" >&2
  exit 4
fi
echo "RESULT: EGRESS AVAILABLE"