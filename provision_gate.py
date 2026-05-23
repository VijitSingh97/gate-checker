"""Factory provisioner for Gate Monitor SD cards.

Flashes the golden gate-client image to a target block device, then mounts
the boot partition and injects a unique device ID and Fernet key. The
operator records both on a product sticker so the customer can pair the
gate with their Base Station via the captive portal.
"""

import argparse
import os
import secrets
import string
import subprocess
import sys
import time
from pathlib import Path

from cryptography.fernet import Fernet

IMAGE_PATH = Path("./releases/gate_client_pi0w.img")
MOUNT_POINT = Path("/mnt/pi_boot")
INVENTORY_FILE = Path("manufacturing_inventory.csv")
INVENTORY_HEADER = "timestamp,device_type,device_id,secret_key\n"
GATE_ID_ALPHABET = string.ascii_uppercase + string.digits
GATE_ID_LENGTH = 4


def generate_gate_id() -> str:
    suffix = "".join(secrets.choice(GATE_ID_ALPHABET) for _ in range(GATE_ID_LENGTH))
    return f"GATE-{suffix}"


def run(argv: list[str], *, allow_failure: bool = False) -> subprocess.CompletedProcess:
    result = subprocess.run(argv, text=True, capture_output=True)
    if result.returncode != 0 and not allow_failure:
        sys.stderr.write(f"Command failed ({' '.join(argv)}): {result.stderr}\n")
        sys.exit(1)
    return result


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


def provision_sd_card(target_device: str) -> None:
    if not IMAGE_PATH.exists():
        sys.exit(f"Golden image not found at {IMAGE_PATH}")

    print("=== Starting Assembly Line for Gate Monitor ===")

    print("[1/5] Flashing golden image...")
    run(
        [
            "sudo", "dd",
            f"if={IMAGE_PATH}",
            f"of={target_device}",
            "bs=4M",
            "conv=fsync",
            "status=progress",
        ]
    )

    print("[2/5] Generating cryptographic credentials...")
    gate_id = generate_gate_id()
    secret_key = Fernet.generate_key().decode("utf-8")
    print(f"  -> Assigned ID: {gate_id}")

    print("[3/5] Mounting boot partition...")
    run(["sudo", "mkdir", "-p", str(MOUNT_POINT)])
    boot_partition = f"{target_device}1"
    fstype = "vfat" if sys.platform.startswith("linux") else "msdos"
    run(["sudo", "mount", "-t", fstype, boot_partition, str(MOUNT_POINT)])

    try:
        print("[4/5] Writing gate_config.env...")
        config_path = MOUNT_POINT / "gate_config.env"
        contents = f"GATE_ID={gate_id}\nLORA_SECRET_KEY={secret_key}\n"
        # tee with sudo so we don't have to shell out for redirection.
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
        run(["sudo", "umount", str(MOUNT_POINT)], allow_failure=True)

    append_inventory("GATE_MONITOR", gate_id, secret_key)

    print("\nSUCCESS! Gate provisioned.")
    print("-" * 50)
    print("PRINT THIS ON THE PRODUCT STICKER:")
    print(f"  Device ID:  {gate_id}")
    print(f"  Secret Key: {secret_key}")
    print("-" * 50)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Provision a Gate Monitor SD card.")
    parser.add_argument("target_device", help="Block device, e.g. /dev/sdX")
    parser.add_argument(
        "--yes", action="store_true", help="Skip the destructive-action confirmation."
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> None:
    args = parse_args(argv)
    if not args.yes:
        confirm = input(
            f"WARNING: this will erase {args.target_device}. Type 'YES' to continue: "
        )
        if confirm != "YES":
            print("Aborted.")
            return
    provision_sd_card(args.target_device)


if __name__ == "__main__":
    main(sys.argv[1:])
