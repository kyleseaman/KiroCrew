# kirocrew-workspace:poc — a Coder workspace image that can host a kiro-cli agent process.
#
# syntax notes for reviewers: two stages. The kiro-cli archive is ~520 MB
# compressed / ~800 MB extracted. Doing the download+extract in a throwaway
# stage keeps the zip and the extracted tree out of the shipped layers; only
# the installed binaries are copied forward.

# ---------------------------------------------------------------------------
# Stage 1: fetch and install kiro-cli
# ---------------------------------------------------------------------------
FROM debian:bookworm-slim AS kiro

# Pinned to the musl target on purpose. The gnu build of kiro-cli requires
# glibc >= 2.34 (x86_64) / >= 2.39 (aarch64), which rules out a large set of
# still-supported base images. The musl artifact is built static-pie with no
# DT_NEEDED entries at all, so it is immune to the base image's libc entirely
# — that is what lets the runtime stage below be chosen for Coder
# compatibility rather than for libc compatibility.
ARG KIRO_URL=https://desktop-release.q.us-east-1.amazonaws.com/latest/kirocli-x86_64-linux-musl.zip

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl unzip \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp/kiro

# --proto '=https' --tlsv1.2 pins the transport so a redirect cannot downgrade
# the transfer to cleartext. -f makes an HTTP error a build failure instead of
# a zip file full of XML.
RUN curl --proto '=https' --tlsv1.2 -sSf "$KIRO_URL" -o kirocli.zip \
 && unzip -q kirocli.zip \
 && rm kirocli.zip

# Q_INSTALL_GLOBAL installs to /usr/local/bin instead of $HOME/.local/bin.
# This is the critical deviation from our EC2 bootstrap, which installs
# per-user: Coder mounts a persistent volume over /home/coder, so a
# $HOME-based install is shadowed the first time a workspace starts and the
# binary appears to vanish. /usr/local/bin lives in the image and survives.
#
# Q_SKIP_SETUP suppresses the installer's `kiro-cli setup` step, which installs
# shell integrations/dotfiles and expects a real terminal. Skipping it is what
# makes this install fully non-interactive — no `yes |` pipe or `|| true` mask
# needed, so a genuine failure still fails the build.
RUN Q_INSTALL_GLOBAL=1 Q_SKIP_SETUP=1 ./kirocli/install.sh

# ---------------------------------------------------------------------------
# Stage 2: the workspace image
# ---------------------------------------------------------------------------
FROM debian:bookworm-slim

# bookworm-slim leaves uid 1000 free, so `coder` gets it without the
# userdel dance Ubuntu 24.04 images need (they ship an `ubuntu` user at 1000).
ARG USERNAME=coder
ARG USER_UID=1000
ARG USER_GID=1000

ENV DEBIAN_FRONTEND=noninteractive

# git and curl are here because the workspace is a dev environment, not
# because kiro-cli needs them. unzip is retained so an operator can pull a
# newer kiro-cli build inside a running workspace.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash \
      ca-certificates \
      curl \
      git \
      sudo \
      unzip \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid "$USER_GID" "$USERNAME" \
 && useradd --uid "$USER_UID" --gid "$USER_GID" --shell /bin/bash --create-home "$USERNAME" \
 # Coder's agent runs as this user and expects to install tooling without a
 # password prompt; a prompt would hang the workspace build silently.
 && echo "$USERNAME ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/"$USERNAME" \
 && chmod 0440 /etc/sudoers.d/"$USERNAME"

# Three binaries: the `kiro-cli` entrypoint plus the `-chat` and `-term`
# helpers it execs. Copying all three from the install location keeps the
# layout byte-identical to a native install, so `kiro-cli doctor` and any
# future self-update path still resolve their siblings.
COPY --from=kiro /usr/local/bin/kiro-cli /usr/local/bin/kiro-cli
COPY --from=kiro /usr/local/bin/kiro-cli-chat /usr/local/bin/kiro-cli-chat
COPY --from=kiro /usr/local/bin/kiro-cli-term /usr/local/bin/kiro-cli-term

# Deliberately NOT setting KIRO_API_KEY. Declaring it here — even empty —
# would bake a value into image metadata and could mask the absence of a
# runtime credential. It is injected per-container at run time instead
# (see README). Nothing in this image reads or stores a credential.

USER $USERNAME
WORKDIR /home/coder

# $HOME/.local/bin is on PATH so a user-scoped kiro-cli upgrade inside a live
# workspace takes precedence over the image copy without further wiring.
ENV HOME=/home/coder \
    PATH=/home/coder/.local/bin:/usr/local/bin:/usr/bin:/bin

# Coder replaces the entrypoint with its agent script; this default only
# matters when the image is run directly for verification or debugging.
CMD ["/bin/bash", "-l"]
