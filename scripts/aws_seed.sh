#!/usr/bin/env bash
# scripts/aws_seed.sh -- first time only: seed the demo data once Argo CD has synced.
. "$(dirname "$0")/aws_lib.sh"
ssm_run "$(tf_output server_instance_id)" \
  "k3s kubectl -n smarthealth create job --from=cronjob/demo-seed demo-seed-\$(date +%s)"
echo "demo seed started; it takes several minutes (scripts/aws_shell.sh to watch)"
