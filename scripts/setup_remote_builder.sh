#!/usr/bin/env bash
# scripts/setup_remote_builder.sh
#
# Provision a fresh Debian/Ubuntu host as a Ranch OS remote build server.
# Run from a developer Mac. After this script completes, ./remote_build.sh
# against the new host will run without any interactive prompts.
#
# During setup itself you may be prompted twice:
#   1. Once for the remote SSH password (one-time, by ssh-copy-id)
#   2. Once for the remote sudo password (one-time, by sudo -v on the
#      remote so apt-get install and sudoers writes are passwordless
#      for the rest of the session)
# Both are unavoidable on a fresh host. After setup, the sudoers fragment
# installed by this script makes every command remote_build.sh needs
# passwordless permanently.
#
# Usage:
#   scripts/setup_remote_builder.sh --host <ip> --user <name>
#
# Options:
#   --host HOST          Remote hostname or IP                   (required)
#   --user USER          Remote username with sudo access        (required)
#   --ssh-port PORT      SSH port                                (default: 22)
#   --ssh-key PATH       Public key to install                   (default:
#                        ~/.ssh/id_ed25519.pub with fallback to id_rsa.pub)
#   --skip-key-copy      Skip ssh-copy-id (keys already in place)
#
# Workload-specific sudoers (e.g. systemctl stop/start of a miner or
# any other process you want to pause via RANCH_BUILD_PRE_HOOK /
# POST_HOOK) are out of scope for this script — add them yourself
# in /etc/sudoers.d/ after the run. See docs/BUILDING.md
# § "Passwordless sudo on the build host" for the convention.
#
# Idempotent. Safe to re-run after tweaking anything.

set -euo pipefail

# -------- argument parsing --------
HOST=""
REMOTE_USER=""
SSH_PORT="22"
SSH_KEY=""
SKIP_KEY_COPY=0

usage() {
    sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --host)           HOST="$2"; shift 2 ;;
        --user)           REMOTE_USER="$2"; shift 2 ;;
        --ssh-port)       SSH_PORT="$2"; shift 2 ;;
        --ssh-key)        SSH_KEY="$2"; shift 2 ;;
        --skip-key-copy)  SKIP_KEY_COPY=1; shift ;;
        -h|--help)        usage; exit 0 ;;
        *) echo "Unknown arg: $1" >&2; usage >&2; exit 2 ;;
    esac
done

[[ -n "$HOST" ]]        || { echo "ERROR: --host is required" >&2; exit 2; }
[[ -n "$REMOTE_USER" ]] || { echo "ERROR: --user is required" >&2; exit 2; }

# -------- find SSH public key --------
if [[ -z "$SSH_KEY" ]]; then
    for candidate in "$HOME/.ssh/id_ed25519.pub" "$HOME/.ssh/id_rsa.pub"; do
        if [[ -f "$candidate" ]]; then
            SSH_KEY="$candidate"
            break
        fi
    done
fi
if [[ -z "$SSH_KEY" ]] || [[ ! -f "$SSH_KEY" ]]; then
    echo "ERROR: no SSH public key found." >&2
    echo "       Generate one with: ssh-keygen -t ed25519" >&2
    echo "       or pass an existing one with --ssh-key /path/to/key.pub" >&2
    exit 1
fi

echo "=== Ranch OS remote-builder setup ==="
echo "  target:  $REMOTE_USER@$HOST:$SSH_PORT"
echo "  ssh key: $SSH_KEY"
echo

# -------- Step 1: install SSH key --------
if [[ "$SKIP_KEY_COPY" -eq 0 ]]; then
    echo "--- Step 1: install SSH key (you may be prompted once for the SSH password) ---"
    if ! command -v ssh-copy-id >/dev/null; then
        echo "ERROR: ssh-copy-id not found on this Mac. Install via Homebrew or copy the key manually." >&2
        exit 1
    fi
    ssh-copy-id -i "$SSH_KEY" -p "$SSH_PORT" "$REMOTE_USER@$HOST"
    echo
else
    echo "--- Step 1: skipped (--skip-key-copy) ---"
    echo
fi

# -------- Step 2: verify keyless SSH --------
echo "--- Step 2: verify keyless SSH ---"
if ! ssh -p "$SSH_PORT" -o BatchMode=yes -o ConnectTimeout=8 \
        "$REMOTE_USER@$HOST" "echo OK" >/dev/null 2>&1; then
    echo "ERROR: keyless SSH still fails. Try running with --skip-key-copy=0 (default)" >&2
    echo "       or check ~/.ssh/config and authorized_keys on the remote." >&2
    exit 1
fi
echo "  [OK] keyless SSH confirmed"
echo

# -------- Step 3: remote provisioning --------
echo "--- Step 3: provision remote (you may be prompted once for sudo on the remote) ---"

# Why scp-then-ssh instead of `ssh -t ... bash -s <<REMOTE`:
# `ssh -t` silently declines to allocate a PTY when stdin is a pipe
# (which it is when you feed a heredoc), which then breaks `sudo -v`
# on the remote with "a terminal is required to read the password".
# `ssh -tt` forces a PTY but the heredoc-as-stdin then traverses the
# PTY and is subject to CR/LF translation, which mangles multi-line
# bash. The reliable shape is: ship the script as a file via scp,
# then run it under a proper interactive SSH where stdin IS a TTY.
#
# REMOTE_USER is passed via the SSH command line as an environment
# variable so the remote bash sees it. The <<'REMOTE' heredoc is
# single-quoted so the local shell doesn't expand $vars — they're
# left literal for the remote bash to interpret.
LOCAL_REMOTE_SCRIPT=$(mktemp -t ranch-remote-setup.XXXXXX)
# shellcheck disable=SC2064  # we want $LOCAL_REMOTE_SCRIPT expanded now
trap "rm -f '$LOCAL_REMOTE_SCRIPT'" EXIT

cat > "$LOCAL_REMOTE_SCRIPT" <<'REMOTE'
set -euo pipefail

# Pre-flight: detect OS family. We only support Debian/Ubuntu in this
# script — the package names and systemd unit conventions are tied to
# apt + the docker.io package.
if [[ ! -r /etc/os-release ]]; then
    echo "ERROR: /etc/os-release missing; cannot identify OS" >&2
    exit 1
fi
. /etc/os-release
case " $ID ${ID_LIKE:-} " in
    *" debian "*|*" ubuntu "*) : ;;
    *)
        echo "ERROR: this script only supports Debian/Ubuntu (found '$ID')." >&2
        echo "       For Arch / RHEL hosts, set up by hand following the" >&2
        echo "       sudoers fragment in docs/BUILDING.md." >&2
        exit 1
        ;;
esac
echo "  detected: $PRETTY_NAME"

# Cache sudo credentials up front so each subsequent sudo call doesn't
# re-prompt. Will prompt once now if /etc/sudoers.d/ranch-build isn't
# already in place from a previous run.
echo "  caching sudo credentials..."
sudo -v

# --- install packages ---
# rsync             remote_build.sh uses this to ship the workspace up
# util-linux        provides losetup, mount, umount used by verify_image.sh
# udev              udevadm settle in verify_image.sh
# docker.io         the build sandbox itself; ships docker.service + docker.socket
# ca-certificates   needed for buildroot's wget downloads inside the container
# coreutils         cat, du used by verify_image.sh / measure_image.sh
# curl              not strictly needed but useful for the user's own debugging
# On a freshly-imaged Ubuntu host, unattended-upgrades fires on first
# boot and holds /var/lib/apt/lists/lock for several minutes — racing
# our `apt-get update` and failing with "Could not get lock". Pass
# DPkg::Lock::Timeout=300 so apt waits up to 5 minutes for the lock
# instead of erroring out. The option was added in apt 2.0 and was
# extended in 2.7 to cover the lists + frontend locks (Ubuntu 24.04
# ships 2.7.x; this works on every supported Ubuntu LTS).
APT_OPTS=(-o "DPkg::Lock::Timeout=300")
echo "  installing packages (waits up to 5 min if apt is busy with unattended-upgrades)..."
sudo DEBIAN_FRONTEND=noninteractive apt-get "${APT_OPTS[@]}" update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get "${APT_OPTS[@]}" install -y -qq \
    docker.io rsync util-linux udev coreutils ca-certificates curl >/dev/null
echo "  [OK] packages installed"

# --- docker group membership ---
# Without this, the user has to sudo every docker call. The build script
# (build.sh inside the container) doesn't expect that. Group membership
# only takes effect on a NEW shell session — we re-test in Step 4 over a
# fresh SSH connection.
if id -nG "$REMOTE_USER" | tr ' ' '\n' | grep -qx docker; then
    echo "  [OK] $REMOTE_USER already in docker group"
else
    sudo usermod -aG docker "$REMOTE_USER"
    echo "  [OK] added $REMOTE_USER to docker group (takes effect on next SSH session)"
fi

# --- build the sudoers fragment ---
# Matches the layout in docs/BUILDING.md § "Passwordless sudo on the
# build host". Build-side only — docker lifecycle (so the pre-hook can
# start it and post-hook can stop it without prompting) and the
# read-only commands verify_image.sh + measure_image.sh need.
#
# Workload-specific pause/resume (xmrig, bitcoind, etc.) is NOT
# covered here — those are operator policy and need their own sudoers
# entries alongside this fragment. The build pipeline doesn't care
# what you're pausing; it just runs whatever RANCH_BUILD_PRE_HOOK /
# POST_HOOK you set.
build_cmd_list() {
    local first=1
    local cmd
    for cmd in "$@"; do
        if [[ "$first" -eq 1 ]]; then
            printf '%s' "$cmd"
            first=0
        else
            printf ', \\\n                           %s' "$cmd"
        fi
    done
}

CMDS=(
    "/usr/bin/systemctl start docker containerd"
    "/usr/bin/systemctl stop docker.socket docker containerd"
    "/usr/sbin/losetup"
    "/usr/bin/mount"
    "/usr/bin/umount"
    "/usr/bin/udevadm"
    "/usr/bin/cat"
    "/usr/bin/du"
)

CMD_LIST=$(build_cmd_list "${CMDS[@]}")

# --- write the sudoers fragment atomically + validated ---
TMP_SUDOERS=$(mktemp)
trap "rm -f $TMP_SUDOERS" EXIT
cat > "$TMP_SUDOERS" <<EOF
# Installed by scripts/setup_remote_builder.sh from the gate-checker repo.
# Allows the Ranch OS remote-build pipeline (build.sh inside Docker, plus
# verify_image.sh post-build) to run a narrow list of privileged commands
# without prompting for a password. Keep the list narrow — never grant
# blanket NOPASSWD: ALL on a build host that also runs other workloads.
$REMOTE_USER ALL=(root) NOPASSWD: $CMD_LIST
EOF

# visudo -c is the only way to know if a sudoers file is parseable. A
# malformed file in /etc/sudoers.d/ locks the user out of sudo, so we
# validate BEFORE installing.
if ! sudo visudo -cf "$TMP_SUDOERS" >/dev/null; then
    echo "ERROR: generated sudoers file failed visudo validation:" >&2
    sed 's/^/  /' "$TMP_SUDOERS" >&2
    exit 1
fi

sudo install -m 0440 -o root -g root "$TMP_SUDOERS" /etc/sudoers.d/ranch-build
echo "  [OK] sudoers fragment installed at /etc/sudoers.d/ranch-build"

# --- prove the NOPASSWD path actually works ---
# `sudo -n` returns non-zero immediately if a password would be needed.
# losetup --help is the cheapest of the NOPASSWD commands.
if sudo -n /usr/sbin/losetup --help >/dev/null 2>&1; then
    echo "  [OK] sudo NOPASSWD confirmed for losetup"
else
    echo "  WARN: sudo -n losetup test failed — recheck /etc/sudoers.d/ranch-build" >&2
fi

# Enable + start Docker so the user can verify, and so remote_build.sh
# works on the first invocation even without the pre-hook starting it.
# (The pre-hook still works fine — `systemctl start docker` on an
# already-active service is a no-op.)
sudo systemctl enable --now docker.service containerd.service >/dev/null 2>&1 || true

echo "  [OK] remote provisioning complete"
REMOTE

# Ship the script to the remote and run it under a real interactive
# SSH (stdin is a TTY, so `sudo -v` can prompt for the password). The
# remote-side cleanup is inlined into the same SSH invocation so the
# temp script disappears even if the run fails mid-way. We preserve
# the inner exit code via rc=$?...exit $rc so a failed provisioning
# step still fails the whole setup.
REMOTE_PATH="/tmp/ranch-builder-setup-$$.sh"
scp -P "$SSH_PORT" -q "$LOCAL_REMOTE_SCRIPT" "$REMOTE_USER@$HOST:$REMOTE_PATH"
ssh -p "$SSH_PORT" -t "$REMOTE_USER@$HOST" \
    "REMOTE_USER='$REMOTE_USER' bash '$REMOTE_PATH'; rc=\$?; rm -f '$REMOTE_PATH'; exit \$rc"

echo

# -------- Step 4: smoke-test docker over a FRESH SSH session --------
# Docker group membership only applies to new login sessions, so we
# explicitly open a new SSH connection here to confirm `docker info`
# works without sudo for the user.
echo "--- Step 4: docker smoke-test over a fresh SSH session ---"
if ssh -p "$SSH_PORT" "$REMOTE_USER@$HOST" "docker info >/dev/null 2>&1"; then
    echo "  [OK] docker accessible to $REMOTE_USER without sudo"
else
    echo "  WARN: docker info failed — most likely the group membership"
    echo "        change hasn't propagated. Try:"
    echo "          ssh $REMOTE_USER@$HOST   # log in fresh"
    echo "          docker info             # should work"
    echo "        If it still fails, check 'getent group docker'."
fi

# -------- closing banner --------
echo
echo "=== Setup complete ==="
echo
echo "Try a build from this Mac:"
echo
echo "  RANCH_BUILD_TARGETS=base \\"
echo "  RANCH_BUILD_PROFILE=development \\"
echo "  RANCH_BUILD_HOST=$HOST \\"
echo "  RANCH_BUILD_USER=$REMOTE_USER \\"
echo "  RANCH_BUILD_PRE_HOOK='sudo systemctl start docker containerd' \\"
echo "  RANCH_BUILD_POST_HOOK='sudo systemctl stop docker.socket docker containerd' \\"
echo "  ./remote_build.sh"
echo
echo "If you also pause some other workload (a miner, a video encoder)"
echo "during builds, add the corresponding systemctl stop/start lines"
echo "to /etc/sudoers.d/ranch-build and chain them in PRE_HOOK / POST_HOOK."
echo
echo "Optional: add a ~/.ssh/config entry on your Mac for shorthand."
echo "  Host ranch-builder"
echo "      HostName $HOST"
echo "      User $REMOTE_USER"
[[ "$SSH_PORT" != "22" ]] && echo "      Port $SSH_PORT"
echo "      IdentityFile $SSH_KEY"
echo
echo "  Then: RANCH_BUILD_HOST=ranch-builder RANCH_BUILD_USER=$REMOTE_USER ./remote_build.sh"
