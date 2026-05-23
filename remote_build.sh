#!/bin/bash
# Drive the Buildroot build on a remote host and pull images back.
#
# Required environment variables:
#   RANCH_BUILD_HOST     SSH host (e.g. builder.lan or 192.168.1.10)
#   RANCH_BUILD_USER     SSH user
#
# Optional environment variables:
#   RANCH_BUILD_DIR       Remote workspace path (default: ~/ranch_os_build)
#   RANCH_BUILD_PRE_HOOK  Shell command to run on the remote before building
#   RANCH_BUILD_POST_HOOK Shell command to run on the remote after building.
#                         Runs unconditionally via an EXIT trap, so it fires
#                         on success, build failure, or Ctrl-C.
#
# The actual remote-side logic lives in scripts/remote_build_inner.sh, which
# rsync ships over alongside the rest of the workspace.
#
# Example: pause an xmrig miner during the build and resume it afterwards.
# The post-hook stops docker.socket as well so dockerd isn't re-activated by
# stray connections while xmrig is running.
#
#   RANCH_BUILD_HOST=builder.lan \
#   RANCH_BUILD_USER=ci \
#   RANCH_BUILD_PRE_HOOK='sudo systemctl stop xmrig && sudo systemctl start docker containerd' \
#   RANCH_BUILD_POST_HOOK='sudo systemctl stop docker.socket docker containerd; sudo systemctl start xmrig' \
#   ./remote_build.sh

set -euo pipefail

: "${RANCH_BUILD_HOST:?Set RANCH_BUILD_HOST to the build server hostname or IP}"
: "${RANCH_BUILD_USER:?Set RANCH_BUILD_USER to the SSH login on the build server}"

REMOTE_DIR="${RANCH_BUILD_DIR:-/home/$RANCH_BUILD_USER/ranch_os_build}"
PRE_HOOK="${RANCH_BUILD_PRE_HOOK:-true}"
POST_HOOK="${RANCH_BUILD_POST_HOOK:-true}"
SSH_TARGET="$RANCH_BUILD_USER@$RANCH_BUILD_HOST"

echo "=== Remote build on $SSH_TARGET:$REMOTE_DIR ==="

echo "-> Syncing source tree..."
rsync -avz --delete \
  --exclude='releases/' \
  --exclude='dl-cache/' \
  --exclude='ccache-dir/' \
  --exclude='.git/' \
  ./ "$SSH_TARGET:$REMOTE_DIR/"

echo "-> Running build..."
# Build the remote command with printf %q so hook strings survive a round of
# shell parsing on the remote intact, regardless of quoting or special chars.
remote_cmd=$(printf 'cd %q && PRE_HOOK=%q POST_HOOK=%q bash scripts/remote_build_inner.sh' \
  "$REMOTE_DIR" "$PRE_HOOK" "$POST_HOOK")

# -t allocates a remote TTY so Ctrl-C is delivered as SIGINT to the remote
# bash (which fires the EXIT trap in the inner script) and so docker's TTY
# output streams live instead of block-buffering.
ssh -t "$SSH_TARGET" "$remote_cmd"

echo "-> Fetching built images..."
rsync -avz "$SSH_TARGET:$REMOTE_DIR/releases/" ./releases/

echo "=== Remote build complete ==="
