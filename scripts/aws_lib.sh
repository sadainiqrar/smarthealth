#!/usr/bin/env bash
# scripts/aws_lib.sh -- sourced by the scripts/aws_*.sh helpers.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export AWS_PROFILE="${AWS_PROFILE:-smarthealth}"
export AWS_REGION="${AWS_REGION:-ap-southeast-2}"
export PYTHONIOENCODING=utf-8   # the CLI fails on a non-UTF-8 Windows console otherwise

if command -v aws >/dev/null 2>&1; then
  AWS=aws
elif [ -x "$HOME/AppData/Local/Programs/Amazon/AWSCLIV2/aws.exe" ]; then
  AWS="$HOME/AppData/Local/Programs/Amazon/AWSCLIV2/aws.exe"
else
  echo "AWS CLI v2 not found (install it or put it on PATH)" >&2; exit 1
fi
# Git Bash rewrites arguments that start with `/` (an SSM name like /smarthealth/x) into
# Windows paths before a Windows exe sees them. No aws call here takes a local path, so
# turn that off for aws only -- terraform -chdir and python still need it.
AWS_BIN="$AWS"
aws_cli() { MSYS_NO_PATHCONV=1 "$AWS_BIN" "$@"; }
AWS=aws_cli

PY=python
command -v python >/dev/null 2>&1 || PY=python3

# The stack state is in S3, so outputs need the exported credentials: go through tf.sh.
tf_output() { "$ROOT/scripts/tf.sh" output -raw "$1"; }

# ssm_run <instance-id> <shell command> -> runs it as root on the instance, prints stdout.
# The parameters are built as JSON by python, so quotes in the command cannot break them.
ssm_run() {
  local id params
  params="$("$PY" -c 'import json,sys; print(json.dumps({"commands": [sys.argv[1]]}))' "$2")"
  id="$("$AWS" ssm send-command --instance-ids "$1" --document-name AWS-RunShellScript \
        --parameters "$params" --query Command.CommandId --output text)"
  "$AWS" ssm wait command-executed --command-id "$id" --instance-id "$1" || true
  "$AWS" ssm get-command-invocation --command-id "$id" --instance-id "$1" \
    --query StandardOutputContent --output text
}
