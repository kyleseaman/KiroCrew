# Coder AWS control-plane POC

This single-user Terraform stack creates the shared AWS foundation: an
always-on ARM Coder control node, an outbound-only VPC/subnet, separate narrow
IAM roles for gateway and session compute, and outputs consumed by both Coder
templates. The Kiro Crew gateway runs in its own Coder workspace; it is not
co-located with the Coder server. Tailscale Serve publishes Coder and the
gateway to your tailnet, while the AWS security groups expose no inbound ports.

Prerequisites are Terraform 1.5+, authenticated AWS credentials, a Tailscale
tailnet with MagicDNS/HTTPS, and three SSM `SecureString` parameters: a reusable
`tag:kirocrew-control` Tailscale key, a separate ephemeral
`tag:kirocrew-session` key, and the Kiro credential. The defaults use `us-east-2`, a
`t4g.medium` control node, and a 30 GiB encrypted gp3 disk.

```bash
terraform -chdir=deploy/coder-aws/control-plane init
terraform -chdir=deploy/coder-aws/control-plane apply \
  -var tailscale_auth_parameter=/kirocrew/poc/tailscale-auth-key \
  -var tailscale_session_auth_parameter=/kirocrew/poc/tailscale-session-auth-key \
  -var kiro_api_key_parameter=/kirocrew/poc/kiro-api-key \
  -var tailnet_dns_name=example.ts.net

terraform -chdir=deploy/coder-aws/control-plane output coder_url
terraform -chdir=deploy/coder-aws/control-plane output gateway_template_values
terraform -chdir=deploy/coder-aws/control-plane output session_template_values
```

The control instance, gateway instance, and their gp3 disks remain billable
around the clock. Kiro Crew creates one workspace per parent session, targets
Coder autostop after 30 inactive minutes, and retains stopped disks for 30
inactive days by default.
Set the gateway template's default TTL to `0h` and its workspace stop schedule
to `manual`, as shown in the remote-host guide. The gateway is the lifecycle
controller for session workspaces and must not inherit Coder's ordinary
workspace autostop default.
The gateway template also retains its root EBS volume if its EC2 instance is
replaced or the workspace is deleted. That recovery guard prevents an
infrastructure update from deleting gateway memory and session state, but the
retained volume must be removed explicitly after a verified backup when the POC
is intentionally torn down.
That means the Coder dashboard can show many retained workspaces while normally
only active sessions consume EC2 compute. The gateway deletes only workspaces
recorded in its integrity-protected binding registry. This is a dogfood POC,
not a multi-user or highly available Coder topology; Coder's built-in
PostgreSQL database lives on the control node.

The generated roles allow KMS decryption only through regional SSM and only for
the exact configured Parameter Store ARN. A customer-managed KMS key must grant
those roles in its own key policy as well. Use the least-privilege tailnet policy
sample in the remote-host guide; session-tagged nodes need HTTPS to the
control-tagged Coder server but do not receive Tailscale SSH access.

The gateway template installs `/usr/local/bin/kirocrew` as an administration
launcher. After the Kiro Crew wheel is installed in its stable
`/home/coder/kirocrew-venv`, commands entered as `ec2-user` on that gateway host
run under the same `coder` identity, home, and working directory as the service.
The Coder control node intentionally has no Kiro Crew CLI or durable Crew data.

Install or update the gateway from a locally built wheel with the repository's
deployment helper:

```bash
deploy/coder-aws/deploy-gateway.sh \
  ec2-user@crew-gateway-user \
  dist/kirocrew-*.whl \
  https://crew-gateway-user.example.ts.net:8443
```

The helper verifies the wheel hash on the gateway node, installs it into the
stable `/home/coder/kirocrew-venv`, installs the gateway as a systemd service
owned by `coder`, and fails unless both systemd and the loopback health endpoint
report ready. It is also the update path for an existing gateway node; the
helper is copied before use, so applying new cloud-init user data is not
required. Continue with the [gateway template](../gateway/) and
[session template](../workspace/).

The full sequence is in the
[remote-host guide](../../../docs/guides/remote-and-mobile.md#aws--coder-graviton-dogfood-poc).
