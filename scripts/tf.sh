#!/usr/bin/env bash
# scripts/tf.sh -- terraform with credentials from the `smarthealth` profile.
# `aws login` credentials live in a cache the Terraform provider may not read, so export
# them as environment variables first. Usage: scripts/tf.sh [bootstrap] <terraform args>
. "$(dirname "$0")/aws_lib.sh"
eval "$("$AWS" configure export-credentials --format env)"
dir="$ROOT/deploy/terraform"
if [ "${1:-}" = bootstrap ]; then dir="$dir/bootstrap"; shift; fi
exec terraform -chdir="$dir" "$@"
