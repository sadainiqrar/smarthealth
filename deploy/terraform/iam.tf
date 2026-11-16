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
