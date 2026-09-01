#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf 'Usage: %s <ssh-host> <wheel> <https-dashboard-url>\n' "$0" >&2
  exit 2
}

[[ $# -eq 3 ]] || usage

ssh_host=$1
wheel=$2
dashboard_url=$3

if [[ ! $ssh_host =~ ^([A-Za-z0-9._-]+@)?[A-Za-z0-9._-]+$ ]]; then
  printf 'Invalid SSH host: %s\n' "$ssh_host" >&2
  exit 2
fi
if [[ ! -f $wheel || $wheel != *.whl ]]; then
  printf 'Wheel does not exist or does not end in .whl: %s\n' "$wheel" >&2
  exit 2
fi
wheel_name=$(basename -- "$wheel")
if [[ ! $wheel_name =~ ^kirocrew-[A-Za-z0-9._+]+-py3-none-any\.whl$ ]]; then
  printf 'Wheel filename is not a supported Kiro Crew wheel: %s\n' "$wheel_name" >&2
  exit 2
fi
if [[ ! $dashboard_url =~ ^https://[A-Za-z0-9._-]+(:[0-9]{1,5})?/?$ ]]; then
  printf 'Dashboard URL must be a simple HTTPS origin: %s\n' "$dashboard_url" >&2
  exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
installer=$script_dir/gateway/kirocrew-install-wheel
admin_launcher=$script_dir/gateway/kirocrew-admin
[[ -f $installer ]] || {
  printf 'Missing remote installer: %s\n' "$installer" >&2
  exit 2
}
[[ -f $admin_launcher ]] || {
  printf 'Missing admin launcher: %s\n' "$admin_launcher" >&2
  exit 2
}

if command -v sha256sum >/dev/null 2>&1; then
  wheel_sha=$(sha256sum "$wheel" | awk '{print $1}')
else
  wheel_sha=$(shasum -a 256 "$wheel" | awk '{print $1}')
fi
dashboard_url_b64=$(printf '%s' "$dashboard_url" | base64 | tr -d '\n')
remote_dir=$(ssh -- "$ssh_host" 'umask 077; mktemp -d /tmp/kirocrew-deploy.XXXXXXXX')
if [[ ! $remote_dir =~ ^/tmp/kirocrew-deploy\.[A-Za-z0-9]{8}$ ]]; then
  printf 'Remote host returned an invalid staging directory: %s\n' "$remote_dir" >&2
  exit 1
fi
cleanup() {
  ssh -- "$ssh_host" /usr/bin/rm -rf -- "$remote_dir" >/dev/null 2>&1 || true
}
trap cleanup EXIT
remote_installer=$remote_dir/kirocrew-install-wheel
remote_admin=$remote_dir/kirocrew-admin
remote_wheel=$remote_dir/$wheel_name

scp -- "$installer" "$ssh_host:$remote_installer"
scp -- "$admin_launcher" "$ssh_host:$remote_admin"
scp -- "$wheel" "$ssh_host:$remote_wheel"
ssh -- "$ssh_host" sudo /usr/bin/install -m 0755 \
  "$remote_installer" /usr/local/sbin/kirocrew-install-wheel
ssh -- "$ssh_host" sudo /usr/bin/install -m 0755 \
  "$remote_admin" /usr/local/bin/kirocrew
ssh -- "$ssh_host" sudo /usr/local/sbin/kirocrew-install-wheel \
  "$remote_wheel" "$wheel_sha" "$dashboard_url_b64"

ssh -- "$ssh_host" \
  'sudo systemctl is-active --quiet kirocrew.service && curl --fail --silent --show-error http://127.0.0.1:8443/api/health >/dev/null'
cleanup
trap - EXIT
printf 'Kiro Crew gateway is healthy at %s\n' "$dashboard_url"
