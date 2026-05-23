"""Factory provisioner for Base Station SD cards.

Flashes the golden base-station image, then injects a per-device pair of
credentials onto the boot partition:

  - AP_PASSWORD     — protects the Wi-Fi access point that the captive
                      portal serves during first-time setup.
  - PORTAL_PASSWORD — gates HTTP Basic auth on the captive portal itself.

Both are printed for the operator to place on the product sticker.
"""

import argparse
import os
import secrets
import string
import subprocess
import sys
import time
from pathlib import Path

IMAGE_PATH = Path("./releases/base_station_pi3.img")
MOUNT_POINT = Path("/mnt/pi_boot")
INVENTORY_FILE = Path("manufacturing_inventory.csv")
INVENTORY_HEADER = "timestamp,device_type,device_id,secret_key\n"

PASSWORD_ALPHABET = string.ascii_letters + string.digits
AP_PASSWORD_LENGTH = 12
PORTAL_PASSWORD_LENGTH = 16


def generate_password(length: int) -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


def run(argv: list[str], *, allow_failure: bool = False) -> subprocess.CompletedProcess:
    result = subprocess.run(argv, text=True, capture_output=True)
    if result.returncode != 0 and not allow_failure:
        sys.stderr.write(f"Command failed ({' '.join(argv)}): {result.stderr}\n")
        sys.exit(1)
    return result


def append_inventory(device_id: str, ap_password: str, portal_password: str) -> None:
    new_file = not INVENTORY_FILE.exists()
    fd = os.open(INVENTORY_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        if new_file:
            handle.write(INVENTORY_HEADER)
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        handle.write(
            f"{timestamp},BASE_STATION,{device_id},ap={ap_password};portal={portal_password}\n"
        )


def flash_sd_card(target_device: str) -> None:
    if not IMAGE_PATH.exists():
        sys.exit(f"Base station image not found at {IMAGE_PATH}")

    print("=== Starting Assembly Line for Base Station ===")

    print("[1/3] Flashing golden image...")
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

    print("[2/3] Generating per-device credentials...")
    device_id = f"BASE-{secrets.token_hex(2).upper()}"
    ap_password = generate_password(AP_PASSWORD_LENGTH)
    portal_password = generate_password(PORTAL_PASSWORD_LENGTH)

    print("[3/3] Injecting credentials into /boot...")
    run(["sudo", "mkdir", "-p", str(MOUNT_POINT)])
    boot_partition = f"{target_device}1"
    fstype = "vfat" if sys.platform.startswith("linux") else "msdos"
    run(["sudo", "mount", "-t", fstype, boot_partition, str(MOUNT_POINT)])

    try:
        creds_path = MOUNT_POINT / "provision_creds.env"
        contents = (
            f"AP_PASSWORD={ap_password}\n"
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
        run(["sudo", "umount", str(MOUNT_POINT)], allow_failure=True)

    append_inventory(device_id, ap_password, portal_password)

    print("\nSUCCESS! Base Station image flashed.")
    print("-" * 50)
    print("PRINT THIS ON THE PRODUCT STICKER:")
    print(f"  Device ID:        {device_id}")
    print(f"  Setup Wi-Fi:      BaseStation_Setup")
    print(f"  Wi-Fi Password:   {ap_password}")
    print(f"  Setup Login:      admin / {portal_password}")
    print("-" * 50)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Provision a Base Station SD card.")
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
    flash_sd_card(args.target_device)


if __name__ == "__main__":
    main(sys.argv[1:])
