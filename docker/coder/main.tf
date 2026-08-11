terraform {
  required_providers {
    coder = {
      source  = "coder/coder"
      version = "~> 2.1"
    }
    docker = {
      source  = "kreuzwerker/docker"
      version = "~> 3.0"
    }
  }
}

# coderd has /var/run/docker.sock bind-mounted, so the provider talks to the
# HOST daemon and every workspace is a sibling container of coderd itself.
provider "docker" {}

# ---------------------------------------------------------------------------
# Template variables (set at push time: coder templates push --var k=v)
# ---------------------------------------------------------------------------

variable "workspace_image" {
  type        = string
  description = "Image used for workspace containers."
  default     = "kirocrew-workspace:poc"
}

variable "docker_network" {
  type        = string
  description = <<-EOT
    User-defined Docker network shared with the coderd container. A
    user-defined network is required because the default bridge has no
    embedded DNS, and coderd publishes its port on 127.0.0.1 only -- so
    neither "localhost" nor host.docker.internal can reach it from a
    sibling container. Attach coderd once with:
      docker network create coder-poc && docker network connect coder-poc coderd
  EOT
  default     = "coder-poc"
}

variable "coderd_internal_url" {
  type        = string
  description = "URL the agent uses to reach coderd from inside the workspace network."
  default     = "http://coderd:3000"
}

variable "kiro_api_key" {
  type        = string
  description = "Template-wide default for KIRO_API_KEY. Never hardcoded; supply with --var."
  sensitive   = true
  default     = ""
}

# ---------------------------------------------------------------------------
# Coder data sources
# ---------------------------------------------------------------------------

data "coder_workspace" "me" {}
data "coder_workspace_owner" "me" {}

data "docker_network" "workspace" {
  name = var.docker_network
}

# ---------------------------------------------------------------------------
# instance_size -- the point of this template: per-session right-sizing.
#
# The option VALUE is a plain key; local.sizes maps it to real cgroup limits
# so the enforced numbers live in one auditable place.
# ---------------------------------------------------------------------------

data "coder_parameter" "instance_size" {
  name         = "instance_size"
  display_name = "Instance size"
  description  = "CPU and memory ceiling enforced on the workspace container."
  type         = "string"
  default      = "small"
  icon         = "/icon/memory.svg"
  mutable      = true
  order        = 1

  option {
    name        = "Small - 2 vCPU / 2 GiB"
    description = "Editing, reviews, light test runs."
    value       = "small"
  }

  option {
    name        = "Medium - 4 vCPU / 4 GiB"
    description = "Full backend test suite."
    value       = "medium"
  }

  option {
    name        = "Build - 8 vCPU / 8 GiB"
    description = "Frontend bundles and parallel builds."
    value       = "build"
  }
}

data "coder_parameter" "kiro_api_key" {
  name         = "kiro_api_key"
  display_name = "KIRO_API_KEY"
  description  = "Per-workspace override. Leave blank to inherit the template default."
  type         = "string"
  default      = ""
  mutable      = true
  order        = 2
}

locals {
  # cpus is the HARD ceiling. kreuzwerker/docker v3.x docker_container has NO
  # nano_cpus argument (that one belongs to docker_service/Swarm); `cpus` is
  # the string equivalent of `docker run --cpus` and lands in HostConfig as
  # NanoCpus. cpu_shares is only a relative weight under contention, so it is
  # scaled alongside but is not what enforces the ceiling. memory/memory_swap
  # are MiB here; setting them equal forbids swap defeating the memory limit.
  sizes = {
    small = {
      cpus       = 2
      cpus_str   = "2.0"
      memory_mib = 2048
      cpu_shares = 512
    }
    medium = {
      cpus       = 4
      cpus_str   = "4.0"
      memory_mib = 4096
      cpu_shares = 1024
    }
    build = {
      cpus       = 8
      cpus_str   = "8.0"
      memory_mib = 8192
      cpu_shares = 2048
    }
  }

  size = local.sizes[data.coder_parameter.instance_size.value]

  # Per-workspace override wins; otherwise inherit the template variable.
  # No value is ever hardcoded here.
  kiro_api_key = (
    data.coder_parameter.kiro_api_key.value != ""
    ? data.coder_parameter.kiro_api_key.value
    : var.kiro_api_key
  )
}

# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

resource "coder_agent" "main" {
  os                     = "linux"
  arch                   = "amd64"
  startup_script_behavior = "non-blocking"

  # Deliberately cheap: records the limits it actually landed with, nothing else.
  startup_script = <<-EOT
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p "$HOME/.kirocrew-poc"
    {
      echo "size_option=${data.coder_parameter.instance_size.value}"
      echo "requested_cpus=${local.size.cpus}"
      echo "requested_memory_mib=${local.size.memory_mib}"
      echo "nproc=$(nproc)"
      if [ -r /sys/fs/cgroup/cpu/cpu.cfs_quota_us ]; then
        echo "cpu.cfs_quota_us=$(cat /sys/fs/cgroup/cpu/cpu.cfs_quota_us)"
        echo "cpu.cfs_period_us=$(cat /sys/fs/cgroup/cpu/cpu.cfs_period_us)"
        echo "cpu.shares=$(cat /sys/fs/cgroup/cpu/cpu.shares)"
        echo "memory.limit_in_bytes=$(cat /sys/fs/cgroup/memory/memory.limit_in_bytes)"
      elif [ -r /sys/fs/cgroup/cpu.max ]; then
        echo "cpu.max=$(cat /sys/fs/cgroup/cpu.max)"
        echo "memory.max=$(cat /sys/fs/cgroup/memory.max)"
      fi
    } > "$HOME/.kirocrew-poc/boot.txt"
  EOT

  env = {
    KIRO_API_KEY            = local.kiro_api_key
    KIROCREW_INSTANCE_SIZE  = data.coder_parameter.instance_size.value
  }

  # cgroup v1 paths first, v2 as fallback, so the UI shows the real ceiling.
  metadata {
    display_name = "CPU limit"
    key          = "cpu_limit"
    interval     = 60
    timeout      = 5
    script       = <<-EOT
      if [ -r /sys/fs/cgroup/cpu/cpu.cfs_quota_us ]; then
        q=$(cat /sys/fs/cgroup/cpu/cpu.cfs_quota_us)
        p=$(cat /sys/fs/cgroup/cpu/cpu.cfs_period_us)
        [ "$q" = "-1" ] && echo "unlimited" || echo "$((q / p)) vCPU"
      else
        cut -d' ' -f1 /sys/fs/cgroup/cpu.max
      fi
    EOT
  }

  metadata {
    display_name = "Memory limit"
    key          = "mem_limit"
    interval     = 60
    timeout      = 5
    script       = <<-EOT
      if [ -r /sys/fs/cgroup/memory/memory.limit_in_bytes ]; then
        b=$(cat /sys/fs/cgroup/memory/memory.limit_in_bytes)
      else
        b=$(cat /sys/fs/cgroup/memory.max)
      fi
      echo "$((b / 1024 / 1024)) MiB"
    EOT
  }
}

# ---------------------------------------------------------------------------
# Persistent home
#
# Keyed on workspace ID, never name or owner, so a rename cannot orphan or
# destroy the volume. No count -- it outlives `coder stop`.
# ---------------------------------------------------------------------------

resource "docker_volume" "home" {
  name = "coder-home-${data.coder_workspace.me.id}"

  lifecycle {
    ignore_changes = all
  }
}

# ---------------------------------------------------------------------------
# Workspace container
#
# start_count is 0 when stopped, so `coder stop` destroys the container and
# leaves docker_volume.home intact.
# ---------------------------------------------------------------------------

resource "docker_container" "workspace" {
  count = data.coder_workspace.me.start_count

  image    = var.workspace_image
  name     = "coder-${data.coder_workspace_owner.me.name}-${lower(data.coder_workspace.me.name)}"
  hostname = data.coder_workspace.me.name

  # ---- right-sizing: these four arguments are the enforcement ----
  cpus        = local.size.cpus_str    # hard CPU ceiling -> HostConfig.NanoCpus
  cpu_shares  = local.size.cpu_shares  # relative weight under contention only
  memory      = local.size.memory_mib  # hard memory ceiling (MiB)
  memory_swap = local.size.memory_mib  # == memory: no swap escape hatch

  # The agent's init_script embeds coderd's access URL (127.0.0.1:3000), which
  # resolves to the workspace container itself. Replace the literal URL rather
  # than regex-matching 127.0.0.1, so nothing else in the script is touched.
  entrypoint = [
    "sh", "-c",
    replace(
      coder_agent.main.init_script,
      data.coder_workspace.me.access_url,
      var.coderd_internal_url,
    ),
  ]

  env = [
    "CODER_AGENT_TOKEN=${coder_agent.main.token}",
    "CODER_AGENT_URL=${var.coderd_internal_url}",
    "KIRO_API_KEY=${local.kiro_api_key}",
    "KIROCREW_INSTANCE_SIZE=${data.coder_parameter.instance_size.value}",
  ]

  networks_advanced {
    name = data.docker_network.workspace.name
  }

  volumes {
    container_path = "/home/coder"
    volume_name    = docker_volume.home.name
    read_only      = false
  }

  labels {
    label = "coder.owner"
    value = data.coder_workspace_owner.me.name
  }
  labels {
    label = "coder.workspace_id"
    value = data.coder_workspace.me.id
  }
  labels {
    label = "coder.workspace_name"
    value = data.coder_workspace.me.name
  }
  labels {
    label = "kirocrew.instance_size"
    value = data.coder_parameter.instance_size.value
  }
}
