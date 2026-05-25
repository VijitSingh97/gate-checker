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
#   RANCH_BUILD_PROFILE   `production` (default) or `development`. Development
#                         images include Dropbear SSH and a known root
#                         password; output filenames get a `_dev` suffix.
#   RANCH_BUILD_TARGETS   Space-separated subset of "gate base" (default both).
#                         Set to e.g. "base" to skip the gate image when you're
#                         iterating on base-only changes — cuts build time
#                         roughly in half.
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
PROFILE="${RANCH_BUILD_PROFILE:-production}"
TARGETS="${RANCH_BUILD_TARGETS:-gate base}"
SSH_TARGET="$RANCH_BUILD_USER@$RANCH_BUILD_HOST"

format_duration() {
    local s=$1
    if (( s < 60 )); then
        printf "%ds" "$s"
    elif (( s < 3600 )); then
        printf "%dm%02ds" $(( s / 60 )) $(( s % 60 ))
    else
        printf "%dh%02dm%02ds" $(( s / 3600 )) $(( (s % 3600) / 60 )) $(( s % 60 ))
    fi
}

OVERALL_START=$SECONDS

echo "=== Remote build on $SSH_TARGET:$REMOTE_DIR (profile=$PROFILE) ==="

echo "-> Syncing source tree..."
SYNC_UP_START=$SECONDS
# Excludes mirror .gitignore for the obvious cache/output directories, plus
# belt-and-braces excludes for secret-bearing files — even if one slips
# out of .gitignore for some reason, rsync still refuses to ship it.
# `*.env` covers per-device credential drops (provision_creds.env etc.)
# whose actual write site is the device SD card, never the working tree.
rsync -avz --delete \
  --exclude='releases/' \
  --exclude='dl-cache/' \
  --exclude='ccache-dir/' \
  --exclude='.git/' \
  --exclude='manufacturing_inventory.csv' \
  --exclude='*.env' \
  ./ "$SSH_TARGET:$REMOTE_DIR/"
SYNC_UP_SECS=$(( SECONDS - SYNC_UP_START ))

echo "-> Running build..."
REMOTE_START=$SECONDS
# Build the remote command with printf %q so hook strings survive a round of
# shell parsing on the remote intact, regardless of quoting or special chars.
remote_cmd=$(printf 'cd %q && PRE_HOOK=%q POST_HOOK=%q RANCH_BUILD_PROFILE=%q RANCH_BUILD_TARGETS=%q bash scripts/remote_build_inner.sh' \
  "$REMOTE_DIR" "$PRE_HOOK" "$POST_HOOK" "$PROFILE" "$TARGETS")

# -t allocates a remote TTY so Ctrl-C is delivered as SIGINT to the remote
# bash (which fires the EXIT trap in the inner script) and so docker's TTY
# output streams live instead of block-buffering.
ssh -t "$SSH_TARGET" "$remote_cmd"
REMOTE_SECS=$(( SECONDS - REMOTE_START ))

echo "-> Fetching built images..."
SYNC_DOWN_START=$SECONDS
rsync -avz "$SSH_TARGET:$REMOTE_DIR/releases/" ./releases/
SYNC_DOWN_SECS=$(( SECONDS - SYNC_DOWN_START ))

TOTAL_SECS=$(( SECONDS - OVERALL_START ))
echo "=== Remote build complete ==="
echo "    rsync up:       $(format_duration $SYNC_UP_SECS)"
echo "    remote (ssh):   $(format_duration $REMOTE_SECS)"
echo "    rsync down:     $(format_duration $SYNC_DOWN_SECS)"
echo "    total:          $(format_duration $TOTAL_SECS)"
