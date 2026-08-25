"""Static contracts for the single-user AWS Coder dogfood POC."""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POC = ROOT / "deploy" / "coder-aws"


def _read(relative: str) -> str:
    return (POC / relative).read_text(encoding="utf-8")


def test_control_plane_is_arm_nat_free_and_has_no_ingress() -> None:
    terraform = _read("control-plane/main.tf")

    assert 'resource "aws_iam_role_policy_attachment" "workspace_ssm"' in terraform
    control_policy = terraform.split('resource "aws_iam_role_policy" "control"', 1)[1]

    assert 'required_version = ">= 1.5"' in terraform
    assert 'default     = "us-east-2"' in terraform
    assert 'default     = "t4g.medium"' in terraform
    assert "al2023-ami-kernel-default-arm64" in terraform
    assert "associate_public_ip_address = true" in terraform
    assert 'http_tokens                 = "required"' in terraform
    assert 'volume_type = "gp3"' in terraform
    assert 'resource "aws_nat_gateway"' not in terraform
    assert "ingress {" not in terraform
    assert "ec2:DescribeInstanceTypes" in terraform
    assert "ec2:DescribeNetworkInterfaces" in terraform
    assert "iam:GetInstanceProfile" in terraform
    assert "parameter/aws/service/ami-amazon-linux-latest/*" in terraform
    assert "local.parameter_arns.kiro" in control_policy


def test_workspace_is_persistent_arm_compute_with_bounded_choices() -> None:
    terraform = _read("workspace/main.tf")

    assert 'required_version = ">= 1.5"' in terraform
    assert re.search(r'default\s*=\s*"us-east-2"', terraform)
    assert re.search(r'default\s*=\s*"c8g.large"', terraform)
    assert 'value = "c8g.xlarge"' in terraform
    assert re.search(r'arch\s*=\s*"arm64"', terraform)
    assert not re.search(r"(?m)^\s*dir\s*=", terraform)
    assert "al2023-ami-kernel-default-arm64" in terraform
    assert 'resource "aws_ec2_instance_state" "workspace"' in terraform
    assert 'volume_type = "gp3"' in terraform
    assert "aws_nat_gateway" not in terraform


def test_workspace_bootstrap_verifies_kiro_and_reads_secrets_from_ssm() -> None:
    terraform = _read("workspace/main.tf")
    bootstrap = _read("workspace/cloud-init.sh.tftpl")

    assert "useradd --create-home --shell /bin/bash coder" in bootstrap
    assert "--uid 1000" not in bootstrap
    assert "coder_agent_token_b64" in terraform
    assert "CODER_AGENT_TOKEN_FILE=/etc/coder-agent-token" in bootstrap
    assert "chmod 600 /etc/coder-agent-token" in bootstrap
    assert 'KIRO_ARCH="aarch64"' in bootstrap
    assert "manifest.json" in bootstrap
    assert "sha256sum -c -" in bootstrap
    assert "aws ssm get-parameter" in bootstrap
    assert "--with-decryption" in bootstrap
    assert "tailscale.repo" in bootstrap
    assert "dnf config-manager --add-repo" in bootstrap
    assert "awscli2" not in bootstrap
    assert "curl | sh" not in bootstrap
    assert "AKIA" not in bootstrap
    assert "probe-value" not in bootstrap
    assert "changeme" not in bootstrap
    assert "User=coder" in bootstrap
    assert "User=root" not in bootstrap
    assert "chown coder:coder /etc/kiro-api-key.b64" in bootstrap


def test_workspace_bootstrap_makes_working_directory_traversable() -> None:
    bootstrap = _read("workspace/cloud-init.sh.tftpl")

    assert "install -d -m 0700 -o coder -g coder /home/coder/workspace" in bootstrap


def test_control_bootstrap_pins_coder_and_serves_only_over_tailscale() -> None:
    bootstrap = _read("control-plane/cloud-init.sh.tftpl")

    assert "coder_${coder_version}_linux_arm64.rpm" in bootstrap
    assert "coder_rpm_sha256" in bootstrap
    assert "sha256sum -c -" in bootstrap
    assert 'KIRO_ARCH="aarch64"' in bootstrap
    assert "kirocli-manifest.json" in bootstrap
    assert "nodejs22" in bootstrap
    assert "python3.11" in bootstrap
    assert "CODER_HTTP_ADDRESS=127.0.0.1:3000" in bootstrap
    assert "CODER_MAX_ADMIN_TOKEN_LIFETIME=8760h" in bootstrap
    assert "tailscale serve --bg --https=443" in bootstrap
    assert "tailscale serve --bg --https=8443 http://127.0.0.1:8443" in bootstrap
    assert "--ssh" in bootstrap
    assert "curl | sh" not in bootstrap


def test_control_bootstrap_starts_with_one_package_install_command(tmp_path: Path) -> None:
    bash = shutil.which("bash")
    if bash is None:
        return

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_dnf = fake_bin / "dnf"
    fake_dnf.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\nexit 42\n', encoding="utf-8")
    fake_dnf.chmod(0o755)

    result = subprocess.run(
        [bash, str(POC / "control-plane/cloud-init.sh.tftpl")],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={"PATH": f"{fake_bin}:/usr/bin:/bin"},
    )

    assert result.returncode == 42
    assert result.stdout.splitlines() == [
        "install",
        "-y",
        "ca-certificates",
        "dnf-plugins-core",
        "git",
        "jq",
        "nodejs22",
        "nodejs22-npm",
        "python3.11",
        "python3.11-pip",
        "unzip",
    ]


def test_workspace_bootstrap_installs_only_available_base_packages(tmp_path: Path) -> None:
    bash = shutil.which("bash")
    if bash is None:
        return

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_dnf = fake_bin / "dnf"
    fake_dnf.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\nexit 42\n', encoding="utf-8")
    fake_dnf.chmod(0o755)

    result = subprocess.run(
        [bash, str(POC / "workspace/cloud-init.sh.tftpl")],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={"PATH": f"{fake_bin}:/usr/bin:/bin"},
    )

    assert result.returncode == 42
    assert result.stdout.splitlines() == [
        "install",
        "-y",
        "ca-certificates",
        "dnf-plugins-core",
        "git",
        "jq",
        "unzip",
    ]


def test_measurement_harness_estimates_only_active_compute_cost() -> None:
    path = POC / "verify.py"
    source = path.read_text(encoding="utf-8")
    spec = importlib.util.spec_from_file_location("coder_aws_verify", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    assert module.active_compute_cost(0.17, 0) == 0.0
    assert module.active_compute_cost(0.17, 1800) == 0.085
    assert module.monthly_idle_cost(0.0336, 30, 0.08) == 26.928
    assert '"protocolVersion": "2025-08-22"' in source
    assert '"readTextFile": False' in source
