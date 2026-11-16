#!/usr/bin/env bash
# Delete the local k3d cluster and everything in it (volumes included).
set -euo pipefail
k3d cluster delete smarthealth
