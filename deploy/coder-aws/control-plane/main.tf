terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

variable "region" {
  description = "AWS region for the control plane and workspaces."
  type        = string
  default     = "us-east-2"
}

variable "control_instance_type" {
  description = "Small always-on ARM instance for Coder and Kiro Crew."
  type        = string
  default     = "t4g.medium"
}

variable "tailscale_auth_parameter" {
  description = "SSM SecureString parameter containing a reusable tagged Tailscale auth key."
  type        = string
}

variable "tailscale_session_auth_parameter" {
  description = "SSM SecureString containing a tagged ephemeral Tailscale auth key for session workspaces."
  type        = string
}

variable "kiro_api_key_parameter" {
  description = "SSM SecureString parameter containing KIRO_API_KEY for workspaces."
  type        = string
}

variable "tailscale_hostname" {
  description = "MagicDNS hostname assigned to the control node."
  type        = string
  default     = "kirocrew-coder"
}

variable "tailnet_dns_name" {
  description = "Tailnet DNS suffix, for example example.ts.net."
  type        = string
}

variable "coder_version" {
  description = "Pinned Coder release installed on the ARM control node."
  type        = string
  default     = "2.34.7"
}

variable "coder_rpm_sha256" {
  description = "SHA-256 of the pinned Coder linux_arm64 RPM."
  type        = string
  default     = "ae0570b3457205235ecd1bb1838ef14090ea901717c3b7e7beba13c00375dc42"
}

variable "root_volume_gb" {
  description = "Persistent gp3 volume for Coder state and Crew data."
  type        = number
  default     = 30
}

provider "aws" {
  region = var.region
}

data "aws_partition" "current" {}
data "aws_caller_identity" "current" {}

data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

locals {
  name = "kirocrew-coder-poc"
  parameter_arns = {
    tailscale = format(
      "arn:%s:ssm:%s:%s:parameter/%s",
      data.aws_partition.current.partition,
      var.region,
      data.aws_caller_identity.current.account_id,
      trimprefix(var.tailscale_auth_parameter, "/"),
    )
    session_tailscale = format(
      "arn:%s:ssm:%s:%s:parameter/%s",
      data.aws_partition.current.partition,
      var.region,
      data.aws_caller_identity.current.account_id,
      trimprefix(var.tailscale_session_auth_parameter, "/"),
    )
    kiro = format(
      "arn:%s:ssm:%s:%s:parameter/%s",
      data.aws_partition.current.partition,
      var.region,
      data.aws_caller_identity.current.account_id,
      trimprefix(var.kiro_api_key_parameter, "/"),
    )
    al2023 = format(
      "arn:%s:ssm:%s::parameter/aws/service/ami-amazon-linux-latest/*",
      data.aws_partition.current.partition,
      var.region,
    )
  }
}

resource "aws_vpc" "poc" {
  cidr_block           = "10.84.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = { Name = local.name }
}

resource "aws_internet_gateway" "poc" {
  vpc_id = aws_vpc.poc.id
  tags   = { Name = local.name }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.poc.id
  cidr_block              = "10.84.1.0/24"
  map_public_ip_on_launch = true

  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.poc.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.poc.id
  }

  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# Both nodes initiate their Tailscale and Coder connections outbound. Keeping
# the groups ingress-free means the public IPv4 addresses are routing devices,
# not public services, and avoids a NAT Gateway's fixed hourly charge.
resource "aws_security_group" "control" {
  name        = "${local.name}-control"
  description = "Outbound-only control plane"
  vpc_id      = aws_vpc.poc.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "workspace" {
  name        = "${local.name}-workspace"
  description = "Outbound-only Coder workspace"
  vpc_id      = aws_vpc.poc.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "gateway" {
  name        = "${local.name}-gateway"
  description = "Outbound-only Kiro Crew gateway workspace"
  vpc_id      = aws_vpc.poc.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_iam_role" "workspace" {
  name = "${local.name}-workspace"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "workspace_ssm" {
  role       = aws_iam_role.workspace.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "workspace_secrets" {
  role = aws_iam_role.workspace.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = [local.parameter_arns.session_tailscale, local.parameter_arns.kiro]
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "kms:ViaService" = "ssm.${var.region}.amazonaws.com"
          }
          StringLike = {
            "kms:EncryptionContext:PARAMETER_ARN" = [
              local.parameter_arns.session_tailscale,
              local.parameter_arns.kiro,
            ]
          }
        }
      },
    ]
  })
}

resource "aws_iam_instance_profile" "workspace" {
  name = "${local.name}-workspace"
  role = aws_iam_role.workspace.name
}

# Gateway and session profiles are separate identities even though the POC
# currently uses the same two bootstrap parameters. That keeps later tightening
# additive and prevents a session template from ever inheriting Coder authority.
resource "aws_iam_role" "gateway" {
  name = "${local.name}-gateway"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "gateway_ssm" {
  role       = aws_iam_role.gateway.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "gateway_secrets" {
  role = aws_iam_role.gateway.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = [local.parameter_arns.tailscale, local.parameter_arns.kiro]
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "kms:ViaService" = "ssm.${var.region}.amazonaws.com"
          }
          StringLike = {
            "kms:EncryptionContext:PARAMETER_ARN" = [
              local.parameter_arns.tailscale,
              local.parameter_arns.kiro,
            ]
          }
        }
      },
    ]
  })
}

resource "aws_iam_instance_profile" "gateway" {
  name = "${local.name}-gateway"
  role = aws_iam_role.gateway.name
}

resource "aws_iam_role" "session" {
  name = "${local.name}-session"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "session_ssm" {
  role       = aws_iam_role.session.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "session_secrets" {
  role = aws_iam_role.session.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = [local.parameter_arns.session_tailscale, local.parameter_arns.kiro]
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "kms:ViaService" = "ssm.${var.region}.amazonaws.com"
          }
          StringLike = {
            "kms:EncryptionContext:PARAMETER_ARN" = [
              local.parameter_arns.session_tailscale,
              local.parameter_arns.kiro,
            ]
          }
        }
      },
    ]
  })
}

resource "aws_iam_instance_profile" "session" {
  name = "${local.name}-session"
  role = aws_iam_role.session.name
}

resource "aws_iam_role" "control" {
  name = "${local.name}-control"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "control_ssm" {
  role       = aws_iam_role.control.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "control" {
  role = aws_iam_role.control.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["ssm:GetParameter"]
        Resource = [
          local.parameter_arns.tailscale,
          local.parameter_arns.al2023,
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "kms:ViaService" = "ssm.${var.region}.amazonaws.com"
          }
          StringLike = {
            "kms:EncryptionContext:PARAMETER_ARN" = local.parameter_arns.tailscale
          }
        }
      },
      {
        Sid    = "DescribeEc2"
        Effect = "Allow"
        Action = [
          "ec2:DescribeImages",
          "ec2:DescribeInstanceAttribute",
          "ec2:DescribeInstanceCreditSpecifications",
          "ec2:DescribeInstances",
          "ec2:DescribeInstanceStatus",
          "ec2:DescribeInstanceTypes",
          "ec2:DescribeNetworkInterfaces",
          "ec2:DescribeRegions",
          "ec2:DescribeSubnets",
          "ec2:DescribeSecurityGroups",
          "ec2:DescribeTags",
          "ec2:DescribeVolumes",
          "ec2:DescribeVolumesModifications",
        ]
        Resource = "*"
      },
      {
        Sid    = "RunCoderInstanceDependencies"
        Effect = "Allow"
        Action = "ec2:RunInstances"
        Resource = [
          format(
            "arn:%s:ec2:%s::image/*",
            data.aws_partition.current.partition,
            var.region,
          ),
          format(
            "arn:%s:ec2:%s::snapshot/*",
            data.aws_partition.current.partition,
            var.region,
          ),
          aws_subnet.public.arn,
          aws_security_group.workspace.arn,
          aws_security_group.gateway.arn,
          format(
            "arn:%s:ec2:%s:%s:network-interface/*",
            data.aws_partition.current.partition,
            var.region,
            data.aws_caller_identity.current.account_id,
          ),
          format(
            "arn:%s:ec2:%s:%s:volume/*",
            data.aws_partition.current.partition,
            var.region,
            data.aws_caller_identity.current.account_id,
          ),
        ]
      },
      {
        Sid    = "RunCoderInstances"
        Effect = "Allow"
        Action = "ec2:RunInstances"
        Resource = format(
          "arn:%s:ec2:%s:%s:instance/*",
          data.aws_partition.current.partition,
          var.region,
          data.aws_caller_identity.current.account_id,
        )
        Condition = {
          StringEquals = {
            "aws:RequestTag/KiroCrewManaged" = "true"
          }
        }
      },
      {
        Sid    = "TagNewCoderResources"
        Effect = "Allow"
        Action = "ec2:CreateTags"
        Resource = [
          format(
            "arn:%s:ec2:%s:%s:instance/*",
            data.aws_partition.current.partition,
            var.region,
            data.aws_caller_identity.current.account_id,
          ),
          format(
            "arn:%s:ec2:%s:%s:volume/*",
            data.aws_partition.current.partition,
            var.region,
            data.aws_caller_identity.current.account_id,
          ),
        ]
        Condition = {
          StringEquals = {
            "ec2:CreateAction"               = "RunInstances"
            "aws:RequestTag/KiroCrewManaged" = "true"
          }
        }
      },
      {
        Sid    = "MutateCoderInstances"
        Effect = "Allow"
        Action = [
          "ec2:CreateTags",
          "ec2:DeleteTags",
          "ec2:StartInstances",
          "ec2:StopInstances",
          "ec2:TerminateInstances",
        ]
        Resource = format(
          "arn:%s:ec2:%s:%s:instance/*",
          data.aws_partition.current.partition,
          var.region,
          data.aws_caller_identity.current.account_id,
        )
        Condition = {
          StringEquals = {
            "ec2:ResourceTag/KiroCrewManaged" = "true"
          }
        }
      },
      {
        Effect = "Allow"
        Action = "iam:PassRole"
        Resource = [
          aws_iam_role.workspace.arn,
          aws_iam_role.gateway.arn,
          aws_iam_role.session.arn,
        ]
      },
      {
        Effect = "Allow"
        Action = "iam:GetInstanceProfile"
        Resource = [
          aws_iam_instance_profile.workspace.arn,
          aws_iam_instance_profile.gateway.arn,
          aws_iam_instance_profile.session.arn,
        ]
      },
    ]
  })
}

resource "aws_iam_instance_profile" "control" {
  name = "${local.name}-control"
  role = aws_iam_role.control.name
}

resource "aws_instance" "control" {
  ami                         = data.aws_ssm_parameter.al2023_arm64.value
  instance_type               = var.control_instance_type
  subnet_id                   = aws_subnet.public.id
  vpc_security_group_ids      = [aws_security_group.control.id]
  iam_instance_profile        = aws_iam_instance_profile.control.name
  associate_public_ip_address = true
  user_data_replace_on_change = false

  user_data = templatefile("${path.module}/cloud-init.sh.tftpl", {
    region                   = var.region
    tailscale_auth_parameter = var.tailscale_auth_parameter
    tailscale_hostname       = var.tailscale_hostname
    tailnet_dns_name         = var.tailnet_dns_name
    coder_version            = var.coder_version
    coder_rpm_sha256         = var.coder_rpm_sha256
  })

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    encrypted   = true
    volume_size = var.root_volume_gb
    volume_type = "gp3"
  }

  tags = {
    Name    = local.name
    Project = "Kiro Crew Coder POC"
  }

  lifecycle {
    ignore_changes = [ami, user_data]
  }
}

output "coder_url" {
  value = "https://${var.tailscale_hostname}.${var.tailnet_dns_name}"
}

output "gateway_template_values" {
  value = {
    region                   = var.region
    subnet_id                = aws_subnet.public.id
    security_group_id        = aws_security_group.gateway.id
    instance_profile_name    = aws_iam_instance_profile.gateway.name
    tailscale_auth_parameter = var.tailscale_auth_parameter
    kiro_api_key_parameter   = var.kiro_api_key_parameter
    tailnet_dns_name         = var.tailnet_dns_name
  }
}

output "session_template_values" {
  value = {
    region                   = var.region
    subnet_id                = aws_subnet.public.id
    security_group_id        = aws_security_group.workspace.id
    instance_profile_name    = aws_iam_instance_profile.session.name
    tailscale_auth_parameter = var.tailscale_session_auth_parameter
    kiro_api_key_parameter   = var.kiro_api_key_parameter
  }
}

# Compatibility alias for existing POC automation.
output "workspace_template_values" {
  value = {
    region                   = var.region
    subnet_id                = aws_subnet.public.id
    security_group_id        = aws_security_group.workspace.id
    instance_profile_name    = aws_iam_instance_profile.session.name
    tailscale_auth_parameter = var.tailscale_session_auth_parameter
    kiro_api_key_parameter   = var.kiro_api_key_parameter
  }
}
