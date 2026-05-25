"""Factory provisioner for Gate Monitor SD cards.

Flashes the golden gate-client image to a target block device, then mounts
the boot partition and injects a unique device ID and Fernet key. The
operator records both on a product sticker so the customer can pair the
gate with their Base Station via the captive portal.

Runs on Linux (/dev/sdX) or macOS (/dev/diskN). On macOS the script
auto-unmounts any auto-mounted partitions, uses the raw block device
(/dev/rdiskN) for ~10x faster `dd`, waits for the boot partition to
auto-mount under /Volumes/, and ejects when done.
"""

import argparse
import base64
import os
import plistlib
import secrets
import subprocess
import sys
import time
from pathlib import Path

from factory_sticker import print_sticker

DEFAULT_PROD_IMAGE = Path("./releases/gate_client_pi0w.img")
DEFAULT_DEV_IMAGE = Path("./releases/gate_client_pi0w_dev.img")
LINUX_MOUNT_POINT = Path("/mnt/pi_boot")
INVENTORY_FILE = Path("manufacturing_inventory.csv")
INVENTORY_HEADER = "timestamp,device_type,device_id,secret_key\n"
# Visually-unambiguous alphabet for the random suffix on gate IDs. I/1 and
# O/0 routinely get confused when an operator squints at a sticker under
# bad lighting at a remote gate enclosure; dropping all four characters
# (and only those four) keeps the keyspace large while making the sticker
# readable.
#
# Keyspace math:
#   old: 36^4 = 1.68M ≈ 20.7 bits — birthday collision at ~1.3k gates
#   new: 32^6 = 1.07B ≈ 30.0 bits — birthday collision at ~32k gates
# The extra two characters more than compensate for the slimmer alphabet.
GATE_ID_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 32 chars, no I/O/0/1
GATE_ID_LENGTH = 6

IS_MACOS = sys.platform == "darwin"
MACOS_MOUNT_TIMEOUT_SECONDS = 30


def generate_gate_id() -> str:
    suffix = "".join(secrets.choice(GATE_ID_ALPHABET) for _ in range(GATE_ID_LENGTH))
    return f"GATE-{suffix}"


def generate_fernet_key() -> str:
    """Mint a Fernet-format key without importing the cryptography package.

    `cryptography.fernet.Fernet.generate_key()` is literally this: 32 random
    bytes URL-safe base64 encoded. Doing it ourselves keeps the operator's
    laptop free of a pip-install step — actual encrypt/decrypt happens on
    the gate, where Buildroot has installed the real library.
    """
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


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


def append_inventory(device_type: str, device_id: str, secret_key: str) -> None:
    new_file = not INVENTORY_FILE.exists()
    fd = os.open(
        INVENTORY_FILE,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        0o600,
    )
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        if new_file:
            handle.write(INVENTORY_HEADER)
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        handle.write(f"{timestamp},{device_type},{device_id},{secret_key}\n")
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


def provision_sd_card(target_device: str, image_path: Path) -> None:
    if not image_path.exists():
        sys.exit(f"Golden image not found at {image_path}")

    print("=== Starting Assembly Line for Gate Monitor ===")

    if IS_MACOS:
        disk_device, raw_device = macos_disk_pair(target_device)
        boot_partition = f"{disk_device}s1"
        write_target = raw_device
        run(["diskutil", "unmountDisk", disk_device])
    else:
        disk_device = target_device
        boot_partition = f"{target_device}1"
        write_target = target_device

    print("[1/5] Flashing golden image...")
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

    print("[2/5] Generating cryptographic credentials...")
    gate_id = generate_gate_id()
    secret_key = generate_fernet_key()
    print(f"  -> Assigned ID: {gate_id}")

    print("[3/5] Mounting boot partition...")
    if IS_MACOS:
        mount_point = macos_wait_for_boot_mount(boot_partition)
    else:
        run(["sudo", "mkdir", "-p", str(LINUX_MOUNT_POINT)])
        run(["sudo", "mount", "-t", "vfat", boot_partition, str(LINUX_MOUNT_POINT)])
        mount_point = LINUX_MOUNT_POINT

    try:
        print("[4/5] Writing gate_config.env...")
        config_path = mount_point / "gate_config.env"
        # DEVICE_ID is consumed by /usr/bin/ranch-set-hostname at early boot
        # so the gate names itself GATE-XXXX on whichever network it sees.
        # We keep GATE_ID too because gate_client.py reads that env var.
        contents = (
            f"DEVICE_ID={gate_id}\n"
            f"GATE_ID={gate_id}\n"
            f"LORA_SECRET_KEY={secret_key}\n"
        )
        tee = subprocess.run(
            ["sudo", "tee", str(config_path)],
            input=contents,
            text=True,
            capture_output=True,
        )
        if tee.returncode != 0:
            sys.exit(f"Failed to write config: {tee.stderr}")
        run(["sudo", "chmod", "600", str(config_path)])
    finally:
        print("[5/5] Unmounting...")
        if IS_MACOS:
            run(["diskutil", "eject", disk_device], allow_failure=True)
        else:
            run(["sudo", "umount", str(LINUX_MOUNT_POINT)], allow_failure=True)

    append_inventory("GATE_MONITOR", gate_id, secret_key)

    print("\nSUCCESS! Gate provisioned.")
    print_sticker(
        ("Device ID",  gate_id),
        ("Secret Key", secret_key),
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Provision a Gate Monitor SD card.")
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
    provision_sd_card(args.target_device, args.image)


if __name__ == "__main__":
    main(sys.argv[1:])
