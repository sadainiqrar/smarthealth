#!/usr/bin/env bash
# scripts/aws_start.sh [--refresh] -- start the NAT instance and the server, wait until the
# app answers through CloudFront, optionally refresh the demo data.
. "$(dirname "$0")/aws_lib.sh"
nat="$(tf_output nat_instance_id)"; server="$(tf_output server_instance_id)"
url="$(tf_output cloudfront_url)"
"$AWS" ec2 start-instances --instance-ids "$nat" >/dev/null
"$AWS" ec2 wait instance-running --instance-ids "$nat"
"$AWS" ec2 start-instances --instance-ids "$server" >/dev/null
"$AWS" ec2 wait instance-running --instance-ids "$server"
echo "instances running; waiting for $url/api/ready (up to 15 min)..."
for _ in $(seq 1 90); do
  if [ "$(curl -s -o /dev/null -w '%{http_code}' "$url/api/ready")" = 200 ]; then
    if [ "${1:-}" = --refresh ]; then
      ssm_run "$server" "k3s kubectl -n smarthealth create job --from=cronjob/demo-refresh demo-refresh-\$(date +%s)"
      echo "demo refresh started (watch: scripts/aws_shell.sh, kubectl -n smarthealth get jobs)"
    fi
    echo "SmartHealth is up: $url/  (tools login: reviewer / scripts/tf.sh output -raw tools_password)"
    echo "It stops itself in about 3 hours; scripts/aws_extend.sh adds 3 hours."
    exit 0
  fi
  sleep 10
done
echo "not ready after 15 minutes; last boot log:" >&2
ssm_run "$server" "journalctl -u smarthealth-boot -u k3s --no-pager -n 60" >&2
exit 1
