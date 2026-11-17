#!/usr/bin/env bash
# scripts/aws_start.sh [--refresh] -- start the NAT instance and the server, wait until the
# app answers through CloudFront, optionally refresh the demo data.
. "$(dirname "$0")/aws_lib.sh"
nat="$(tf_output nat_instance_id)"; server="$(tf_output server_instance_id)"
url="$(tf_output cloudfront_url 2>/dev/null || true)"   # empty until CloudFront exists (PRD R-35)
"$AWS" ec2 start-instances --instance-ids "$nat" >/dev/null
"$AWS" ec2 wait instance-running --instance-ids "$nat"
"$AWS" ec2 start-instances --instance-ids "$server" >/dev/null
"$AWS" ec2 wait instance-running --instance-ids "$server"
ready() {  # through CloudFront when it exists, else from inside the server through Traefik
  if [ -n "$url" ]; then curl -s -o /dev/null -w '%{http_code}' "$url/api/ready"
  else ssm_run "$server" "curl -s -o /dev/null -w '%{http_code}' http://localhost/api/ready" 2>/dev/null; fi
}
echo "instances running; waiting for ${url:-the server (no CloudFront yet)}/api/ready (up to 15 min)..."
for _ in $(seq 1 90); do
  if [ "$(ready)" = 200 ]; then
    if [ "${1:-}" = --refresh ]; then
      ssm_run "$server" "k3s kubectl -n smarthealth create job --from=cronjob/demo-refresh demo-refresh-\$(date +%s)"
      echo "demo refresh started (watch: scripts/aws_shell.sh, kubectl -n smarthealth get jobs)"
    fi
    if [ -z "$url" ]; then
      echo "SmartHealth is up inside the server; no CloudFront yet -- check it with scripts/aws_check.sh"
    else
      echo "SmartHealth is up: $url/  (tools login: reviewer / scripts/tf.sh output -raw tools_password)"
    fi
    echo "It stops itself in about 3 hours; scripts/aws_extend.sh adds 3 hours."
    exit 0
  fi
  sleep 10
done
echo "not ready after 15 minutes; last boot log:" >&2
ssm_run "$server" "journalctl -u smarthealth-boot -u k3s --no-pager -n 60" >&2
exit 1
