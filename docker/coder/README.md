# Coder POC harness

A self-contained Coder deployment for testing **Kiro Crew sessions whose agent
process runs inside a Coder workspace** instead of on the gateway host.

Everything here runs locally against Docker. Nothing touches a cloud account.
See `docs/request-for-change/rfc-coder-remote-sandboxes.md` for the design and
the measured results.

- `Dockerfile` — workspace image (`kirocrew-workspace:poc`) carrying `kiro-cli`.
- `main.tf` — Coder template with a per-workspace size parameter.

## Prerequisites

Docker, and a `coder` CLI whose version matches the server you run below.
Download the release binary rather than piping an install script to a shell.

## Bring it up

**1. A user-defined network.** Required: the default bridge has no embedded DNS,
so the workspace agent could not resolve the coderd container by name.

```bash
docker network create coder-poc
```

**2. coderd.** Loopback-published and telemetry off. The Docker socket mount is
what lets the Terraform provider create sibling containers; `--group-add` gives
the non-root coderd user access to it.

```bash
docker volume create coder-data
# The volume is root-owned on creation but coderd runs as uid 1000.
docker run --rm -v coder-data:/data alpine:3.20 chown -R 1000:1000 /data

docker run -d --name coderd \
  --network coder-poc \
  -p 127.0.0.1:3000:3000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v coder-data:/home/coder/.config \
  --group-add "$(getent group docker | cut -d: -f3)" \
  -e CODER_ACCESS_URL=http://127.0.0.1:3000 \
  -e CODER_HTTP_ADDRESS=0.0.0.0:3000 \
  -e CODER_TELEMETRY_ENABLE=false \
  ghcr.io/coder/coder:latest
```

Wait for `GET /api/v2/buildinfo` to return 200.

**3. First user and an automation token.** Keep the password and token out of
the shell history and out of this repo — `*.env` and `secrets/` are gitignored.

```bash
docker exec -e PW="$PASSWORD" coderd sh -c 'coder login http://127.0.0.1:3000 \
  --first-user-email you@example.com --first-user-username poc \
  --first-user-password "$PW" --first-user-trial=false'

docker exec coderd coder tokens create --lifetime 168h --name poc-automation
```

`--lifetime` is capped at **168h**; anything larger is rejected.

**4. The workspace image.**

```bash
docker build -t kirocrew-workspace:poc docker/coder/
docker run --rm kirocrew-workspace:poc kiro-cli --version   # expect: kiro-cli <version>
```

**5. The template.**

```bash
export CODER_URL=http://127.0.0.1:3000 CODER_SESSION_TOKEN=<token>
coder templates push kirocrew-poc -d docker/coder --yes \
  --var kiro_api_key="$KIRO_API_KEY"
```

Supply the credential as a **template variable** (`sensitive = true`), not as the
per-workspace `coder_parameter` — parameter values are persisted as workspace
build parameters in coderd's database and are readable back through the API.

**6. A workspace.**

```bash
coder create ws-small --template kirocrew-poc \
  --parameter instance_size=small --parameter kiro_api_key= --yes
```

Every parameter needs a default or an explicit `--parameter`: `--yes` does not
suppress the prompt for a parameter with no default, and the build dies with
`prepare build: EOF`. Note `--parameter` takes the parameter's `name`, not its
`display_name`.

## Verify

Run the whole evidence chain — sizing ceilings, transport latency, credential
delivery, and the ACP handshake — in one go:

```bash
python3 docker/coder/verify.py
python3 docker/coder/verify.py --workspace ws-small --workspace ws-build
```

It exits non-zero on the number of failed checks, prints nothing secret, and
*skips* rather than fails the ACP handshake when no credential is available
(`kiro-cli acp` refuses to start without one). Supply a key by exporting
`KIRO_API_KEY`, or point at a different variable with `--api-key-env`.

To check individual pieces by hand instead:

Sizes are `small` (2 vCPU / 2 GiB), `medium` (4/4), `build` (8/8). Confirm the
ceiling actually binds, from both sides:

```bash
docker inspect coder-poc-ws-small \
  --format 'NanoCpus={{.HostConfig.NanoCpus}} Memory={{.HostConfig.Memory}}'
coder ssh ws-small -- cat /sys/fs/cgroup/cpu/cpu.cfs_quota_us \
                        /sys/fs/cgroup/memory/memory.limit_in_bytes
```

The workspace image runs **cgroup v1**, so the enforcement files are
`cpu.cfs_quota_us` / `cpu.cfs_period_us` and `memory.limit_in_bytes` — not
cgroup v2's `cpu.max` / `memory.max`.

Then the agent itself:

```bash
coder ssh ws-small -- kiro-cli --version
```

## Traps

These each cost real debugging time.

- **`coder ssh` mangles multi-argument commands.** `coder ssh ws kiro-cli acp --help`
  fails with `unrecognized subcommand`. Always use the `--` separator. A
  single-word command works either way, which makes this easy to miss.
- **`coder ssh -e` silently drops some variable names.** A generic marker is
  delivered; a credential-named variable arrives empty, with exit 0 and no
  warning. Set it inside the remote command or, better, from the template's
  container env.
- **stdin must be closed for one-shot remote commands.** `coder ssh` holds the
  exec channel open while stdin is readable, so an inherited stdin makes even
  `--version` hang. Use `DEVNULL`.
- **`kiro-cli` installs to `$HOME/.local/bin` by default, and Coder mounts a
  volume over `/home/coder`** — a `$HOME` install is shadowed on first workspace
  start and the binary appears to vanish. The Dockerfile uses
  `Q_INSTALL_GLOBAL=1` to install to `/usr/local/bin`.
- **The agent's `init_script` embeds coderd's access URL**, which inside a
  workspace container resolves to the container itself. The template rewrites it
  to `var.coderd_internal_url` (`http://coderd:3000`), which is why the shared
  user-defined network in step 1 is mandatory.
- **`nproc` does not respect cgroup CPU quota**, so `pytest -n auto` in a 2-vCPU
  workspace still forks one worker per host core. The ceiling protects the host;
  it does not size the run.

## Tear down

```bash
coder delete ws-small --yes
docker rm -f coderd
docker volume rm coder-data
docker network rm coder-poc
```
