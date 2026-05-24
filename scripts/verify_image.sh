#!/bin/bash
# Verify that a built Ranch OS image contains the files the build was
# supposed to produce. Loop-mounts the image's rootfs, runs a fixed set of
# existence + content checks, and prints a pass/fail summary.
#
# Usage:  scripts/verify_image.sh path/to/image.img
# Exit:   0 if every check passes, 1 otherwise.
#
# Requires losetup + mount, so this script only runs on Linux. Use it on
# the build host right after remote_build.sh, or copy the image to any
# Linux box. Needs sudo to loop-mount; you'll be prompted once.

set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
    echo "verify_image.sh only runs on Linux (needs losetup). Run it on the build host." >&2
    exit 2
fi

IMAGE="${1:-}"
if [[ -z "$IMAGE" ]]; then
    echo "Usage: $0 path/to/image.img" >&2
    exit 2
fi
if [[ ! -f "$IMAGE" ]]; then
    echo "Image not found: $IMAGE" >&2
    exit 1
fi

case "$(basename "$IMAGE")" in
    base_station*) FLAVOR=base ;;
    gate_client*)  FLAVOR=gate ;;
    *)
        echo "Cannot determine image flavor from filename: $(basename "$IMAGE")" >&2
        echo "Expected base_station*.img or gate_client*.img" >&2
        exit 2
        ;;
esac

# Detect dev vs prod from filename suffix so we can run profile-specific
# checks (dropbear, root password hash) only where they apply.
IS_DEV=0
case "$(basename "$IMAGE")" in
    *_dev.img) IS_DEV=1 ;;
esac

MOUNT_POINT=$(mktemp -d)
LOOP=$(sudo losetup -Pf --show "$IMAGE")

cleanup() {
    set +e
    sudo umount "$MOUNT_POINT" 2>/dev/null
    sudo losetup -d "$LOOP" 2>/dev/null
    rmdir "$MOUNT_POINT" 2>/dev/null
}
trap cleanup EXIT INT TERM HUP

sudo udevadm settle

if [[ ! -e "${LOOP}p2" ]]; then
    echo "Expected partition ${LOOP}p2 not found — is this a sdcard.img?" >&2
    exit 1
fi
sudo mount -o ro "${LOOP}p2" "$MOUNT_POINT"

pass=0
fail=0

check_file() {
    local path="$1"
    local desc="${2:-$path}"
    if [[ -e "$MOUNT_POINT$path" ]]; then
        echo "  [OK]   $desc"
        pass=$((pass + 1))
    else
        echo "  [FAIL] $desc  (missing: $path)"
        fail=$((fail + 1))
    fi
}

# Pass when at least one of several alternate paths exists. Useful for
# binaries whose location varies (/usr/bin vs /usr/sbin) across Buildroot
# package versions.
check_any() {
    local desc="$1"
    shift
    for path in "$@"; do
        if [[ -e "$MOUNT_POINT$path" ]]; then
            echo "  [OK]   $desc"
            pass=$((pass + 1))
            return
        fi
    done
    echo "  [FAIL] $desc  (not found at any of: $*)"
    fail=$((fail + 1))
}

check_grep() {
    local path="$1"
    local pattern="$2"
    local desc="$3"
    # Read via `sudo cat` so the only privileged command we need is cat —
    # narrower NOPASSWD surface than allowing arbitrary grep invocations.
    if sudo cat "$MOUNT_POINT$path" 2>/dev/null | grep -qE "$pattern"; then
        echo "  [OK]   $desc"
        pass=$((pass + 1))
    else
        echo "  [FAIL] $desc  (pattern '$pattern' not in $path)"
        fail=$((fail + 1))
    fi
}

# Inverse of check_file: pass when the path is ABSENT. Used by the
# production-profile block to assert dev-only artifacts (dropbear,
# debug tools, gate-side networkd) haven't leaked into a shippable
# image. Follows symlinks (-e) so a dangling symlink also counts as
# "not really there".
check_absent() {
    local path="$1"
    local desc="${2:-$path absent}"
    if [[ ! -e "$MOUNT_POINT$path" ]]; then
        echo "  [OK]   $desc"
        pass=$((pass + 1))
    else
        echo "  [FAIL] $desc  (unexpected: $path is present)"
        fail=$((fail + 1))
    fi
}

check_no_grep() {
    # Asserts a pattern is ABSENT from a file — for guarding against
    # imports of packages we don't actually ship (the dotenv class of bug),
    # leftover debug imports, etc.
    local path="$1"
    local pattern="$2"
    local desc="$3"
    
    # Python script to strip ONLY actual comments (ignores '#' inside strings)
    local strip_comments="import sys, tokenize
try:
    for t in tokenize.generate_tokens(sys.stdin.readline):
        if t.type != tokenize.COMMENT:
            sys.stdout.write(t.string)
except Exception:
    pass"

    if sudo cat "$MOUNT_POINT$path" 2>/dev/null | python3 -c "$strip_comments" | grep -qE "$pattern"; then
        echo "  [FAIL] $desc  (forbidden pattern '$pattern' present in $path)"
        fail=$((fail + 1))
    else
        echo "  [OK]   $desc"
        pass=$((pass + 1))
    fi
}

echo "== Verifying $FLAVOR image: $IMAGE =="
echo

# ----------------------------------------------------------------------
# Common to both flavors: /boot must be mounted at runtime so the factory
# flash scripts' cred-injection is visible to userspace, and the LoRa
# radio needs the GPIO UART enabled + serial-getty masked so it can own
# /dev/serial0 exclusively.
# ----------------------------------------------------------------------
check_grep "/etc/fstab" \
    "^/dev/mmcblk0p1[[:space:]]+/boot[[:space:]]+vfat" \
    "/etc/fstab mounts /boot at runtime"

# Hostname-from-DEVICE_ID — common to both flavors. The script and unit
# file are dropped in by the rootfs-overlay, the wants-symlink is what
# actually enables the service at boot.
check_file "/usr/bin/ranch-set-hostname" "hostname helper installed"
check_any "ranch-hostname.service unit installed" \
    "/etc/systemd/system/ranch-hostname.service" \
    "/usr/lib/systemd/system/ranch-hostname.service"
check_file "/etc/systemd/system/multi-user.target.wants/ranch-hostname.service" \
    "ranch-hostname.service enabled at multi-user.target"

# NTP-before-TLS handling: not done via systemd ordering anymore (the
# wait-sync coupling caused first-boot ExecStopPost timeouts). Instead
# base_station.py waits in-process before sending the Telegram online
# ping. Verify the Python code contains that wait, and that
# base-provision uses --no-block when activating base-station.
if [[ "$FLAVOR" == "base" ]]; then
    check_grep "/usr/bin/base_station.py" "_wait_for_clock_sync" \
        "base_station.py waits for NTP sync in-process before Telegram ping"
    check_grep "/usr/bin/base_station.py" "_force_ntp_sync" \
        "base_station.py has manual NTP fallback when timesyncd is idle"
    # Telegram bot token must be scrubbed from exception text before logging
    # so support bundles / journal exports don't leak it. Guard against
    # silent regression: the call site must wrap exc in _redact_token().
    check_grep "/usr/bin/base_station.py" "_redact_token\\(str\\(exc\\)\\)" \
        "base_station.py redacts the bot token from Telegram error logs"

    # First-time Telegram credential rejection flips the device back into
    # the captive portal with a banner, instead of waiting for the watchdog.
    # All three pieces have to ship together: the sentinel, the flip
    # helper, and the call site in run().
    check_grep "/usr/bin/base_station.py" "SETUP_VALIDATED_PATH" \
        "base_station.py tracks first-time setup validation via sentinel file"
    check_grep "/usr/bin/base_station.py" "_flip_to_setup_mode" \
        "base_station.py has the setup-mode rearm helper for credential rejection"
    check_grep "/usr/bin/base_station.py" "SETUP_ERROR_PATH" \
        "base_station.py writes a banner-ready reason for provision.py"

    # Telegram command channel — long-poll thread that drives /pair,
    # /unpair, /rename, /confirm, /cancel, /status, /help. The class
    # and all named handlers must ship together; a missing handler
    # silently swallows the command (returns "Unknown command") and
    # the failure mode wouldn't show up until an operator tried it.
    check_grep "/usr/bin/base_station.py" "class TelegramCommandChannel" \
        "TelegramCommandChannel class shipped"
    check_grep "/usr/bin/base_station.py" "class PendingAction" \
        "PendingAction dataclass shipped (confirm-flow state)"
    for handler in _cmd_pair _cmd_unpair _cmd_rename _cmd_open _cmd_close _cmd_factory_reset _cmd_confirm _cmd_cancel _cmd_status _cmd_help; do
        check_grep "/usr/bin/base_station.py" "def ${handler}\\(" \
            "command handler ${handler} present"
    done
    # /pair must redact the operator's message after parsing — without
    # this the Fernet key sits in chat history forever. Guard against
    # the regression.
    check_grep "/usr/bin/base_station.py" "_delete_message\\(chat_id, message_id\\)" \
        "/pair calls deleteMessage on the operator's input"
    # Constant-time token compare matters: without it, an attacker
    # can side-channel the 4-char token from response timing.
    check_grep "/usr/bin/base_station.py" "compare_digest\\(args\\[0\\], pending.token\\)" \
        "/confirm uses constant-time compare on the token"
    # /open and /close drive the LoRa challenge/command sequence — the
    # transport methods must ship together. lora_command also acquires
    # _lora_tx_lock so the alert-reader thread can't interleave bytes
    # mid-frame; check both pieces.
    check_grep "/usr/bin/base_station.py" "def lora_command\\(" \
        "BaseStation.lora_command (challenge → command flow) present"
    check_grep "/usr/bin/base_station.py" "def lora_status_request\\(" \
        "BaseStation.lora_status_request (status_req flow) present"
    check_grep "/usr/bin/base_station.py" "_lora_tx_lock" \
        "serial-port write lock present (prevents frame interleave)"
    check_grep "/usr/bin/base_station.py" "class _LoRaRequestSlot" \
        "LoRa pending-reply slot class present"
    # /factory_reset uses os._exit from the daemon thread; if a future
    # refactor swaps it for sys.exit, only the daemon thread dies and
    # base-station keeps running while base-provision tries to take
    # wlan0. Guard the call site.
    check_grep "/usr/bin/base_station.py" "def _perform_factory_reset\\(" \
        "factory-reset wipe routine present"
    check_grep "/usr/bin/base_station.py" "os\\._exit\\(0\\)" \
        "factory reset exits the whole process (os._exit, not sys.exit)"
    # The user-ID allow-list was removed in favour of "the chat IS
    # the auth boundary". Make sure no stray reference sneaks back in
    # — its presence here would suggest a half-applied revert that
    # would silently disable command access.
    check_no_grep "/usr/bin/base_station.py" "TELEGRAM_ALLOWED_USERS" \
        "no TELEGRAM_ALLOWED_USERS references remain"

    # Captive portal must validate token / chat ID format at submit time,
    # surface post-join errors as a banner, and pre-fill form values.
    check_grep "/usr/bin/provision.py" "_validate_setup_form" \
        "provision.py runs form-format validation before AP teardown"
    check_grep "/usr/bin/provision.py" "_read_setup_error" \
        "provision.py surfaces post-join setup errors back to the operator"
    check_grep "/usr/bin/provision.py" "setup_error" \
        "captive portal template renders a setup_error banner"
    check_grep "/usr/lib/systemd/system/base-provision.service" \
        "systemctl --no-block start base-station" \
        "base-provision uses --no-block when starting base-station"
    # timesyncd needs explicit NTP servers — Buildroot's systemd may have
    # been built without built-in fallbacks, leaving timesyncd with
    # nothing to query.
    check_grep "/etc/systemd/timesyncd.conf" \
        "^FallbackNTP=" \
        "timesyncd.conf has explicit FallbackNTP servers"
    # PrivateTmp drop-in lets timesyncd resolve through the
    # Buildroot-default /etc/resolv.conf -> /tmp/resolv.conf symlink.
    # See file's own comment header for full rationale.
    check_grep \
        "/etc/systemd/system/systemd-timesyncd.service.d/ranch-os.conf" \
        "^PrivateTmp=no" \
        "timesyncd PrivateTmp=no drop-in (works around Buildroot symlink + sandbox)"
fi

# config.txt isn't on the rootfs at runtime (it lives in the FAT boot
# partition), so we sneak-peek it via the same loop device. The boot
# partition is the FIRST partition (p1).
{
    BOOT_MP=$(mktemp -d)
    sudo mount -o ro "${LOOP}p1" "$BOOT_MP" 2>/dev/null
    if sudo cat "$BOOT_MP/config.txt" 2>/dev/null | grep -qE "^enable_uart=1"; then
        echo "  [OK]   config.txt enables the UART (enable_uart=1)"
        pass=$((pass + 1))
    else
        echo "  [FAIL] config.txt enables the UART (enable_uart=1)  (missing or wrong)"
        fail=$((fail + 1))
    fi
    if sudo cat "$BOOT_MP/config.txt" 2>/dev/null | grep -qE "^dtoverlay=disable-bt"; then
        echo "  [OK]   config.txt frees PL011 from Bluetooth"
        pass=$((pass + 1))
    else
        echo "  [FAIL] config.txt frees PL011 from Bluetooth  (dtoverlay=disable-bt missing)"
        fail=$((fail + 1))
    fi
    # Pi 3 boards have been observed to fail to even boot (black-screen
    # HDMI, no network) when gpu_mem is set below ~32 MB. Guard against
    # accidentally re-introducing that.
    GPU_MEM=$(sudo cat "$BOOT_MP/config.txt" 2>/dev/null | grep -E "^gpu_mem=" | tail -1 | cut -d= -f2 | tr -d ' ')
    if [ -n "$GPU_MEM" ] && [ "$GPU_MEM" -ge 32 ] 2>/dev/null; then
        echo "  [OK]   config.txt gpu_mem=$GPU_MEM is safe (>=32)"
        pass=$((pass + 1))
    else
        echo "  [FAIL] config.txt gpu_mem must be >=32 (got '${GPU_MEM:-unset}')"
        echo "         Pi 3 fails to boot when GPU memory is starved."
        fail=$((fail + 1))
    fi

    # Kernel name must match the kernel Buildroot produced for this
    # board. Pi Zero W (32-bit ARMv6) → zImage; Pi 3 64-bit → Image.
    # We assert the right per-board [piN] section is present.
    case "$FLAVOR" in
    gate)
        if sudo cat "$BOOT_MP/config.txt" 2>/dev/null | grep -qE "^kernel=zImage"; then
            echo "  [OK]   config.txt sets kernel=zImage for Pi Zero W"
            pass=$((pass + 1))
        else
            echo "  [FAIL] config.txt has no kernel=zImage line — Pi Zero W needs it"
            fail=$((fail + 1))
        fi
        ;;
    base)
        if sudo cat "$BOOT_MP/config.txt" 2>/dev/null | grep -qE "^kernel=Image"; then
            echo "  [OK]   config.txt sets kernel=Image for Pi 3 64-bit"
            pass=$((pass + 1))
        else
            echo "  [FAIL] config.txt has no kernel=Image line — Pi 3 64-bit needs it"
            fail=$((fail + 1))
        fi
        if sudo cat "$BOOT_MP/config.txt" 2>/dev/null | grep -qE "^arm_64bit=1"; then
            echo "  [OK]   config.txt sets arm_64bit=1 for Pi 3 64-bit"
            pass=$((pass + 1))
        else
            echo "  [FAIL] config.txt has no arm_64bit=1 — Pi 3 64-bit needs it"
            fail=$((fail + 1))
        fi
        ;;
    esac

    sudo umount "$BOOT_MP" 2>/dev/null
    rmdir "$BOOT_MP" 2>/dev/null
}

# serial-getty@ttyAMA0 must be MASKED (symlink to /dev/null) so it can't
# fight base_station.py / gate_client.py for the UART. We also mask
# ttyS0 — on Pi 3 with disable-bt the mini-UART node still appears,
# and Buildroot's systemd preset has been observed to enable a getty
# there on some defconfigs. Either getty grabbing the line corrupts
# LoRa traffic, so both must stay masked.
check_getty_mask() {
    local tty="$1"
    local link="$MOUNT_POINT/etc/systemd/system/serial-getty@${tty}.service"
    if [[ -L "$link" ]] && [[ "$(readlink "$link")" == "/dev/null" ]]; then
        echo "  [OK]   serial-getty@${tty} is masked"
        pass=$((pass + 1))
    else
        echo "  [FAIL] serial-getty@${tty} is masked"
        echo "         (expected $link -> /dev/null)"
        fail=$((fail + 1))
    fi
}
check_getty_mask ttyAMA0
check_getty_mask ttyS0

if [[ "$FLAVOR" == "base" ]]; then
    # ------------------------------------------------------------------
    # Base-station application + services
    # ------------------------------------------------------------------
    check_file "/usr/bin/base_station.py"
    check_file "/usr/bin/provision.py"
    check_file "/usr/bin/ranch-wifi-connect" "captive-portal Wi-Fi helper installed"
    check_file "/etc/sudoers.d/basesetup" "sudoers drop-in installed"
    check_file "/usr/lib/systemd/system/base-station.service"
    check_file "/usr/lib/systemd/system/base-provision.service"
    check_file "/etc/systemd/system/multi-user.target.wants/base-station.service" \
        "base-station.service enabled"
    check_file "/etc/systemd/system/multi-user.target.wants/base-provision.service" \
        "base-provision.service enabled"
    check_any "sudo binary" /usr/bin/sudo /usr/sbin/sudo
    check_grep "/etc/passwd" "^basesetup:" "basesetup system user exists"
    check_grep "/etc/sudoers.d/basesetup" "ranch-wifi-connect" \
        "sudoers rule scoped to ranch-wifi-connect"

    # ------------------------------------------------------------------
    # Async setup flow contracts — make sure the structural pieces that
    # got us out of the WPA2-debug hell are still in the image. Each of
    # these failing means a regression that would re-break the captive-
    # portal handoff in subtle ways.
    # ------------------------------------------------------------------
    check_grep "/usr/bin/provision.py" "_complete_setup" \
        "provision.py uses async _complete_setup (background Wi-Fi join)"
    check_grep "/usr/bin/provision.py" "_restart_access_point" \
        "provision.py can restart the setup AP on Wi-Fi failure"
    check_grep "/usr/bin/provision.py" "wifi.mode.*ap" \
        "provision.py uses explicit wifi.mode=ap profile (not nmcli hotspot shortcut)"
    check_grep "/usr/bin/provision.py" "_scan_networks" \
        "provision.py pre-scans Wi-Fi for the SSID dropdown"
    check_grep "/usr/bin/provision.py" "datalist" \
        "captive portal template includes the SSID datalist"
    # Captive portal is intentionally credentials-only — gate pairing
    # happens over Telegram once the device is online. If the /add_gate
    # route ever sneaks back in, it'd give operators a UI they can never
    # reach in steady state (since the AP is gone after setup), so guard
    # against the regression.
    check_no_grep "/usr/bin/provision.py" "/add_gate" \
        "provision.py has no /add_gate route (pairing is Telegram-only)"

    # Wi-Fi connectivity watchdog — brings the captive portal back if the
    # device gets locked out of its configured Wi-Fi (changed password,
    # router replaced, sustained outage).
    check_file "/usr/bin/ranch-wifi-watchdog" "Wi-Fi watchdog installed"
    check_file "/usr/lib/systemd/system/ranch-wifi-watchdog.service" \
        "Wi-Fi watchdog service unit installed"
    check_grep "/usr/lib/systemd/system/base-station.service" \
        "Wants=.*ranch-wifi-watchdog" \
        "base-station.service pulls in the watchdog via Wants="
    check_grep "/usr/bin/ranch-wifi-connect" "restart-ap" \
        "wifi helper supports --restart-ap mode"
    check_grep "/usr/bin/ranch-wifi-connect" "connection down" \
        "wifi helper tears down AP before joining station mode"

    # Guard against importing packages we don't actually select in the
    # Buildroot fragment — those crash at runtime with ModuleNotFoundError
    # and only show up in `journalctl` on a real device.
    check_no_grep "/usr/bin/base_station.py" "^[[:space:]]*from dotenv|^[[:space:]]*import dotenv" \
        "base_station.py doesn't import dotenv (not packaged)"
    # /dev/serial0 doesn't exist on Buildroot (no rpi-sys-mods udev rule);
    # use the real PL011 device directly.
    check_no_grep "/usr/bin/base_station.py" "/dev/serial0" \
        "base_station.py uses /dev/ttyAMA0, not the missing /dev/serial0 symlink"

    # ------------------------------------------------------------------
    # Captive portal AP dependencies — added incrementally as each one
    # turned out to be silently missing during the initial bring-up. Now
    # they're checked on every build so the same regressions can't recur.
    # ------------------------------------------------------------------
    check_any "NetworkManager daemon" \
        /usr/sbin/NetworkManager /sbin/NetworkManager /usr/bin/NetworkManager
    check_any "nmcli CLI" \
        /usr/bin/nmcli /usr/sbin/nmcli
    check_any "wpa_supplicant" \
        /usr/sbin/wpa_supplicant /usr/bin/wpa_supplicant
    check_any "rfkill" \
        /usr/sbin/rfkill /usr/bin/rfkill
    check_any "iw (diagnostic)" \
        /usr/sbin/iw /usr/bin/iw
    check_any "dnsmasq" \
        /usr/sbin/dnsmasq /usr/bin/dnsmasq

    # NM must be told to write /etc/resolv.conf directly. Without this,
    # Buildroot's NM build leaves /etc/resolv.conf empty/missing and
    # libc can't resolve anything → no Telegram, no NTP, no internet.
    check_grep "/etc/NetworkManager/conf.d/dns.conf" \
        "^rc-manager=file" \
        "NetworkManager configured to write /etc/resolv.conf (rc-manager=file)"

    # Wireless firmware + regulatory database.
    check_file "/lib/firmware/regulatory.db" "wireless regulatory database"
    check_file "/lib/firmware/brcm/brcmfmac43430-sdio.bin" \
        "BCM43430 wifi firmware (symlink)"
    check_file "/lib/firmware/cypress/cyfmac43430-sdio.bin" \
        "Cypress 43430 firmware blob (symlink target)"

else
    # ------------------------------------------------------------------
    # Gate-client application + service
    # ------------------------------------------------------------------
    check_file "/usr/bin/gate_client.py"
    check_file "/usr/lib/systemd/system/gate-client.service"
    check_file "/etc/systemd/system/multi-user.target.wants/gate-client.service" \
        "gate-client.service enabled"

    # Gate config migration off FAT32 /boot → ext4 /var/lib/gate-client.
    # If any of these regress, the LORA_SECRET_KEY ends up readable from a
    # plugged-in SD card again — the threat this whole unit exists to close.
    check_file "/usr/bin/ranch-gate-config-migrate" \
        "gate config migration helper installed"
    check_file "/usr/lib/systemd/system/gate-config-migrate.service" \
        "gate-config-migrate.service unit installed"
    check_file "/etc/systemd/system/multi-user.target.wants/gate-config-migrate.service" \
        "gate-config-migrate.service enabled at multi-user.target"
    check_grep "/usr/lib/systemd/system/gate-client.service" \
        "^EnvironmentFile=/var/lib/gate-client/gate_config.env" \
        "gate-client.service reads EnvironmentFile from ext4 (not /boot)"
    check_grep "/usr/lib/systemd/system/gate-client.service" \
        "^Requires=gate-config-migrate.service" \
        "gate-client.service Requires= the migration unit"
    # Belt and braces: the migration script itself must keep the source
    # path correct, otherwise a typo silently turns the migration into
    # a no-op and the key sits on FAT32 forever.
    check_grep "/usr/bin/ranch-gate-config-migrate" \
        "^SRC=/boot/gate_config.env" \
        "migration script points at the right FAT32 source"
    check_grep "/usr/bin/ranch-gate-config-migrate" \
        "^DST=" \
        "migration script declares an ext4 destination"

    # Same dotenv guard as on the base side — both files used to import
    # python-dotenv even though we never selected the package.
    check_no_grep "/usr/bin/gate_client.py" "^[[:space:]]*from dotenv|^[[:space:]]*import dotenv" \
        "gate_client.py doesn't import dotenv (not packaged)"
    # /dev/serial0 doesn't exist on Buildroot (no rpi-sys-mods udev rule);
    # use the real PL011 device directly.
    check_no_grep "/usr/bin/gate_client.py" "/dev/serial0" \
        "gate_client.py uses /dev/ttyAMA0, not the missing /dev/serial0 symlink"

    # gpiozero + a pin-factory backend must be installed; gate_client.py
    # imports Button and OutputDevice from gpiozero, and gpiozero needs
    # RPi.GPIO (or equivalent) under the hood. We check for the package
    # *directory*, not a specific `__init__.py`, because the gate
    # fragment sets BR2_PACKAGE_PYTHON3_PYC_ONLY=y — Buildroot strips
    # the source files and ships only `.pyc`, so `__init__.py` won't
    # exist even when the package is correctly installed. Globbing the
    # python site-packages dir because the Python version segment
    # varies.
    check_any "gpiozero installed" \
        /usr/lib/python3.11/site-packages/gpiozero \
        /usr/lib/python3.12/site-packages/gpiozero \
        /usr/lib/python3.13/site-packages/gpiozero
    check_any "RPi.GPIO installed (gpiozero pin factory)" \
        /usr/lib/python3.11/site-packages/RPi/GPIO \
        /usr/lib/python3.12/site-packages/RPi/GPIO \
        /usr/lib/python3.13/site-packages/RPi/GPIO
fi

# ----------------------------------------------------------------------
# Dev-profile-only checks: dropbear must be present AND root must have a
# real password hash, not an empty/locked field. Catches the regression
# where dev.fragment silently fails to merge and the image ships without
# remote-debug access.
# ----------------------------------------------------------------------
if [[ "$IS_DEV" -eq 1 ]]; then
    echo
    echo "-- dev profile --"
    check_any "dropbear SSH server" \
        /usr/sbin/dropbear /usr/bin/dropbear
    # /etc/shadow root row must start with `root:$` (a real hash starts
    # with $1$, $5$, $6$, or $y$). Empty (`root::`) or locked (`root:!`
    # / `root:*`) fields would all fail to match.
    check_grep "/etc/shadow" "^root:\\\$[0-9a-zA-Z]" \
        "root has a password hash in /etc/shadow"

    # Dev gate images get a USB-ethernet SSH path so a developer can plug
    # a USB-to-eth adapter into the Pi Zero W's data micro-USB port and
    # SSH in over the LAN. Production gate images strip all this back
    # out — no networkd, no .network file — so the LoRa-only invariant
    # in gate.fragment holds.
    if [[ "$FLAVOR" == "gate" ]]; then
        check_file "/usr/lib/systemd/system/systemd-networkd.service" \
            "systemd-networkd unit installed (re-enabled by dev-gate.fragment)"
        # The vendor preset (90-systemd.preset → enable systemd-networkd)
        # creates this alias symlink on `systemctl preset-all`. Its
        # presence is a reliable signal the service is enabled at boot.
        check_file "/etc/systemd/system/dbus-org.freedesktop.network1.service" \
            "systemd-networkd enabled at boot"
        check_file "/etc/systemd/network/20-usb-eth.network" \
            "USB-ethernet DHCP .network unit installed"
        check_grep "/etc/systemd/network/20-usb-eth.network" \
            "^DHCP=yes" \
            "USB-ethernet .network unit requests DHCP"

        # "Black box" diagnostic snapshot: a single self-restarting
        # service (Type=simple + Restart=always + RestartSec=60) that
        # writes ip/journal/dmesg to /boot/dev_diag.txt every 60s. The
        # boot partition is FAT32 so the developer can pull the SD
        # card, plug it into macOS Finder, and read the file with no
        # ext4 tools. Critical when the gate doesn't come up on the
        # LAN and there's no console cable handy.
        #
        # Note: a .timer unit would be more "systemd-idiomatic" but
        # Buildroot's `systemctl preset-all` finalize step wipes
        # overlay-installed wants-symlinks for units in /usr/lib.
        # The pattern that survives is: unit in /etc/systemd/system/
        # + relative wants-symlink, same as ranch-hostname.service.
        check_file "/usr/bin/ranch-dev-diag" \
            "dev-diag snapshot script installed"
        check_file "/etc/systemd/system/ranch-dev-diag.service" \
            "dev-diag service unit installed in /etc"
        check_file "/etc/systemd/system/multi-user.target.wants/ranch-dev-diag.service" \
            "dev-diag service enabled at multi-user.target"
    fi
fi

# ----------------------------------------------------------------------
# Production-profile-only checks: assert NO dev-only artifact leaked in.
# Catches the regression where a build forgets to clear
# RANCH_BUILD_PROFILE=development before producing a shippable image,
# or where dev.fragment / dev-gate.fragment paths get accidentally
# referenced from a production code path. None of these should EVER
# appear in an image whose filename doesn't carry the `_dev` suffix.
# ----------------------------------------------------------------------
if [[ "$IS_DEV" -eq 0 ]]; then
    echo
    echo "-- production profile (no dev leakage) --"

    # SSH server must not be present — production has no remote login
    # path on purpose. The only way in is physical access to the SD card.
    check_absent /usr/sbin/dropbear "dropbear absent (no remote login)"
    check_absent /usr/bin/dropbear  "dropbear absent (no remote login, alt path)"

    # /etc/shadow's root row must NOT carry a real password hash. Real
    # hashes start with $1$, $5$, $6$, or $y$ — the regex catches all of
    # those. Acceptable lockout values (empty field, `*`, `!`) do not
    # match. If this fires, dev.fragment leaked into the build.
    check_no_grep "/etc/shadow" "^root:\\\$" \
        "root account is not unlocked (no real password hash)"

    # Debug tools shipped only by dev.fragment. Any one of these in a
    # production image means dev.fragment got merged for this build.
    check_absent /usr/bin/less    "less absent"
    check_absent /usr/bin/strace  "strace absent"
    check_absent /usr/bin/tcpdump "tcpdump absent"

    # Gate-specific dev-gate.fragment artifacts. Production gates must
    # honor the LoRa-only invariant from gate.fragment: no networkd,
    # no IP stack, no USB-eth helpers, no on-device diagnostic script.
    if [[ "$FLAVOR" == "gate" ]]; then
        check_absent /usr/lib/systemd/system/systemd-networkd.service \
            "systemd-networkd not installed (LoRa-only invariant)"
        check_absent /etc/systemd/network/20-usb-eth.network \
            "USB-eth .network unit absent"
        check_absent /usr/bin/ranch-dev-diag \
            "ranch-dev-diag script absent"
        check_absent /etc/systemd/system/ranch-dev-diag.service \
            "ranch-dev-diag service absent"
    fi
fi

echo
echo "Result: $pass passed, $fail failed"
[[ "$fail" -eq 0 ]]
