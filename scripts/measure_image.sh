#!/bin/bash
# Loop-mount one or more Ranch OS images and report where the bytes go.
# Useful for deciding BR2_TARGET_ROOTFS_EXT2_SIZE and finding fat to trim.
#
# Usage:  scripts/measure_image.sh IMAGE [IMAGE ...]
# Linux only (needs losetup). Run on the build host.

set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
    echo "measure_image.sh only runs on Linux (needs losetup)." >&2
    exit 2
fi

if [[ $# -eq 0 ]]; then
    echo "Usage: $0 IMAGE [IMAGE ...]" >&2
    exit 2
fi

LOOPS=()
MPS=()

cleanup() {
    set +e
    local m l
    for m in "${MPS[@]:-}"; do sudo umount "$m" 2>/dev/null; done
    for l in "${LOOPS[@]:-}"; do sudo losetup -d "$l" 2>/dev/null; done
    for m in "${MPS[@]:-}"; do rmdir "$m" 2>/dev/null; done
}
trap cleanup EXIT INT TERM HUP

inspect_one() {
    local image="$1"
    if [[ ! -f "$image" ]]; then
        echo "Not found: $image" >&2
        return 1
    fi

    local mp loop
    mp=$(mktemp -d)
    MPS+=("$mp")
    loop=$(sudo losetup -Pf --show "$image")
    LOOPS+=("$loop")
    sudo udevadm settle

    if [[ ! -e "${loop}p2" ]]; then
        echo "No p2 partition on $image — is this a Pi sdcard.img?" >&2
        return 1
    fi
    sudo mount -o ro "${loop}p2" "$mp"

    echo "=== $image ==="
    echo
    echo "-- rootfs usage --"
    df -h "$mp" | tail -1
    echo
    echo "-- top-level (sorted, biggest last) --"
    sudo du -sh "$mp"/* 2>/dev/null | sort -h
    echo
    echo "-- 10 biggest /usr/lib subdirs --"
    sudo du -sh "$mp"/usr/lib/* 2>/dev/null | sort -h | tail -10
    echo
    echo "-- 10 biggest /usr/share subdirs --"
    sudo du -sh "$mp"/usr/share/* 2>/dev/null | sort -h | tail -10
    echo

    # Pop this image's entries so cleanup only tries each path once.
    sudo umount "$mp"
    sudo losetup -d "$loop"
    rmdir "$mp"
    unset 'MPS[-1]' 'LOOPS[-1]'
}

for img in "$@"; do
    inspect_one "$img"
done
