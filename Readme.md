# LoRa Ranch Sentinel

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
| **Gate Monitor** | Raspberry Pi Zero W + LoRa radio + magnetic contact sensor | Sits at the gate, on its own battery / solar. Sends encrypted "OPEN" / "CLOSED" packets over LoRa. |
| **Base Station** | Raspberry Pi 3B+ + LoRa radio | Lives inside, on the operator's Wi-Fi. Decrypts gate packets, persists them to SQLite, forwards alerts to Telegram. |
| **Telegram** | The operator's existing app | Receives alerts, runs commands like `/open GATE-A1B2` or `/status`. |

The system is designed for a small ranch deployment — one base station,
one to a handful of gates, one operator (or a small group sharing a
Telegram chat).

## Architecture

```
       ─── No cellular at the gate ───       ─── Operator's home Wi-Fi ───

    ┌─────────────────────┐    LoRa     ┌─────────────────────┐    HTTPS
    │   Gate Monitor      │ ◀────────▶  │   Base Station      │ ───────▶  Telegram
    │   (Pi Zero W)       │  Fernet-    │   (Pi 3B+)          │   Bot API
    │                     │  encrypted  │                     │
    │   • Magnetic sensor │  JSON with  │   • LoRa receiver   │   ───────▶  Operator's
    │   • Optional relay  │  monotonic  │   • SQLite event log│             phone
    │   • Persisted seq   │  seq + TTL  │   • Captive portal  │
    └─────────────────────┘             │   • Watchdog        │
                                        └─────────────────────┘
```

Each gate is paired with the base station once — either at setup time
via the captive portal, or any time after via the Telegram `/pair`
command — and from then on the base station only accepts packets that
decrypt under that gate's key and advance its per-gate sequence
counter. The Telegram bot is the runtime control plane: after the
initial captive-portal credential entry, everything the operator does
(adding a gate, opening a gate, renaming a gate, factory-resetting the
device) happens by sending the bot a slash command.

## What's verified today

- ✅ **Base station setup end-to-end**: clean flash → boot → captive
  portal at `http://10.42.0.1/` → operator submits Wi-Fi + Telegram
  credentials → device joins home network → clock syncs → Telegram
  online ping arrives. Tested across many cold-boot cycles.
- ✅ **Wi-Fi watchdog**: 30 minutes of no upstream connectivity
  auto-flips the device back into setup mode. Requires *both*
  NetworkManager reporting connected *and* a TCP probe to `1.1.1.1:53`
  succeeding — defends against the "associated but no internet" case
  that single-signal checks miss.
- ✅ **Crypto path**: per-gate Fernet keys, 30-second TTL on the base
  decrypt, monotonic per-gate sequence counter persisted on the gate.
  Replay attempts are rejected and logged.
- ✅ **Build pipeline**: reproducible Docker build, remote-SSH driven
  builds with hook support, `verify_image.sh` runs ~80 invariant
  checks against every built image before flash.
- ✅ **Telegram command channel** (`/pair`, `/unpair`, `/rename`,
  `/status`, `/help`, `/confirm`, `/cancel`): unit-tested with 111
  stdlib-unittest tests; works end-to-end against a mocked
  transport.

⚠️ **Not yet exercised against real hardware** — the LoRa-driving
commands (`/open`, `/close`, `/status GATE-XXXX`) and the gate side
itself. The wire code is in place; the next milestone is end-to-end
validation against a real gate.

## Repository layout

```
ranch_os/                Buildroot external tree (custom OS layer)
  configs/               Buildroot config fragments (gate / base / dev)
  package/
    gate-client/         Gate Monitor application + systemd unit
    base-station/        Base Station application, captive portal, watchdog
  rootfs-overlay/        Files rsync'd over the rootfs at build time
  boot/                  Pi firmware config.txt for each board

buildroot/               Upstream Buildroot, as a submodule
Dockerfile               Reproducible build container
build.sh                 In-container build orchestration
run_build.sh             Run the build in a local Docker container
remote_build.sh          Drive the build on a remote machine over SSH

scripts/
  remote_build_inner.sh  Remote-side half of remote_build.sh
  verify_image.sh        Invariant checks against a built .img
  measure_image.sh       Rootfs size measurement
  check_factory_deps.py  Asserts factory scripts are stdlib-only
  run_tests.sh           Discovers and runs the unit test suite

tests/                   Stdlib-unittest suite — 111 tests, ~7s end-to-end
.githooks/pre-commit     Runs factory-deps + unit tests on every commit

flash_base_station.py    Flash + portal-credentials inject (Base Station)
provision_gate.py        Flash + Fernet-key inject (Gate Monitor)
factory_sticker.py       Shared sticker-rendering helper

docs/
  USER_GUIDE.md          ⭐ Start here if you just want to use the system
  TELEGRAM.md            Every Telegram command, with example replies
  BUILDING.md            All the ways to build, flash, and test

Todo.md                  Live backlog
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

### I want to build the OS images myself
→ **[docs/BUILDING.md](docs/BUILDING.md)** covers the Docker build,
remote-SSH build, build profiles (production vs development),
flashing, and the test suite.

### I'm picking up development mid-stream
→ Read [BUILDING.md](docs/BUILDING.md) for the build pipeline,
[tests/README.md](tests/README.md) for the test layout, and the
inline comments in `ranch_os/package/base-station/base_station.py`
for the non-obvious wiring (NTP, Wi-Fi watchdog, Telegram command
channel). The Buildroot package `.mk` files and the systemd unit
files in `ranch_os/package/` are the load-bearing OS-integration
seams.

## Security at a glance

- **LoRa traffic** is authenticated and encrypted with a per-gate
  Fernet key. Replay-protected via a monotonic seq counter
  (persisted on the gate, accepted-only-if-advances on the base)
  and a 30-second TTL on the Fernet decrypt.
- **Relay actuation** (planned `/open`, `/close`) requires a
  single-use, 15-second-lifetime challenge nonce from the gate. A
  captured `command` packet replayed by anyone other than the
  legitimate base station fails the nonce check.
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
- **Manufacturing inventory** (device IDs + portal passwords + Fernet
  keys) lives in a single `manufacturing_inventory.csv` with mode
  0600 and is gitignored. Never commit it; never upload it.

Full threat model with attacker scenarios is in
[docs/TELEGRAM.md § Security](docs/TELEGRAM.md#security).

## Status

This is a working hobby-grade system for a single ranch with one
operator. The base-station path is validated on real hardware
end-to-end; the gate side and the LoRa command path are
code-complete and unit-tested but have not yet been exercised
against real radios. The next milestone is on the Todo list.

## License

[MIT](LICENSE)
