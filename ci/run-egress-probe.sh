#!/usr/bin/env bash
# Local wrapper for the egress probe. Exists because passing AWS_* env vars
# through cmd.exe into `bash -c` mangles them; running a file avoids the issue.
set -euo pipefail
export AWS_PROFILE="${AWS_PROFILE:-sandbox}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-eu-west-1}"
export PROBE_SUBNET="${PROBE_SUBNET:?set PROBE_SUBNET}"
export PROBE_AMI="${PROBE_AMI:?set PROBE_AMI}"
cd "$(dirname "$0")/../.."
bash ci/egress-probe.sh