#!/usr/bin/env bash
# scripts/aws_shell.sh -- a root shell on the server through Session Manager
# (needs the Session Manager plugin for the AWS CLI). kubectl there: `k3s kubectl`.
. "$(dirname "$0")/aws_lib.sh"
if ! command -v session-manager-plugin >/dev/null 2>&1 &&
   [ ! -x "/c/Program Files/Amazon/SessionManagerPlugin/bin/session-manager-plugin.exe" ]; then
  echo "the Session Manager plugin is not installed; get it from" >&2
  echo "https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html" >&2
  echo "(meanwhile scripts/aws_check.sh runs its checks on the server without it)" >&2
  exit 1
fi
target="$(tf_output server_instance_id)"
# `exec` runs programs, not functions: call the CLI binary itself, without path rewriting.
MSYS_NO_PATHCONV=1 exec "$AWS_BIN" ssm start-session --target "$target"
