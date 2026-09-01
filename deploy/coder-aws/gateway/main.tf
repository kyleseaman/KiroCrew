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
  description = "AWS region containing the Coder control-plane subnet."
  type        = string
  default     = "us-east-2"
}

variable "subnet_id" {
  description = "NAT-free public subnet created by the control-plane stack."
  type        = string
}

variable "security_group_id" {
  description = "Outbound-only gateway security group."
  type        = string
}

variable "instance_profile_name" {
  description = "Gateway profile allowed to read its two bootstrap secrets."
  type        = string
}

variable "tailscale_auth_parameter" {
  description = "SSM SecureString name for the gateway Tailscale auth key."
  type        = string
}

variable "kiro_api_key_parameter" {
  description = "SSM SecureString name for the gateway Kiro API key."
  type        = string
}

variable "tailnet_dns_name" {
  description = "Tailnet DNS suffix, for example example.ts.net."
  type        = string
}

variable "coder_version" {
  description = "Pinned Coder CLI release installed on the gateway."
  type        = string
  default     = "2.34.7"
}

variable "coder_rpm_sha256" {
  description = "SHA-256 of the pinned Coder linux_arm64 RPM."
  type        = string
  default     = "ae0570b3457205235ecd1bb1838ef14090ea901717c3b7e7beba13c00375dc42"
}

provider "aws" {
  region = var.region
}

data "coder_workspace" "me" {}
data "coder_workspace_owner" "me" {}

data "coder_parameter" "instance_type" {
  name         = "instance_type"
  display_name = "Gateway compute size"
  description  = "The gateway is always on; start small and resize if MCP or memory pressure requires it."
  type         = "string"
  default      = "t4g.small"
  mutable      = true
  order        = 1

  option {
    name  = "T4g small - 2 vCPU / 2 GiB"
    value = "t4g.small"
  }

  option {
    name  = "T4g medium - 2 vCPU / 4 GiB"
    value = "t4g.medium"
  }
}

data "coder_parameter" "volume_gb" {
  name         = "volume_gb"
  display_name = "Persistent gateway disk"
  description  = "Stores Kiro Crew history, memory, OAuth state, policy, and the encrypted vault."
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

resource "coder_app" "kiro_crew" {
  agent_id     = coder_agent.main.id
  slug         = "kiro-crew"
  display_name = "Kiro Crew"
  url          = "http://localhost:8443"
  subdomain    = false
  share        = "owner"

  healthcheck {
    url       = "http://localhost:8443/api/health"
    interval  = 10
    threshold = 6
  }
}

data "cloudinit_config" "gateway" {
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
      tailscale_gateway_hostname   = lower(data.coder_workspace.me.name)
      gateway_url                  = "https://${lower(data.coder_workspace.me.name)}.${var.tailnet_dns_name}:8443"
      coder_version                = var.coder_version
      coder_rpm_sha256             = var.coder_rpm_sha256
      kirocrew_admin_launcher_b64  = filebase64("${path.module}/kirocrew-admin")
      kirocrew_wheel_installer_b64 = filebase64("${path.module}/kirocrew-install-wheel")
    })
  }
}

resource "aws_instance" "gateway" {
  ami                         = data.aws_ssm_parameter.al2023_arm64.value
  instance_type               = data.coder_parameter.instance_type.value
  subnet_id                   = var.subnet_id
  vpc_security_group_ids      = [var.security_group_id]
  iam_instance_profile        = var.instance_profile_name
  associate_public_ip_address = true
  user_data_base64            = data.cloudinit_config.gateway.rendered
  user_data_replace_on_change = false

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    delete_on_termination = false
    encrypted             = true
    volume_size           = data.coder_parameter.volume_gb.value
    volume_type           = "gp3"
  }

  tags = {
    Name              = "coder-gateway-${data.coder_workspace_owner.me.name}"
    Coder_Provisioned = "true"
    Coder_Workspace   = data.coder_workspace.me.id
    KiroCrewManaged   = "true"
    Project           = "Kiro Crew Gateway"
  }

  lifecycle {
    ignore_changes = [
      ami,
      associate_public_ip_address,
      user_data_base64,
    ]
  }
}

resource "aws_ec2_instance_state" "gateway" {
  instance_id = aws_instance.gateway.id
  state       = data.coder_workspace.me.transition == "start" ? "running" : "stopped"
}

resource "coder_metadata" "gateway" {
  resource_id = aws_instance.gateway.id

  item {
    key   = "role"
    value = "Kiro Crew gateway"
  }

  item {
    key   = "instance_type"
    value = aws_instance.gateway.instance_type
  }

  item {
    key   = "gateway_url"
    value = "https://${lower(data.coder_workspace.me.name)}.${var.tailnet_dns_name}:8443"
  }
}
