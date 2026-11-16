# Generated secrets (spec section 4). The JWT signing key and the Groq key are NOT here:
# scripts/aws_secrets.sh puts them, so they never enter Terraform state.
locals {
  generated = toset([
    "postgres-password", "knowledge-db-password", "assistant-db-password",
    "rabbitmq-password", "grafana-admin-password", "demo-password", "tools-password",
    "argocd-admin-password",
  ])
}

resource "random_password" "secret" {
  for_each = local.generated
  length   = 24
  special  = false
}

resource "aws_ssm_parameter" "secret" {
  for_each = local.generated
  name     = "/smarthealth/${each.key}"
  type     = "SecureString"
  value    = random_password.secret[each.key].result
}
