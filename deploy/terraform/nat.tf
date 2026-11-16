# Outbound internet for the private server (spec A-1): a t4g.micro doing NAT, instead of a
# managed NAT Gateway (~$35/month idle). Started and stopped with the server.
data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

resource "aws_security_group" "nat" {
  name        = "smarthealth-nat"
  description = "NAT instance: anything from the private subnet, nothing from the internet"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "traffic to forward, from the private subnet"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [aws_subnet.private.cidr_block]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_instance" "nat" {
  ami                                  = data.aws_ssm_parameter.al2023_arm64.value
  instance_type                        = var.nat_instance_type
  subnet_id                            = aws_subnet.public.id
  vpc_security_group_ids               = [aws_security_group.nat.id]
  source_dest_check                    = false
  iam_instance_profile                 = aws_iam_instance_profile.ssm_only.name
  instance_initiated_shutdown_behavior = "stop"
  user_data = templatefile("${path.module}/templates/nat-user-data.sh.tftpl", {
    private_cidr     = aws_subnet.private.cidr_block
    autostop_minutes = var.autostop_minutes + 15
  })

  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 8
    encrypted   = true
  }

  lifecycle {
    ignore_changes = [ami]
  }

  tags = { Name = "smarthealth-nat" }
}
