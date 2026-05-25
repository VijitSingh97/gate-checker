# Security policy

## Reporting a vulnerability

If you believe you've found a security issue in this project, please
report it privately rather than opening a public GitHub issue.

**Email:** vijit.n.singh@gmail.com

Include in your report:

- A description of the issue and the impact you observed (or believe
  is possible).
- Steps to reproduce, ideally with sample input or a small test case.
- The commit hash or release you found it in.
- Whether you've discussed it with anyone else, and any timeline
  constraints on your end.

You should expect an acknowledgement within a few days. I'll work
with you on a fix and coordinate any disclosure timing before
publishing details.

## Scope

In scope for this policy:

- The Python application code in `ranch_os/package/base-station/` and
  `ranch_os/package/gate-client/`.
- The factory provisioner scripts (`flash_base_station.py`,
  `provision_gate.py`, `factory_sticker.py`).
- The build pipeline (`Dockerfile`, `build.sh`, `remote_build.sh`,
  `scripts/`).
- The captive-portal Flask app shipped with the base station.
- The systemd unit files and rootfs-overlay configuration that ship
  with the images.

Out of scope:

- Vulnerabilities in upstream Buildroot, the Linux kernel, systemd,
  Python, the `cryptography` library, NetworkManager, dropbear, or
  any other third-party component pulled in by the build. Report
  those upstream.
- Issues that require local physical access to the SD card (the
  threat model already acknowledges "stolen SD card briefly held in
  a Windows machine" as a covered attack — see the gate-key migration
  to ext4 — but other physical-access attacks like reading the GPIO
  header live are out of scope).
- Anything that requires already-authenticated access to the
  Telegram chat. The chat is the auth boundary by design.

## Already-acknowledged design tradeoffs

The threat model and the explicit non-goals are documented in
[docs/TELEGRAM.md § Security](docs/TELEGRAM.md#security). Specifically:

- The captive portal AP is open (no WPA2) for the ~30 seconds it's
  up; the per-device portal password (≈95 bits) is the actual auth.
- The Fernet key passes through Telegram in cleartext on `/pair`;
  the bot deletes the operator's message immediately, but Telegram's
  own backups may retain it for up to 48 hours.
- Gate IDs are sent in cleartext as the LoRa frame prefix (the
  encrypted payload that follows is the only secret).

Please don't file these as bugs — but if you have a concrete attack
that exploits one of them in a way the docs don't acknowledge, do
report it.
