#!/usr/bin/env bash
# scripts/aws_secrets.sh -- put the two secrets Terraform must not hold (spec section 4):
# a fresh JWT signing key, and the Groq key from the git-ignored .env (if present).
# Re-run to rotate; then reboot the server (or wait for the next start).
. "$(dirname "$0")/aws_lib.sh"
key="$(cd "$ROOT" && "$PY" -m app.cli generate-signing-key)"
"$AWS" ssm put-parameter --name /smarthealth/jwt-private-key --type SecureString \
  --value "$key" --overwrite >/dev/null
echo "jwt-private-key: set"
llm="$(grep -E '^ASSISTANT_LLM_API_KEY=' "$ROOT/.env" 2>/dev/null | head -n1 | cut -d= -f2- || true)"
if [ -n "$llm" ]; then
  "$AWS" ssm put-parameter --name /smarthealth/llm-api-key --type SecureString \
    --value "$llm" --overwrite >/dev/null
  echo "llm-api-key: set from .env"
else
  echo "llm-api-key: not in .env -- the assistant runs offline"
fi
