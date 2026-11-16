data "aws_ssm_parameter" "ubuntu_2404" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}

data "aws_ec2_managed_prefix_list" "cloudfront" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

resource "aws_security_group" "server" {
  name        = "smarthealth-server"
  description = "k3s server: HTTP from CloudFront only"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "Traefik, from CloudFront (VPC origin)"
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    prefix_list_ids = [data.aws_ec2_managed_prefix_list.cloudfront.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_instance" "server" {
  ami                                  = data.aws_ssm_parameter.ubuntu_2404.value
  instance_type                        = var.server_instance_type
  subnet_id                            = aws_subnet.private.id
  vpc_security_group_ids               = [aws_security_group.server.id]
  iam_instance_profile                 = aws_iam_instance_profile.server.name
  instance_initiated_shutdown_behavior = "stop"
  user_data = templatefile("${path.module}/templates/server-user-data.sh.tftpl", {
    region               = var.region
    autostop_minutes     = var.autostop_minutes
    k3s_version          = var.k3s_version
    argocd_chart_version = var.argocd_chart_version
    repo_url             = var.repo_url
    repo_revision        = var.repo_revision
    boot_script          = file("${path.module}/templates/smarthealth-boot.sh")
  })

  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_gb
    encrypted             = true
    delete_on_termination = false # all data (local-path volumes) lives here
  }

  # The NAT route must exist before first boot downloads k3s.
  depends_on = [aws_route_table_association.private, aws_instance.nat]

  lifecycle {
    ignore_changes = [ami, user_data]
  }

  tags = { Name = "smarthealth-server" }
}
