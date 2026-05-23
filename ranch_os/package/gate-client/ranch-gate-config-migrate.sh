#!/bin/sh
# Migrate gate_config.env off FAT32 /boot onto ext4 /var/lib/gate-client.
#
# WHY THIS EXISTS
# ---------------
# The factory flash writes gate_config.env to the /boot FAT32 partition
# (which is the easy way to inject per-device credentials from a host
# laptop without touching the rootfs). LORA_SECRET_KEY is the gate's
# Fernet bearer credential — anyone who reads it can decrypt and forge
# every alert from that gate.
#
# FAT32 doesn't enforce Unix permissions; mode 0600 on a vfat file is
# advisory only. Pulling the SD card out and reading it on any laptop
# yields the key. Moving the file to ext4 narrows that window to "an
# attacker who can read ext4 on a Pi as root" — same threat surface as
# the base-station's Telegram token.
#
# WHY A SEPARATE UNIT INSTEAD OF AN ExecStartPre ON gate-client.service
# --------------------------------------------------------------------
# systemd's EnvironmentFile= is read at unit-activation time, BEFORE any
# ExecStartPre runs. We can't migrate the file from inside the service
# that depends on it. A standalone oneshot ordered Before=gate-client
# does the migration first, then gate-client reads from the new path.
#
# CRASH SAFETY
# ------------
# This script can be interrupted at any point and the next boot picks up
# from where it left off:
#   - dst missing, src present  → copy, atomic mv, then rm src
#   - dst present, src present  → previous run crashed after cp; rm src
#   - dst present, src missing  → migrated; no-op
#   - dst missing, src missing  → nothing to do; gate-client will log
#                                  critical on missing GATE_ID env var
# The cp → mv → rm ordering means the gate can never lose the key
# entirely. Worst case is duplicate copies on disk, which the next run
# fixes.

set -eu

SRC=/boot/gate_config.env
DST_DIR=/var/lib/gate-client
DST=$DST_DIR/gate_config.env

# StateDirectory= on the service also creates this dir, but we run
# BEFORE gate-client and need it to exist for our own write.
mkdir -p "$DST_DIR"
chmod 0750 "$DST_DIR"

# Copy first. Tempfile + mv so a power loss can't leave dst half-written.
if [ ! -f "$DST" ] && [ -f "$SRC" ]; then
    cp "$SRC" "$DST.tmp"
    chmod 0600 "$DST.tmp"
    chown root:root "$DST.tmp"
    sync
    mv "$DST.tmp" "$DST"
    sync
    echo "Copied $SRC -> $DST"
fi

# Only remove src once dst is durably in place. If the previous step ran
# but this one was interrupted, the next boot sees both files and cleans
# up here. Sync between steps so the unlink is visible on the next read.
if [ -f "$SRC" ] && [ -f "$DST" ]; then
    rm -f "$SRC"
    sync
    echo "Removed $SRC (key is now ext4-protected at $DST)"
fi

exit 0
