data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

# The NAT instance: Session Manager only.
resource "aws_iam_role" "ssm_only" {
  name               = "smarthealth-nat"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "ssm_only" {
  role       = aws_iam_role.ssm_only.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "ssm_only" {
  name = "smarthealth-nat"
  role = aws_iam_role.ssm_only.name
}

data "aws_caller_identity" "current" {}

# The server: Session Manager, plus reading its own parameters (spec section 4).
resource "aws_iam_role" "server" {
  name               = "smarthealth-server"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "server_ssm" {
  role       = aws_iam_role.server.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "server_parameters" {
  statement {
    actions = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
    resources = [
      "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter/smarthealth",
      "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter/smarthealth/*",
    ]
  }
}

resource "aws_iam_role_policy" "server_parameters" {
  name   = "read-smarthealth-parameters"
  role   = aws_iam_role.server.id
  policy = data.aws_iam_policy_document.server_parameters.json
}

resource "aws_iam_instance_profile" "server" {
  name = "smarthealth-server"
  role = aws_iam_role.server.name
}
