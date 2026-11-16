output "cloudfront_url" {
  value = "https://${aws_cloudfront_distribution.main.domain_name}"
}

output "server_instance_id" {
  value = aws_instance.server.id
}

output "nat_instance_id" {
  value = aws_instance.nat.id
}

output "tools_password" {
  description = "Login for /grafana, /jaeger, /temporal, /mailpit, /rabbitmq, /prometheus, /argocd (user: reviewer)."
  value       = random_password.secret["tools-password"].result
  sensitive   = true
}

output "demo_password" {
  description = "Password of every demo login (README, Demo data)."
  value       = random_password.secret["demo-password"].result
  sensitive   = true
}

output "argocd_admin_password" {
  value     = random_password.secret["argocd-admin-password"].result
  sensitive = true
}
