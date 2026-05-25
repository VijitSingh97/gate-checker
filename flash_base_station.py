"""Factory provisioner for Base Station SD cards.

Flashes the golden base-station image, then injects a per-device
PORTAL_PASSWORD onto the boot partition. That password gates the
captive portal's HTTP Basic auth and is printed on the product sticker.

(The first-boot setup AP is intentionally an OPEN Wi-Fi network — Pi 3
brcmfmac firmware reliably fails WPA2 AP-mode handshakes. See the
comment in ranch_os/package/base-station/provision.py
`_start_access_point` for the detailed why. The portal's per-device
admin password is the real auth boundary, not the AP layer.)

Runs on Linux (/dev/sdX) or macOS (/dev/diskN). On macOS the script
auto-unmounts any auto-mounted partitions, uses the raw block device
(/dev/rdiskN) for ~10x faster `dd`, waits for the boot partition to
auto-mount under /Volumes/, and ejects when done.
"""

import argparse
import os
import plistlib
import secrets
import string
import subprocess
import sys
import time
from pathlib import Path

from factory_sticker import print_sticker

DEFAULT_PROD_IMAGE = Path("./releases/base_station_pi3.img")
DEFAULT_DEV_IMAGE = Path("./releases/base_station_pi3_dev.img")
LINUX_MOUNT_POINT = Path("/mnt/pi_boot")
INVENTORY_FILE = Path("manufacturing_inventory.csv")
INVENTORY_HEADER = "timestamp,device_type,device_id,secret_key\n"

PASSWORD_ALPHABET = string.ascii_letters + string.digits
PORTAL_PASSWORD_LENGTH = 16
# 62-char alphabet × 16 characters ⇒ log2(62^16) ≈ 95.3 bits of entropy.
# That's why provision.py deliberately doesn't rate-limit failed Basic-auth
# attempts: at this strength, brute force is intractable inside any
# realistic setup-AP window, and the lockout UX bug (an operator who fat-
# fingers their sticker password twice and gets locked out with no way
# back in) would be the more common failure than an actual attack.
#
# If you ever shrink the alphabet (e.g. drop ambiguous I/O/0/1 to make
# stickers easier to read — see Todo) OR cut the length below ~12, the
# math no longer holds and provision.py:_requires_auth needs to grow a
# real rate limiter before you ship.

IS_MACOS = sys.platform == "darwin"
MACOS_MOUNT_TIMEOUT_SECONDS = 30


def generate_password(length: int) -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


def run(argv: list[str], *, allow_failure: bool = False) -> subprocess.CompletedProcess:
    result = subprocess.run(argv, text=True, capture_output=True)
    if result.returncode != 0 and not allow_failure:
        sys.stderr.write(f"Command failed ({' '.join(argv)}): {result.stderr}\n")
        sys.exit(1)
    return result


def _drop_to_invoking_user(path: Path) -> None:
    """When run via `sudo`, hand the file back to the real user.

    Without this, files we create end up root-owned mode 0600 and the
    operator can't even read them afterwards. Worse, `rsync` and other
    user-context tools choke on them. SUDO_UID/SUDO_GID are set by sudo
    to identify who invoked it.
    """
    sudo_uid = os.environ.get("SUDO_UID")
    sudo_gid = os.environ.get("SUDO_GID")
    if sudo_uid and sudo_gid:
        try:
            os.chown(path, int(sudo_uid), int(sudo_gid))
        except OSError as exc:
            sys.stderr.write(f"Warning: could not chown {path}: {exc}\n")


def append_inventory(device_id: str, portal_password: str) -> None:
    new_file = not INVENTORY_FILE.exists()
    fd = os.open(INVENTORY_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        if new_file:
            handle.write(INVENTORY_HEADER)
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        handle.write(
            f"{timestamp},BASE_STATION,{device_id},portal={portal_password}\n"
        )
    _drop_to_invoking_user(INVENTORY_FILE)


def macos_disk_pair(device: str) -> tuple[str, str]:
    """Return (cooked, raw) device paths regardless of which the user passed."""
    if device.startswith("/dev/rdisk"):
        return device.replace("/dev/rdisk", "/dev/disk"), device
    if device.startswith("/dev/disk"):
        return device, device.replace("/dev/disk", "/dev/rdisk")
    sys.exit(f"On macOS, expected /dev/diskN or /dev/rdiskN, got {device}")


def macos_wait_for_boot_mount(boot_partition: str) -> Path:
    """Poll diskutil until macOS auto-mounts the boot partition."""
    deadline = time.monotonic() + MACOS_MOUNT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["diskutil", "info", "-plist", boot_partition],
            capture_output=True,
        )
        if result.returncode == 0:
            info = plistlib.loads(result.stdout)
            mount_point = info.get("MountPoint", "")
            if mount_point and Path(mount_point).is_dir():
                return Path(mount_point)
        time.sleep(1)
    sys.exit(f"Timed out waiting for {boot_partition} to auto-mount on macOS")


def flash_sd_card(target_device: str, image_path: Path) -> None:
    if not image_path.exists():
        sys.exit(f"Base station image not found at {image_path}")

    print("=== Starting Assembly Line for Base Station ===")

    if IS_MACOS:
        disk_device, raw_device = macos_disk_pair(target_device)
        boot_partition = f"{disk_device}s1"
        write_target = raw_device
        # Release any partitions Finder/Spotlight auto-mounted on insertion.
        run(["diskutil", "unmountDisk", disk_device])
    else:
        disk_device = target_device
        boot_partition = f"{target_device}1"
        write_target = target_device

    print("[1/3] Flashing golden image...")
    run(
        [
            "sudo", "dd",
            f"if={image_path}",
            f"of={write_target}",
            "bs=4M",
            "conv=fsync",
            "status=progress",
        ]
    )

    print("[2/3] Generating per-device credentials...")
    device_id = f"BASE-{secrets.token_hex(2).upper()}"
    portal_password = generate_password(PORTAL_PASSWORD_LENGTH)

    print("[3/3] Injecting credentials into boot partition...")
    if IS_MACOS:
        # macOS will re-mount the FAT32 partition the moment dd finishes
        # writing a valid filesystem. We just have to wait for it.
        mount_point = macos_wait_for_boot_mount(boot_partition)
    else:
        run(["sudo", "mkdir", "-p", str(LINUX_MOUNT_POINT)])
        run(["sudo", "mount", "-t", "vfat", boot_partition, str(LINUX_MOUNT_POINT)])
        mount_point = LINUX_MOUNT_POINT

    try:
        creds_path = mount_point / "provision_creds.env"
        # DEVICE_ID is consumed by /usr/bin/ranch-set-hostname at early boot
        # so the device names itself BASE-XXXX on your home network — much
        # easier to spot in your router's DHCP table than "buildroot".
        contents = (
            f"DEVICE_ID={device_id}\n"
            f"PORTAL_PASSWORD={portal_password}\n"
        )
        tee = subprocess.run(
            ["sudo", "tee", str(creds_path)],
            input=contents,
            text=True,
            capture_output=True,
        )
        if tee.returncode != 0:
            sys.exit(f"Failed to write provision creds: {tee.stderr}")
        run(["sudo", "chmod", "600", str(creds_path)])
    finally:
        if IS_MACOS:
            run(["diskutil", "eject", disk_device], allow_failure=True)
        else:
            run(["sudo", "umount", str(LINUX_MOUNT_POINT)], allow_failure=True)

    append_inventory(device_id, portal_password)

    print("\nSUCCESS! Base Station image flashed.")
    print_sticker(
        ("Device ID",    device_id),
        ("Setup Wi-Fi",  "BaseStation_Setup"),
        ("Portal URL",   "http://10.42.0.1/"),
        ("Portal Login", f"admin / {portal_password}"),
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Provision a Base Station SD card.")
    parser.add_argument(
        "target_device",
        help="Block device, e.g. /dev/sdX (Linux) or /dev/diskN (macOS)",
    )

    image_group = parser.add_mutually_exclusive_group()
    image_group.add_argument(
        "--image",
        type=Path,
        help=f"Path to the .img file to flash (default: {DEFAULT_PROD_IMAGE}).",
    )
    image_group.add_argument(
        "--dev",
        action="store_true",
        help=f"Shortcut for --image {DEFAULT_DEV_IMAGE} (development build "
             "with SSH + debug tools — never deploy to customers).",
    )

    parser.add_argument(
        "--yes", action="store_true", help="Skip the destructive-action confirmation."
    )
    args = parser.parse_args(argv)
    if args.dev:
        args.image = DEFAULT_DEV_IMAGE
    elif args.image is None:
        args.image = DEFAULT_PROD_IMAGE
    return args


def main(argv: list[str]) -> None:
    args = parse_args(argv)
    if not args.yes:
        confirm = input(
            f"WARNING: this will erase {args.target_device}. Type 'YES' to continue: "
        )
        if confirm != "YES":
            print("Aborted.")
            return
    flash_sd_card(args.target_device, args.image)


if __name__ == "__main__":
    main(sys.argv[1:])
