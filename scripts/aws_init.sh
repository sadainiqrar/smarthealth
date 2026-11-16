#!/usr/bin/env bash
# scripts/aws_init.sh -- write the git-ignored backend config and `terraform init` the stack.
# Run after `scripts/tf.sh bootstrap apply` has created the state bucket.
. "$(dirname "$0")/aws_lib.sh"
bucket="$(terraform -chdir="$ROOT/deploy/terraform/bootstrap" output -raw state_bucket)"
printf 'bucket = "%s"\n' "$bucket" > "$ROOT/deploy/terraform/backend.hcl"
"$ROOT/scripts/tf.sh" init -backend-config=backend.hcl -input=false
