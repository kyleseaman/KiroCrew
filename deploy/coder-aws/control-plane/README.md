# Coder AWS control-plane POC

This single-user Terraform stack creates the complete low-fixed-cost AWS shape:
an always-on ARM control node for Coder and the Kiro Crew gateway, an
outbound-only VPC/subnet, narrow IAM roles, and outputs consumed by the
workspace template. Tailscale Serve publishes Coder and Kiro Crew to your
tailnet; the AWS security groups expose no inbound ports.

Prerequisites are Terraform 1.5+, authenticated AWS credentials, a Tailscale
tailnet with MagicDNS/HTTPS, and two SSM `SecureString` parameters for the
Tailscale auth key and Kiro credential. The defaults use `us-east-2`, a
`t4g.medium` control node, and a 30 GiB encrypted gp3 disk.

```bash
terraform -chdir=deploy/coder-aws/control-plane init
terraform -chdir=deploy/coder-aws/control-plane apply \
  -var tailscale_auth_parameter=/kirocrew/poc/tailscale-auth-key \
  -var kiro_api_key_parameter=/kirocrew/poc/kiro-api-key \
  -var tailnet_dns_name=example.ts.net

terraform -chdir=deploy/coder-aws/control-plane output coder_url
terraform -chdir=deploy/coder-aws/control-plane output crew_url
```

The control instance and its gp3 disk remain billable around the clock. Kiro
Crew creates one workspace per parent session, targets Coder autostop after 30
inactive minutes, and retains stopped disks for 30 inactive days by default.
That means the Coder dashboard can show many retained workspaces while normally
only active sessions consume EC2 compute. The gateway deletes only workspaces
recorded in its integrity-protected binding registry; an operator-created
`crew-dogfood` bootstrap workspace remains unmanaged. This is a dogfood POC,
not a multi-user or highly available Coder topology; Coder's built-in PostgreSQL
database lives on the control node.

Continue with the [workspace template](../workspace/) and the full
[remote-host guide](../../../docs/guides/remote-and-mobile.md#aws--coder-graviton-dogfood-poc).
