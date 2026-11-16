#!/usr/bin/env bash
# scripts/aws_extend.sh -- push both auto-stops 3 hours (+15 min for NAT) from now.
. "$(dirname "$0")/aws_lib.sh"
ssm_run "$(tf_output server_instance_id)" "shutdown -c; shutdown -h +180 'smarthealth auto-stop'"
ssm_run "$(tf_output nat_instance_id)" "shutdown -c; shutdown -h +195 'smarthealth auto-stop'"
echo "auto-stop moved to 3 hours from now"
