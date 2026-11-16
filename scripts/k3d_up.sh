#!/usr/bin/env bash
# A local stand-in for the AWS server: one k3s node capped at 8 GB, SmartHealth installed.
# Stop the compose stacks first -- Docker Desktop needs ~10 GB free for this.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! k3d cluster list smarthealth >/dev/null 2>&1; then
  # The API on 127.0.0.1: on Docker Desktop k3d otherwise writes host.docker.internal into
  # the kubeconfig, which resolves to the LAN address and can be firewalled off.
  k3d cluster create smarthealth --servers 1 --agents 0 --servers-memory 8G \
    --api-port 127.0.0.1:6550 -p "8080:80@loadbalancer" --wait
fi
kubectl config use-context k3d-smarthealth
kubectl create namespace smarthealth --dry-run=client -o yaml | kubectl apply -f -

scripts/push_images.sh --local
python scripts/sync_chart_files.py --check
helm dependency update deploy/helm/smarthealth >/dev/null
helm upgrade --install smarthealth deploy/helm/smarthealth -n smarthealth \
  -f deploy/helm/smarthealth/values-local.yaml --wait --timeout 15m
echo "SmartHealth is up: http://localhost:8080/  (smoke: python scripts/k3d_smoke.py)"
