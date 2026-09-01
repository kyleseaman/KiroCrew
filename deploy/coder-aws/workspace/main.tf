terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    coder = {
      source  = "coder/coder"
      version = ">= 2.4"
    }
    cloudinit = {
      source  = "hashicorp/cloudinit"
      version = "~> 2.3"
    }
  }
}

variable "region" {
  description = "AWS region containing the control-plane subnet."
  type        = string
  default     = "us-east-2"
}

variable "subnet_id" {
  description = "NAT-free public subnet created by the control-plane stack."
  type        = string
}

variable "security_group_id" {
  description = "Outbound-only workspace security group."
  type        = string
}

variable "instance_profile_name" {
  description = "Workspace profile allowed to read only the two POC secrets."
  type        = string
}

variable "tailscale_auth_parameter" {
  description = "SSM SecureString name for a tagged ephemeral Tailscale auth key."
  type        = string
}

variable "kiro_api_key_parameter" {
  description = "SSM SecureString name for the workspace Kiro API key."
  type        = string
}

provider "aws" {
  region = var.region
}

data "coder_workspace" "me" {}
data "coder_workspace_owner" "me" {}

data "coder_parameter" "instance_type" {
  name         = "instance_type"
  display_name = "ARM compute size"
  description  = "Start small; move up only when dogfood measurements show CPU pressure."
  type         = "string"
  default      = "c8g.large"
  mutable      = true
  order        = 1

  option {
    name  = "C8g large - 2 vCPU / 4 GiB"
    value = "c8g.large"
  }

  option {
    name  = "C8g xlarge - 4 vCPU / 8 GiB"
    value = "c8g.xlarge"
  }

  option {
    name  = "M8g large - 2 vCPU / 8 GiB"
    value = "m8g.large"
  }
}

data "coder_parameter" "volume_gb" {
  name         = "volume_gb"
  display_name = "Persistent disk"
  description  = "Stopped workspaces retain this encrypted gp3 root volume."
  type         = "number"
  default      = 30
  mutable      = false
  order        = 2

  validation {
    min = 20
    max = 200
  }
}

data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

resource "coder_agent" "main" {
  arch               = "arm64"
  os                 = "linux"
  auth               = "aws-instance-identity"
  connection_timeout = 600

  metadata {
    key          = "cpu"
    display_name = "CPU Usage"
    interval     = 10
    timeout      = 2
    script       = "coder stat cpu"
  }

  metadata {
    key          = "memory"
    display_name = "Memory Usage"
    interval     = 10
    timeout      = 2
    script       = "coder stat mem"
  }
}

data "cloudinit_config" "workspace" {
  gzip          = true
  base64_encode = true

  part {
    content_type = "text/x-shellscript"
    filename     = "cloud-init.sh"
    content = templatefile("${path.module}/cloud-init.sh.tftpl", {
      region                       = var.region
      coder_init_script_b64        = base64encode(coder_agent.main.init_script)
      tailscale_auth_parameter     = var.tailscale_auth_parameter
      kiro_api_key_parameter       = var.kiro_api_key_parameter
      tailscale_workspace_hostname = "crew-${lower(data.coder_workspace.me.name)}"
    })
  }
}

resource "aws_instance" "workspace" {
  ami                         = data.aws_ssm_parameter.al2023_arm64.value
  instance_type               = data.coder_parameter.instance_type.value
  subnet_id                   = var.subnet_id
  vpc_security_group_ids      = [var.security_group_id]
  iam_instance_profile        = var.instance_profile_name
  associate_public_ip_address = true
  user_data_base64            = data.cloudinit_config.workspace.rendered
  user_data_replace_on_change = false

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    encrypted   = true
    volume_size = data.coder_parameter.volume_gb.value
    volume_type = "gp3"
  }

  tags = {
    Name              = "coder-${data.coder_workspace_owner.me.name}-${lower(data.coder_workspace.me.name)}"
    Coder_Provisioned = "true"
    Coder_Workspace   = data.coder_workspace.me.id
    KiroCrewManaged   = "true"
    Project           = "Kiro Crew Coder POC"
  }

  lifecycle {
    ignore_changes = [ami, user_data_base64]
  }
}

resource "aws_ec2_instance_state" "workspace" {
  instance_id = aws_instance.workspace.id
  state       = data.coder_workspace.me.transition == "start" ? "running" : "stopped"
}

resource "coder_metadata" "workspace" {
  resource_id = aws_instance.workspace.id

  item {
    key   = "instance_type"
    value = aws_instance.workspace.instance_type
  }

  item {
    key   = "architecture"
    value = "arm64 / Graviton"
  }

  item {
    key   = "persistent_disk_gb"
    value = tostring(data.coder_parameter.volume_gb.value)
  }
}
