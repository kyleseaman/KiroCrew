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
    control_policy = terraform.split('resource "aws_iam_role_policy" "control"', 1)[1].split(
        'resource "aws_iam_instance_profile" "control"', 1
    )[0]

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
    assert "ec2:DescribeInstanceCreditSpecifications" in terraform
    assert "ec2:DescribeNetworkInterfaces" in terraform
    assert "iam:GetInstanceProfile" in terraform
    assert "parameter/aws/service/ami-amazon-linux-latest/*" in terraform
    assert "local.parameter_arns.kiro" not in control_policy


def test_control_plane_limits_ec2_mutations_to_coder_managed_resources() -> None:
    terraform = _read("control-plane/main.tf")
    control_policy = terraform.split('resource "aws_iam_role_policy" "control"', 1)[1].split(
        'resource "aws_iam_instance_profile" "control"', 1
    )[0]

    describe_statement = control_policy.split('Sid    = "DescribeEc2"', 1)[1].split("},", 1)[0]
    mutate_statement = control_policy.split('Sid    = "MutateCoderInstances"', 1)[1].split("},", 1)[
        0
    ]
    run_statement = control_policy.split('Sid    = "RunCoderInstances"', 1)[1].split("},", 1)[0]

    assert 'Resource = "*"' in describe_statement
    assert "ec2:StartInstances" in mutate_statement
    assert "ec2:ResourceTag/KiroCrewManaged" in mutate_statement
    assert '"ec2:ResourceTag/KiroCrewManaged" = "true"' in mutate_statement
    assert "ec2:RunInstances" in run_statement
    assert "aws:RequestTag/KiroCrewManaged" in run_statement
    assert re.search(r'"ec2:CreateAction"\s*=\s*"RunInstances"', control_policy)
    assert re.search(r'"aws:RequestTag/KiroCrewManaged"\s*=\s*"true"', control_policy)
    assert re.search(r'KiroCrewManaged\s*=\s*"true"', _read("workspace/main.tf"))
    assert re.search(r'KiroCrewManaged\s*=\s*"true"', _read("gateway/main.tf"))


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
    assert re.search(r'volume_type\s*=\s*"gp3"', terraform)
    assert "aws_nat_gateway" not in terraform


def test_workspace_bootstrap_verifies_kiro_and_reads_secrets_from_ssm() -> None:
    terraform = _read("workspace/main.tf")
    bootstrap = _read("workspace/cloud-init.sh.tftpl")

    assert "useradd --create-home --shell /bin/bash coder" in bootstrap
    assert "--uid 1000" not in bootstrap
    assert 'auth               = "aws-instance-identity"' in terraform
    assert "coder_agent_token_b64" not in terraform
    assert "coder-agent-token" not in bootstrap
    assert "CODER_AGENT_TOKEN_FILE" not in bootstrap
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
    assert 'KIRO_API_KEY="$(base64 -d /etc/kiro-api-key.b64)"' in bootstrap
    assert "ExecStart=/usr/local/libexec/coder-agent-start" in bootstrap
    assert "connection_timeout = 600" in terraform
    assert "curl --retry 5 --retry-all-errors" in bootstrap
    assert "/var/lib/kirocrew/bootstrap-status" in bootstrap
    assert 'write_bootstrap_status "complete"' in bootstrap


def test_session_workspace_uses_ephemeral_tailnet_identity_on_every_boot() -> None:
    bootstrap = _read("workspace/cloud-init.sh.tftpl")
    control = _read("control-plane/main.tf")

    assert "--state=mem:" in bootstrap
    assert "--port=$${PORT} $FLAGS" in bootstrap
    assert "$${FLAGS}" not in bootstrap
    assert "$$FLAGS" not in bootstrap
    assert "kirocrew-tailscale-up.service" in bootstrap
    assert "systemctl enable --now kirocrew-tailscale-up.service" in bootstrap
    assert "After=network-online.target kirocrew-tailscale-up.service" in bootstrap
    assert 'variable "tailscale_session_auth_parameter"' in control
    session_variable = control.split('variable "tailscale_session_auth_parameter"', 1)[1].split(
        "}", 1
    )[0]
    assert "default" not in session_variable
    assert "var.tailscale_session_auth_parameter" in control


def test_gateway_is_a_dedicated_persistent_coder_workspace() -> None:
    terraform = _read("gateway/main.tf")
    bootstrap = _read("gateway/cloud-init.sh.tftpl")

    assert re.search(r'default\s*=\s*"t4g.small"', terraform)
    assert re.search(r'arch\s*=\s*"arm64"', terraform)
    assert 'resource "aws_ec2_instance_state" "gateway"' in terraform
    assert 'resource "coder_app" "kiro_crew"' in terraform
    assert 'url          = "http://localhost:8443"' in terraform
    assert re.search(r'volume_type\s*=\s*"gp3"', terraform)
    assert re.search(r"delete_on_termination\s*=\s*false", terraform)
    assert "associate_public_ip_address," in terraform
    assert "aws_nat_gateway" not in terraform
    assert "kirocrew-install-wheel" in bootstrap
    assert "tailscale serve --bg --https=8443 http://127.0.0.1:8443" in bootstrap
    assert 'auth               = "aws-instance-identity"' in terraform
    assert "coder_agent_token_b64" not in terraform
    assert "coder-agent-token" not in bootstrap
    assert "CODER_AGENT_TOKEN_FILE" not in bootstrap
    assert "KIRO_API_KEY=" in bootstrap
    assert "coder_${coder_version}_linux_arm64.rpm" in bootstrap
    assert "coder_rpm_sha256" in bootstrap
    assert "usermod --shell /bin/bash coder" in bootstrap
    assert re.search(
        r"(?m)^install -d -o coder -g coder -m 0700 /home/coder/\.kiro$",
        bootstrap,
    )
    assert 'KIRO_BOOTSTRAP_DIR="/var/tmp/kirocrew-bootstrap"' in bootstrap
    assert "/tmp/kirocli.zip" not in bootstrap
    assert "connection_timeout = 600" in terraform
    assert "curl --retry 5 --retry-all-errors" in bootstrap
    assert "/var/lib/kirocrew/bootstrap-status" in bootstrap


def test_gateway_runbook_disables_autostop_without_changing_session_ttl() -> None:
    guide = (ROOT / "docs" / "guides" / "remote-and-mobile.md").read_text(encoding="utf-8")

    assert "coder templates edit kirocrew-gateway-aws --default-ttl 0h" in guide
    assert "coder schedule stop crew-gateway-user manual" in guide
    assert "autostop after 30 minutes" in guide


def test_control_plane_exports_distinct_gateway_and_session_template_values() -> None:
    terraform = _read("control-plane/main.tf")

    assert 'output "gateway_template_values"' in terraform
    assert 'output "session_template_values"' in terraform
    assert 'resource "aws_iam_instance_profile" "gateway"' in terraform
    assert 'resource "aws_iam_instance_profile" "session"' in terraform


def test_workspace_bootstrap_makes_working_directory_traversable() -> None:
    bootstrap = _read("workspace/cloud-init.sh.tftpl")

    assert "install -d -m 0700 -o coder -g coder /home/coder/workspace" in bootstrap
    assert "systemd-run --user --scope --quiet /usr/bin/true" in bootstrap
    assert "systemd-run --user --scope --quiet --wait" not in bootstrap
    assert "/etc/kirocrew-coder-contract.json" in bootstrap
    assert '"version": 1' in bootstrap
    assert '"remote_cwd": "/home/coder/workspace"' in bootstrap
    assert '"systemd-user-scopes"' in bootstrap


def test_control_bootstrap_pins_coder_and_serves_only_over_tailscale() -> None:
    terraform = _read("control-plane/main.tf")
    bootstrap = _read("control-plane/cloud-init.sh.tftpl")

    assert "coder_${coder_version}_linux_arm64.rpm" in bootstrap
    assert "coder_rpm_sha256" in bootstrap
    assert "sha256sum -c -" in bootstrap
    assert "CODER_HTTP_ADDRESS=127.0.0.1:3000" in bootstrap
    assert "CODER_MAX_ADMIN_TOKEN_LIFETIME=8760h" in bootstrap
    assert "tailscale serve --bg --https=443" in bootstrap
    assert "--https=8443" not in bootstrap
    assert "kirocrew" not in bootstrap.lower()
    assert "kiro_api_key_parameter" not in bootstrap
    assert 'filebase64("${path.module}/kirocrew-admin")' not in terraform
    assert 'filebase64("${path.module}/kirocrew-install-wheel")' not in terraform
    assert "--ssh" in bootstrap
    assert "curl | sh" not in bootstrap


def test_control_launcher_preserves_gateway_identity_and_arguments(tmp_path: Path) -> None:
    shell = shutil.which("sh")
    if shell is None:
        return

    source_path = POC / "gateway" / "kirocrew-admin"
    assert source_path.exists(), "the gateway template must ship its admin launcher"

    service_home = tmp_path / "coder-home"
    service_bin = service_home / "kirocrew-venv" / "bin" / "kirocrew"
    service_bin.parent.mkdir(parents=True)
    service_bin.write_text(
        """#!/bin/sh
printf 'home=%s\\n' "$HOME"
printf 'user=%s\\n' "$USER"
printf 'logname=%s\\n' "$LOGNAME"
printf 'cwd=%s\\n' "$PWD"
printf 'sudo-user=%s\\n' "$OBSERVED_SUDO_USER"
printf 'argc=%s\\n' "$#"
for argument in "$@"; do
  printf 'arg=%s\\n' "$argument"
done
""",
        encoding="utf-8",
    )
    service_bin.chmod(0o755)

    fake_sudo = tmp_path / "sudo"
    fake_sudo.write_text(
        """#!/bin/sh
set -eu
test "$1" = "-n"
shift
test "$1" = "-u"
shift
export OBSERVED_SUDO_USER="$1"
shift
exec "$@"
""",
        encoding="utf-8",
    )
    fake_sudo.chmod(0o755)

    launcher = tmp_path / "kirocrew"
    launcher.write_text(
        source_path.read_text(encoding="utf-8")
        .replace("/usr/bin/sudo", str(fake_sudo))
        .replace("/home/coder", str(service_home)),
        encoding="utf-8",
    )
    launcher.chmod(0o755)

    injection_target = tmp_path / "must-not-exist"
    injection_argument = f"$(touch {injection_target})"
    result = subprocess.run(
        [str(launcher), "token", "value with spaces", injection_argument],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        f"home={service_home}",
        "user=coder",
        "logname=coder",
        f"cwd={service_home}",
        "sudo-user=coder",
        "argc=3",
        "arg=token",
        "arg=value with spaces",
        f"arg={injection_argument}",
    ]
    assert not injection_target.exists()


def test_gateway_deployer_preserves_valid_wheel_name_for_remote_pip(
    tmp_path: Path,
) -> None:
    bash = shutil.which("bash")
    if bash is None:
        return

    wheel = tmp_path / "kirocrew-0.0.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel-under-test")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    trace = tmp_path / "trace"
    for command in ("scp", "ssh"):
        executable = fake_bin / command
        body = (
            "#!/bin/sh\n"
            "set -eu\n"
            f"printf '{command}' >> {trace!s}\n"
            f"printf ' <%s>' \"$@\" >> {trace!s}\n"
            f"printf '\\n' >> {trace!s}\n"
        )
        if command == "ssh":
            body += "case \"$*\" in *mktemp*) printf '%s\\n' /tmp/kirocrew-deploy.A1b2C3d4;; esac\n"
        executable.write_text(body, encoding="utf-8")
        executable.chmod(0o755)

    result = subprocess.run(
        [
            bash,
            str(POC / "deploy-gateway.sh"),
            "ec2-user@kirocrew-coder",
            str(wheel),
            "https://kirocrew-coder.example.ts.net:8443",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={"PATH": f"{fake_bin}:/usr/bin:/bin"},
    )

    assert result.returncode == 0, result.stderr
    lines = trace.read_text(encoding="utf-8").splitlines()
    remote_dir = "/tmp/kirocrew-deploy.A1b2C3d4"
    remote_wheel = f"{remote_dir}/{wheel.name}"

    assert len(lines) == 9
    assert lines[0].startswith("ssh <--> <ec2-user@kirocrew-coder>")
    assert "mktemp -d /tmp/kirocrew-deploy.XXXXXXXX" in lines[0]
    assert lines[1].startswith("scp <-->")
    assert "gateway/kirocrew-install-wheel" in lines[1]
    assert f"ec2-user@kirocrew-coder:{remote_dir}/kirocrew-install-wheel" in lines[1]
    assert lines[2].startswith("scp <-->")
    assert "gateway/kirocrew-admin" in lines[2]
    assert f"ec2-user@kirocrew-coder:{remote_dir}/kirocrew-admin" in lines[2]
    assert lines[3] == f"scp <--> <{str(wheel)}> <ec2-user@kirocrew-coder:{remote_wheel}>"
    assert lines[4].startswith("ssh <--> <ec2-user@kirocrew-coder>")
    assert "/usr/local/sbin/kirocrew-install-wheel" in lines[4]
    assert lines[5].startswith("ssh <--> <ec2-user@kirocrew-coder>")
    assert "/usr/local/bin/kirocrew" in lines[5]
    assert f"<{remote_wheel}>" in lines[6]
    assert lines[7].startswith("ssh <--> <ec2-user@kirocrew-coder>")
    assert "systemctl is-active --quiet kirocrew.service" in lines[7]
    assert "curl --fail --silent --show-error http://127.0.0.1:8443/api/health" in lines[7]
    assert lines[8] == (
        "ssh <--> <ec2-user@kirocrew-coder> </usr/bin/rm> <-rf> <--> " f"<{remote_dir}>"
    )


def test_gateway_deployer_rejects_shell_metacharacters_before_remote_calls(
    tmp_path: Path,
) -> None:
    bash = shutil.which("bash")
    if bash is None:
        return

    wheel = tmp_path / "kirocrew.whl"
    wheel.write_bytes(b"wheel-under-test")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    touched = tmp_path / "remote-command-ran"
    for command in ("scp", "ssh"):
        executable = fake_bin / command
        executable.write_text(
            f"#!/bin/sh\ntouch {touched!s}\nexit 99\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)

    result = subprocess.run(
        [
            bash,
            str(POC / "deploy-gateway.sh"),
            "ec2-user@host;touch-bad",
            str(wheel),
            "https://kirocrew-coder.example.ts.net:8443",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={"PATH": f"{fake_bin}:/usr/bin:/bin"},
    )

    assert result.returncode != 0
    assert not touched.exists()


def test_gateway_remote_installer_is_identity_scoped_and_health_checked() -> None:
    installer = _read("gateway/kirocrew-install-wheel")

    assert "sha256sum" in installer
    assert "KIROCREW_PORT=8443" in installer
    assert "SUDO_USER=coder" in installer
    assert "USER=coder" in installer
    assert "LOGNAME=coder" in installer
    assert "HOME=/home/coder" in installer
    assert "KIROCREW_SERVICE_BIN=/home/coder/kirocrew-venv/bin/kirocrew" in installer
    assert "kirocrew setup --agent-only" in installer
    assert "kirocrew service install" in installer
    assert "loginctl enable-linger coder" in installer
    assert "/var/lib/kirocrew/swapfile" in installer
    assert "mkswap" in installer
    assert "swapon" in installer
    assert 'pip install --force-reinstall --no-deps "$verified_wheel"' in installer
    assert "/var/lib/kirocrew-staging" in installer
    assert "mktemp -d /var/lib/kirocrew-staging/install.XXXXXXXXXX" in installer
    assert "/var/lib/kirocrew/staging" not in installer
    assert "/tmp/kirocrew-verified-" not in installer
    assert "systemctl is-active --quiet kirocrew.service" in installer
    assert "http://127.0.0.1:8443/api/health" in installer
    assert "curl | sh" not in installer
    assert "git clone" not in installer


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

    bootstrap_status_dir = tmp_path / "bootstrap-status"
    bootstrap = (POC / "workspace/cloud-init.sh.tftpl").read_text(encoding="utf-8")
    bootstrap = bootstrap.replace(
        'BOOTSTRAP_STATUS="/var/lib/kirocrew/bootstrap-status"',
        f'BOOTSTRAP_STATUS="{bootstrap_status_dir / "status"}"',
    ).replace(
        "install -d -m 0755 /var/lib/kirocrew",
        f'install -d -m 0755 "{bootstrap_status_dir}"',
    )
    script = tmp_path / "cloud-init.sh"
    script.write_text(bootstrap, encoding="utf-8")

    result = subprocess.run(
        [bash, str(script)],
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
