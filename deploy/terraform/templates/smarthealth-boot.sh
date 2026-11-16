#!/bin/bash
# deploy/terraform/templates/smarthealth-boot.sh -- runs on EVERY boot of the server
# (smarthealth-boot.service, after k3s). Copied verbatim by user-data; reads its settings
# from /etc/smarthealth/boot.env. Idempotent.
#  1. schedule the auto-stop;  2. wait for the API server;
#  3. write the Secrets from SSM;  4. set Argo CD's admin password;
#  5. apply the Argo CD Application and the /argocd ingress once Argo CD's CRDs exist.
set -euo pipefail
# shellcheck disable=SC1091
. /etc/smarthealth/boot.env   # REGION, AUTOSTOP_MINUTES
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl() { /usr/local/bin/k3s kubectl "$@"; }

shutdown -h "+$AUTOSTOP_MINUTES" "smarthealth auto-stop" || true

until kubectl get --raw=/readyz >/dev/null 2>&1; do sleep 3; done

params=""
until params="$(aws ssm get-parameters-by-path --region "$REGION" --path /smarthealth \
                  --with-decryption --output json 2>/dev/null)"; do
  echo "waiting for SSM..."; sleep 10
done
value() {  # value <name> -> the parameter's value, or "" when it does not exist
  printf '%s' "$params" | python3 -c '
import json, sys
name = "/smarthealth/" + sys.argv[1]
for p in json.load(sys.stdin)["Parameters"]:
    if p["Name"] == name:
        print(p["Value"], end="")' "$1"
}

for ns in smarthealth argocd; do
  kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f -
done

kubectl -n smarthealth create secret generic smarthealth-secrets \
  --from-literal=postgres-password="$(value postgres-password)" \
  --from-literal=knowledge-db-password="$(value knowledge-db-password)" \
  --from-literal=assistant-db-password="$(value assistant-db-password)" \
  --from-literal=rabbitmq-password="$(value rabbitmq-password)" \
  --from-literal=grafana-admin-password="$(value grafana-admin-password)" \
  --from-literal=demo-password="$(value demo-password)" \
  --from-literal=jwt-private-key="$(value jwt-private-key)" \
  --from-literal=llm-api-key="$(value llm-api-key)" \
  --dry-run=client -o yaml | kubectl apply -f -

users="$(htpasswd -nbB reviewer "$(value tools-password)" | head -n1)"
for ns in smarthealth argocd; do
  kubectl -n "$ns" create secret generic smarthealth-tools-auth \
    --from-literal=users="$users" --dry-run=client -o yaml | kubectl apply -f -
done

until kubectl get crd applications.argoproj.io >/dev/null 2>&1; do
  echo "waiting for Argo CD..."; sleep 5
done
until kubectl -n argocd get secret argocd-secret >/dev/null 2>&1; do sleep 5; done
hash="$(htpasswd -nbBC 10 "" "$(value argocd-admin-password)" | tr -d ':\n')"
kubectl -n argocd patch secret argocd-secret --type merge -p \
  "{\"stringData\":{\"admin.password\":\"$hash\",\"admin.passwordMtime\":\"$(date -u +%FT%TZ)\"}}"

kubectl apply -f /etc/smarthealth/argocd-extra.yaml
echo "smarthealth boot: done"
