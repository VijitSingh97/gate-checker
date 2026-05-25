# LoRa Ranch Sentinel

[![tests](https://github.com/VijitSingh97/gate-checker/actions/workflows/tests.yml/badge.svg)](https://github.com/VijitSingh97/gate-checker/actions/workflows/tests.yml)
[![Latest release](https://img.shields.io/github/v/release/VijitSingh97/gate-checker?display_name=tag&sort=semver)](https://github.com/VijitSingh97/gate-checker/releases/latest)
[![Release date](https://img.shields.io/github/release-date/VijitSingh97/gate-checker)](https://github.com/VijitSingh97/gate-checker/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/VijitSingh97/gate-checker/total)](https://github.com/VijitSingh97/gate-checker/releases)
![Base](https://img.shields.io/badge/base-Pi%203B%2B-c51a4a)
![Gate](https://img.shields.io/badge/gate-Pi%20Zero%20W%20%2F%202W-c51a4a)
[![License: MIT](https://img.shields.io/github/license/VijitSingh97/gate-checker)](LICENSE)

A self-hosted gate monitoring system for properties where running power
or pulling cellular service to every gate isn't realistic. A small
Raspberry Pi at each gate watches a magnetic contact sensor and reports
state changes over **LoRa** — a long-range, low-power radio — to a base
station inside the operator's home that bridges the alerts to
**Telegram**.

No cellular at the gate. No cloud account to maintain. The whole
control plane is a Telegram chat the operator already has on their
phone.

---

## What's in the box

| Device | Hardware | Role |
| --- | --- | --- |
| **Gate Monitor** | Raspberry Pi Zero W / Zero 2 W + LoRa radio + magnetic contact sensor | Sits at the gate, on its own battery / solar. Sends encrypted "OPEN" / "CLOSED" packets over LoRa. |
| **Base Station** | Raspberry Pi 3B+ + LoRa radio | Lives inside, on the operator's Wi-Fi. Decrypts gate packets, persists them to SQLite, forwards alerts to Telegram. |
| **Telegram** | The operator's existing app | Receives alerts, runs commands like `/status`, `/pair`, `/open GATE-A1B2`. |

The system is designed for a small ranch deployment — one base station,
one to a handful of gates, one operator (or a small group sharing a
Telegram chat).

## Architecture

```
       ─── No cellular at the gate ───       ─── Operator's home Wi-Fi ───

    ┌─────────────────────┐    LoRa     ┌─────────────────────┐    HTTPS
    │   Gate Monitor      │ ◀────────▶  │   Base Station      │ ───────▶  Telegram
    │   (Pi Zero W/2W)    │  Fernet-    │   (Pi 3B+)          │   Bot API
    │                     │  encrypted  │                     │
    │   • Magnetic sensor │  JSON with  │   • LoRa receiver   │   ───────▶  Operator's
    │   • Optional relay  │  per-gate   │   • SQLite event log│             phone
    │   • Persisted seq   │  seq        │   • Captive portal  │
    └─────────────────────┘             │   • Watchdog        │
                                        └─────────────────────┘
```

Each gate is paired with the base station once — over the Telegram
`/pair` command after the operator has finished the captive-portal
setup — and from then on the base station only accepts packets that
decrypt under that gate's key and advance its per-gate sequence
counter. The Telegram bot is the runtime control plane: every
operator action after the initial captive-portal credential entry
(adding a gate, querying state, opening or closing a gate, renaming,
factory-resetting the device) happens by sending the bot a slash
command.

## What's verified today

- ✅ **Base station setup end-to-end**: clean flash → boot → captive
  portal at `http://10.42.0.1/` → operator submits Wi-Fi + Telegram
  credentials → device joins home network → clock syncs → Telegram
  online ping arrives (with device ID, e.g. `BASE-9A22`). Tested
  across many cold-boot cycles.
- ✅ **Wi-Fi watchdog**: 30 minutes of no upstream connectivity
  auto-flips the device back into setup mode. Requires *both*
  NetworkManager reporting connected *and* a TCP probe to `1.1.1.1:53`
  succeeding — defends against the "associated but no internet" case
  that single-signal checks miss.
- ✅ **Gate alert path on real hardware**: magnetic reed switch on
  a Pi Zero 2 W → LoRa frame to the base → Fernet decrypt → seq
  check → Telegram message with 🔓 / 🔒 emoji prefix.
- ✅ **Telegram command channel** on production-profile images:
  `/help`, `/status`, `/status GATE-XXXX` (live LoRa state query),
  `/pair`, `/unpair`, `/rename`, `/factory_reset`, `/confirm`,
  `/cancel`. State-change dedup so a `/status GATE-X` reply doesn't
  also fire an unsolicited Telegram message.
- ✅ **Build pipeline**: reproducible Docker build, remote-SSH driven
  builds with hook support, `verify_image.sh` runs ~80 invariant
  checks against every built image — including a production-profile
  block that fails the build if any dev-only artifact (dropbear,
  debug tools, gate-side networkd) leaks into a shippable image.
- ✅ **Gate actuation on real hardware**: `/open` and `/close` drive
  a relay wired to the gate's `RELAY_GPIO` pin (default BCM 17), the
  reed switch reports the new state back, and the base records the
  cycle duration into its adaptive grace-period buffer so subsequent
  commands wait against a threshold learned from that gate's actual
  mechanical behavior.
- ✅ **Unit suite**: 165 stdlib-`unittest` tests, ~9s end-to-end via
  `scripts/run_tests.sh`. Pre-commit hook runs it on every commit.

## Repository layout

```
ranch_os/                       Buildroot external tree (custom OS layer)
  configs/
    base.fragment                  Pi 3 production config
    gate.fragment                  Pi Zero W production config
    dev.fragment                   SSH + root password + debug tools (gate+base)
    dev-gate.fragment              Gate-only dev: systemd-networkd for USB-eth
  package/
    gate-client/                Gate Monitor application + systemd unit
    base-station/              Base Station application, captive portal, watchdog
  rootfs-overlay/             Files rsync'd over the rootfs at build time
  rootfs-overlay-gate-dev/   Dev-only gate overlay: .network unit, dev-diag
  boot/                       Pi firmware config.txt for each board

buildroot/                  Upstream Buildroot, as a submodule
Dockerfile                  Reproducible build container
build.sh                    In-container build orchestration
run_build.sh                Run the build in a local Docker container
remote_build.sh             Drive the build on a remote machine over SSH

scripts/
  setup_remote_builder.sh    Bootstrap a fresh Debian/Ubuntu host as a builder
  remote_build_inner.sh      Remote-side half of remote_build.sh
  verify_image.sh            Invariant checks against a built .img
  measure_image.sh           Rootfs size measurement
  check_factory_deps.py      Asserts factory scripts are stdlib-only
  run_tests.sh               Discovers and runs the unit test suite

tests/                       Stdlib-unittest suite — 130 tests, ~8s end-to-end
.githooks/pre-commit         Runs factory-deps + unit tests on every commit

flash_base_station.py        Flash + portal-credentials inject (Base Station)
provision_gate.py            Flash + Fernet-key inject (Gate Monitor)
factory_sticker.py           Shared sticker-rendering helper

docs/
  USER_GUIDE.md              ⭐ Start here if you just want to use the system
  TELEGRAM.md                Every Telegram command, with example replies
  BUILDING.md                All the ways to build, flash, and test
```

## Quick start

### I just received my devices and want to set them up
→ **[docs/USER_GUIDE.md](docs/USER_GUIDE.md)** walks through unboxing,
plugging the base station in, finding the setup Wi-Fi, entering
credentials, and pairing each gate over Telegram.

### I want to send a command to my bot
→ **[docs/TELEGRAM.md](docs/TELEGRAM.md)** is the per-command
reference with syntax, examples, and expected replies for every
command the base station understands.

### I want to flash a pre-built image
→ The **[Releases page](https://github.com/VijitSingh97/gate-checker/releases/latest)**
hosts production `.img` files for the base station and the gate,
plus a `SHA256SUMS` file so you can verify what you downloaded
before flashing. Each release lists the gates and base versions that
shipped together.

### I want to build the OS images myself
→ **[docs/BUILDING.md](docs/BUILDING.md)** covers the Docker build,
the remote-SSH build, build profiles (production vs development),
flashing, and the test suite.

### I want to cut and publish a new release
→ **[docs/RELEASING.md](docs/RELEASING.md)** walks through the
build-verify-tag-publish dance. The `scripts/cut_release.sh`
helper handles the mechanical bits (verify, hash, draft).

### I want to set up a fresh build server
→ `scripts/setup_remote_builder.sh --host <ip> --user <name>` —
copies your SSH key, installs Docker + the loop-mount utilities,
adds your user to the `docker` group, writes a passwordless-sudo
fragment for the build commands. One-shot Debian/Ubuntu provisioner.

### I'm picking up development mid-stream
→ Read [BUILDING.md](docs/BUILDING.md) for the build pipeline,
[tests/README.md](tests/README.md) for the test layout, and the
inline comments in `ranch_os/package/base-station/base_station.py`
for the non-obvious wiring (NTP, Wi-Fi watchdog, Telegram command
channel, replay protection). The Buildroot package `.mk` files
and the systemd unit files in `ranch_os/package/` are the
load-bearing OS-integration seams.

## Security at a glance

- **LoRa traffic** is authenticated and encrypted with a per-gate
  Fernet key. Replay-protected via a monotonic seq counter persisted
  on the gate (accepted-only-if-advances on the base). The base does
  NOT enforce a wall-clock Fernet TTL — the gate has no RTC and no
  NTP path, so its clock starts at the kernel build epoch and a TTL
  check would reject every packet from a cold-booted gate. The seq
  counter is the real replay defense.
- **Relay actuation** (`/open`, `/close`) requires a single-use,
  15-second-lifetime challenge nonce from the gate. A captured
  `command` packet replayed by anyone other than the legitimate base
  station fails the nonce check.
- **Per-device portal password** for the captive portal HTTP Basic
  auth — 16 chars × 62-char alphabet ≈ 95 bits of entropy, generated
  at flash time, printed on the product sticker, never reused.
- **Per-device gate Fernet key** generated at flash time, written to
  FAT32 by the factory provisioner, then migrated to ext4 mode-0600
  by a oneshot systemd service on first boot — so the "stolen SD
  card briefly held in a Windows machine" attack only works in the
  narrow window before the first boot.
- **Telegram bot token**: stored in `/var/lib/base_station/base_config.env`
  (mode 0600 on ext4, never on FAT32). Redacted from all logged
  exception messages via a token-shaped regex so journal exports
  can't leak it.
- **Telegram command authorization**: the configured chat ID is the
  auth boundary. Whoever the operator invited to that chat can issue
  every command. The bot rejects messages from any other chat. There
  is intentionally no per-user allow-list — the operator chooses
  chat composition.
- **Production vs dev image separation**: `verify_image.sh` runs a
  production-profile-only block that fails the build if any dev
  artifact (SSH server, debug tools, gate-side networkd, dev-diag
  script) leaks into a non-`_dev` image. Dev images are tagged with
  a `_dev` filename suffix and carry a known root password so they're
  never confused for shippable artifacts.
- **Manufacturing inventory** (device IDs + portal passwords + Fernet
  keys) lives in a single `manufacturing_inventory.csv` with mode
  0600 and is gitignored. Never commit it; never upload it.

Full threat model with attacker scenarios is in
[docs/TELEGRAM.md § Security](docs/TELEGRAM.md#security).

## Status

Production-validated end-to-end on real hardware — alerts, every
Telegram command (`/pair`, `/unpair`, `/rename`, `/status`,
`/status GATE-X`, `/open`, `/close`, `/factory_reset`, `/confirm`,
`/cancel`, `/help`), the captive-portal setup flow, the Wi-Fi
watchdog, and production-profile image hygiene. Working hobby-grade
system for a single ranch with one operator.

## License

[MIT](LICENSE)
