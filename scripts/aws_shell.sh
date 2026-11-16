#!/usr/bin/env bash
# scripts/aws_shell.sh -- a root shell on the server through Session Manager
# (needs the Session Manager plugin for the AWS CLI). kubectl there: `k3s kubectl`.
. "$(dirname "$0")/aws_lib.sh"
exec "$AWS" ssm start-session --target "$(tf_output server_instance_id)"
