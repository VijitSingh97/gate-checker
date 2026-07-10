# Building, flashing, and testing

This document covers every way to build, flash, and test the project.
For end-user setup (after you have flashed devices in hand), see
[USER_GUIDE.md](USER_GUIDE.md).

---

## What gets built

Each successful build produces two SD-card images under `releases/`:

| Image | Target hardware | Used for |
| --- | --- | --- |
| `releases/gate_client_pi0w.img` | Raspberry Pi Zero W | Gate Monitor |
| `releases/base_station_pi3.img` | Raspberry Pi 3B+ | Base Station |

Both images come out of the same Buildroot tree; the difference is
config fragments applied at `make olddefconfig` time. Production
images strip SSH and root password; development images add Dropbear
SSH, `tcpdump`, `strace`, `less`, and a known root password
(`ranchdev`). Dev images are tagged with a `_dev` suffix on disk so
they can't be confused with shippable artifacts.

---

## Prerequisites

| What | Why |
| --- | --- |
| Docker | The Buildroot toolchain runs in a containerized environment so every build is reproducible across host distros. |
| `git` with submodule support | `buildroot/` is an upstream submodule. |
| ~30 GB free disk | Buildroot's downloads + ccache + per-target output trees. |
| Linux host *or* a Linux build server | `verify_image.sh` and the loop-mount steps need Linux. macOS can drive `run_build.sh` if you only want the build itself; you'll skip verification or run it on a remote Linux box. |

**First-time clone:**

```bash
git clone --recursive https://github.com/<you>/gate-checker.git
cd gate-checker
docker build -t ranch-builder .
```

---

## Building

There are three ways to drive a build. Pick by environment:

### 1. Local Docker (`./run_build.sh`)

The simplest path — works anywhere Docker runs. Mounts the workspace
into the `ranch-builder` container with caching enabled, runs
`build.sh` inside, and writes images to `./releases/`.

```bash
./run_build.sh
```

Subsequent runs reuse the Buildroot download cache (`dl-cache/`) and
the per-target ccache (`ccache-dir/`), so an incremental change
rebuilds in minutes instead of an hour.

### 2. Remote SSH (`./remote_build.sh`)

For when your build host is more powerful than your dev laptop — or
when you're on macOS and want a Linux build that can run
`verify_image.sh`. `remote_build.sh` rsyncs the workspace to a remote
host, runs the build there, rsyncs images back, and cleans up.

```bash
RANCH_BUILD_HOST=builder.lan \
RANCH_BUILD_USER=ci \
./remote_build.sh
```

Optional environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `RANCH_BUILD_HOST` | (required) | SSH host of the build machine. |
| `RANCH_BUILD_USER` | (required) | SSH login on the build machine. |
| `RANCH_BUILD_DIR` | `/home/$USER/ranch_os_build` | Workspace path on the remote. |
| `RANCH_BUILD_PRE_HOOK` | `true` | Shell command to run on the remote *before* building (e.g. pause a workload). |
| `RANCH_BUILD_POST_HOOK` | `true` | Shell command to run on the remote *after* building (success, failure, or Ctrl-C — fired via an EXIT trap). |
| `RANCH_BUILD_PROFILE` | `production` | `production` or `development`. |
| `RANCH_BUILD_TARGETS` | `gate base` | Space-separated subset. |

Example with hooks (pausing a background workload — here an xmrig
miner — during the build):

```bash
RANCH_BUILD_HOST=builder.lan \
RANCH_BUILD_USER=ci \
RANCH_BUILD_PRE_HOOK='sudo systemctl stop xmrig && sudo systemctl start docker containerd' \
RANCH_BUILD_POST_HOOK='sudo systemctl stop docker.socket docker containerd; sudo systemctl start xmrig' \
./remote_build.sh
```

For passwordless sudo on the hooks, see
[Passwordless sudo on the build host](#passwordless-sudo-on-the-build-host).

### 3. Manual (`docker run`)

Run-build wraps a single `docker run` invocation; if you need
something the wrapper doesn't expose, read `run_build.sh` and adapt.

---

## Build profiles

Profile is selected by `RANCH_BUILD_PROFILE`:

| Profile | Default | Output filenames | What's included |
| --- | --- | --- | --- |
| `production` | yes | `*_pi*.img` | Captive portal + gate/base services. **No SSH. No shell debug tools. No known passwords.** Ship this to operators. |
| `development` | | `*_pi*_dev.img` | Everything in `production` **plus** Dropbear SSH, root password `ranchdev`, `less`, `strace`, `tcpdump`. **Never deploy to a customer.** The root password is in git. |

```bash
RANCH_BUILD_PROFILE=development ./run_build.sh
```

---

## Building one target only

By default the build produces both images. When you're iterating on
one side, skip the other to roughly halve build time:

```bash
RANCH_BUILD_TARGETS=base ./run_build.sh        # base only
RANCH_BUILD_TARGETS=gate ./run_build.sh        # gate only
RANCH_BUILD_TARGETS="gate base" ./run_build.sh # both (default)
```

The same variable works with `./remote_build.sh`. `verify_image.sh`
and `measure_image.sh` auto-skip the target that wasn't built, so the
overall pipeline still finishes green.

---

## Flashing a device

Each device needs a per-device credential injection step on top of the
golden image. The factory scripts handle both in one invocation —
they flash, then mount the boot partition and inject credentials.

```bash
sudo python3 flash_base_station.py /dev/sdY      # Linux, base station
sudo python3 provision_gate.py     /dev/sdX      # Linux, gate

sudo python3 flash_base_station.py /dev/disk6    # macOS, base station
sudo python3 provision_gate.py     /dev/disk6    # macOS, gate
```

On macOS the scripts auto-unmount Finder-mounted partitions, switch
to the raw device (`/dev/rdiskN`) for ~10× faster `dd`, wait for the
boot partition to auto-mount under `/Volumes/`, and eject when done.
On Linux they mount under `/mnt/pi_boot` and unmount at the end.

To flash a development image instead of the default production image:

```bash
sudo python3 flash_base_station.py /dev/disk6 --dev
sudo python3 provision_gate.py     /dev/disk6 --dev
```

`--dev` is a shortcut for `--image releases/<name>_dev.img`. Both
flags are mutually exclusive — use `--image PATH` to flash any
arbitrary `.img` (e.g. an archived release for regression testing).

### What flashing produces

Both factory scripts:

1. Write the golden image to the SD card with `dd`.
2. Mint a per-device ID and the relevant credentials:
   - Base station: 16-char portal password (95 bits of entropy).
   - Gate: 32-byte URL-safe-base64 Fernet key (256 bits).
3. Write those credentials to the FAT32 boot partition. For the
   base, that's `/boot/provision_creds.env` (read by the captive
   portal). For the gate, that's `/boot/gate_config.env`, which a
   first-boot oneshot service moves to ext4 + mode 0600.
4. Append a row to `manufacturing_inventory.csv` (mode 0600,
   gitignored).
5. Print a "PRINT THIS ON THE PRODUCT STICKER" block to stdout. The
   sticker output is the operator-facing source of truth — they need
   the device ID and the secret to either log into the captive portal
   (base) or pair the gate over Telegram (gate).

### Sticker output

Example base-station sticker block:

```
--------------------------------------------------
PRINT THIS ON THE PRODUCT STICKER:
  Device ID:    BASE-AB12
  Setup Wi-Fi:  BaseStation_Setup
  Portal URL:   http://10.42.0.1/
  Portal Login: admin / kJ9wF2pQrXmL5zVn
--------------------------------------------------
```

Example gate sticker block:

```
--------------------------------------------------
PRINT THIS ON THE PRODUCT STICKER:
  Device ID:  GATE-A1B2C3
  Secret Key: gAAAAABl1234567890_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx=
--------------------------------------------------
```

Both blocks come from `factory_sticker.print_sticker()`, which is the
shared helper both scripts use so the two stickers stay visually
aligned and can grow new fields (e.g. QR codes) in one place.

### Back up `manufacturing_inventory.csv`

Both provisioners append a row to `manufacturing_inventory.csv` in
the repo root every time they run. The row carries the device ID,
the portal password (for base stations) or the Fernet key (for
gates), and a timestamp. The file is mode `0600`, gitignored, and
the only off-device record of every key and password the factory
has ever generated.

**Lose this file and you lose access to every paired gate**: the
base station's SQLite has the Fernet key for each gate it's paired
with, but without the inventory you can't re-pair a replacement
base, you can't validate a sticker against the on-disk truth, and
you can't recover the portal login if you ever lose the printed
sticker.

Treat it like a credentials vault:

- Back it up to a password manager (1Password, Bitwarden, etc.) or
  an encrypted file vault (`age`, `gpg`, an encrypted .dmg). Don't
  put it in any cloud-synced folder that you haven't deliberately
  encrypted at rest — most cloud sync clients see the local file
  before any client-side encryption layer.
- After every flash, refresh the backup before moving on.
- Don't email it, don't paste it in chat, don't commit it. The
  `.gitignore` and the mode `0600` will help you, but the only
  reliable defence is operator discipline.
- If you ever believe the file has leaked, the only mitigation is
  to `/factory_reset` every base station and re-flash every gate
  with fresh keys.

---

## Testing

Two layers of tests catch different kinds of regression.

### Unit tests — `scripts/run_tests.sh`

Stdlib `unittest` suite under `tests/` covering the base-station
Python (command channel, LoRa transport, registry, watchdog, factory
reset). 184 tests, ~9 seconds on a developer laptop.

```bash
scripts/run_tests.sh                            # default
scripts/run_tests.sh -v                         # verbose
scripts/run_tests.sh tests.test_telegram_commands  # one module
PYTHON=/path/to/venv/bin/python scripts/run_tests.sh  # specific Python
```

Requires Python 3.10+ (the app uses PEP 604 `X | None`) and
`cryptography` (the suite uses real Fernet keys to exercise the
`/pair` invalid-key path). If either is missing, the runner prints
an install hint and exits 0, so the pre-commit hook stays
non-blocking for fresh contributors. See
[tests/README.md](../tests/README.md) for the full coverage breakdown
and how to add new tests.

### Image invariants — `scripts/verify_image.sh`

Loop-mounts a built `.img` and runs ~80 assertion checks against
the rootfs: required files exist, systemd units are enabled at the
right target, the bot-token redaction call site is in place, `dotenv`
hasn't snuck back into a Python import, the captive-portal flow
hasn't lost its async `_complete_setup` thread, gpiozero's pin-factory
backend ships, etc. Linux-only (needs `losetup`).

```bash
# After a build:
scripts/verify_image.sh releases/base_station_pi3.img
scripts/verify_image.sh releases/gate_client_pi0w.img
```

Both calls exit 0 on success. A `[FAIL]` line tells you which file is
missing or which pattern regressed — usually a sign that a Buildroot
`.mk` install line didn't run because the package archive was cached.

`remote_build.sh` invokes `verify_image.sh` automatically after every
remote build, provided passwordless sudo is configured for the few
read-only commands the script uses. See
[Passwordless image verification](#passwordless-image-verification).

### Pre-commit hook

`.githooks/pre-commit` chains two checks on every commit:

1. `scripts/check_factory_deps.py` — asserts the factory scripts
   (`flash_base_station.py`, `provision_gate.py`) depend only on the
   Python stdlib, so operators can run them on a fresh laptop with no
   `pip install`.
2. `scripts/run_tests.sh` — the unit suite.

Git deliberately doesn't run hooks from an in-repo path by default
(it would be a code-execution vector on every fresh clone), so each
contributor opts in once per clone:

```bash
git config core.hooksPath .githooks
```

After that, commits that fail either check are rejected with a
pointer to the relevant doc.

---

## Debugging a development image

After flashing a `_dev.img` and booting it:

**Base station** — Ethernet is the easiest path. Find its IP in your
router's DHCP table:

```bash
ssh root@<base-station-ip>           # password: ranchdev
```

**Gate (Pi Zero W)** — no Ethernet. Use the UART serial console on
GPIO pins 8 and 10 (TX/RX, 3.3V) at 115200 baud, then log in as
`root` with password `ranchdev`.

Once you're in:

```bash
systemctl status base-provision.service
journalctl -u base-station -b --no-pager
journalctl -u ranch-wifi-watchdog -b --no-pager
ip link show
nmcli device status
rfkill list
```

**Never deploy a dev image.** The root password is checked into git
and SSH is reachable on every interface.

---

## Cleaning state

### What persists between builds

The build scripts bind-mount several host directories into the
container so work survives across `docker run --rm`. Each lives at
the repo root locally, or at `~/ranch_os_build/` on the remote
builder:

| Directory | Size | What's in it | Safe to delete? |
| --- | --- | --- | --- |
| `dl-cache/` | ~3 GB | Upstream tarballs (kernel, busybox, etc.) Buildroot has downloaded | Yes — next build re-downloads. Slow but harmless. |
| `ccache-dir/` | ~1 GB | Compiler output cache. Massive rebuild speedup. | Yes — next build is slow until it rewarms. |
| `output_base/` | ~10–15 GB | Buildroot intermediate state for the base image (host tools, toolchain, package builds, target rootfs) | Yes — see below for when. |
| `output_gate/` | ~10–15 GB | Same as above for the gate image | Yes — same rules as `output_base`. |
| `releases/` | per build | The final `.img` files | Don't delete — these are your shippable artifacts. |

`output_base/` and `output_gate/` are the big ones; the bind mounts
exist specifically to keep them off the Docker overlay filesystem
where they'd both bloat `/var/lib/docker` and vanish on every
`--rm`. Persisting them on the host makes builds incremental — a
no-op rebuild after only source edits is minutes instead of hours.

### Cleaning out the persistent output dirs

You usually don't need to. Buildroot is robust to incremental
rebuilds — source edits, overlay file changes, application code
changes, and the typical config-fragment tweaks are all handled
correctly without a clean.

Clean when:

- **You changed `BR2_*` config in a way that removed a package.**
  Buildroot doesn't always remove the now-deselected package's
  files from the staging area / target rootfs.
- **You bumped the `buildroot/` submodule.** Different Buildroot
  versions can produce subtly different output and a clean rebuild
  avoids hours of debugging weird symptoms.
- **You changed the target architecture, libc, or toolchain config.**
  Anything that affects host tools or the cross-compiler.
- **You're cutting a release.** A clean rebuild against the tagged
  commit is the most reproducible state to ship from.
- **You're seeing "this shouldn't be happening" errors** — a build
  step succeeds on incremental but fails after `make distclean`, or
  vice versa.

How:

```bash
# Local — from the repo root:
rm -rf output_base output_gate

# Remote builder — from your laptop:
ssh <user>@<build-host> 'rm -rf ranch_os_build/output_base ranch_os_build/output_gate'

# Selective — just nuke the broken one and let the other stay warm:
rm -rf output_base    # base only
```

The next build is a full Buildroot run for whatever you wiped
(~45 min per target on a fast box). `dl-cache/` and `ccache-dir/`
survive the wipe, so toolchain compilation and most third-party
package compilation gets ccache hits — substantially faster than a
truly-cold rebuild.

How often: a few times a year for steady-state development. Always
before cutting a release if you want belt-and-braces reproducibility.

### Deeper clean — Docker image too

If a Dockerfile change is involved or the container itself is misbehaving:

```bash
# Local
rm -rf output_base output_gate
docker build --no-cache -t ranch-builder .
./run_build.sh

# Remote — let remote_build.sh rebuild the container automatically
# (it does so each run), just nuke the output dirs:
ssh <user>@<build-host> 'rm -rf ranch_os_build/output_base ranch_os_build/output_gate'
./remote_build.sh
```

The nuclear option (also clears `dl-cache/` and `ccache-dir/`):

```bash
( cd buildroot && make distclean )
rm -rf dl-cache ccache-dir output_base output_gate
docker build --no-cache -t ranch-builder .
./run_build.sh
```

Use sparingly — that's a 1.5–3 hour cold rebuild because every
upstream tarball gets re-downloaded and every toolchain stage
recompiled.

---

## Passwordless sudo on the build host

The remote-build pipeline runs a handful of system commands on the
build host: starting/stopping Docker around the build, loop-mounting
the resulting image for verification, and `du`-ing the rootfs. None
of those is interactive, and waiting on a `sudo` prompt would break
`remote_build.sh`.

### Automated setup (Debian/Ubuntu)

For a fresh Debian or Ubuntu host, `scripts/setup_remote_builder.sh`
does everything in one shot from your Mac: copies your SSH key
(one password prompt), installs Docker + rsync + the loop-mount
utilities, adds your remote user to the `docker` group, and writes
the sudoers fragment shown below with `visudo` validation:

```bash
scripts/setup_remote_builder.sh --host <build-host> --user <user>
```

The script is idempotent — re-run it any time you change the sudoers
list. After it finishes, `./remote_build.sh` against the new host
should be fully passwordless for the build pipeline itself.

### Manual setup (any distro)

Configure passwordless sudo on the build host for exactly those
commands. Edit (substituting your username):

```bash
sudo visudo -f /etc/sudoers.d/ranch-build
```

```
<user> ALL=(root) NOPASSWD: /usr/bin/systemctl start docker containerd, \
                           /usr/bin/systemctl stop docker.socket docker containerd, \
                           /usr/sbin/losetup, \
                           /usr/bin/mount, \
                           /usr/bin/umount, \
                           /usr/bin/udevadm, \
                           /usr/bin/cat, \
                           /usr/bin/du
```

The paths must match what `which` prints on the build host — they
vary by distro. On Ubuntu 22.04 / 24.04 the paths above are correct;
check yours with:

```bash
ssh <user>@<host> 'which systemctl losetup mount umount udevadm cat du'
```

Then verify the rules are loaded:

```bash
ssh <user>@<host> sudo -n -l
```

The listed `NOPASSWD` commands should appear with exactly the paths
above.

### Workload-specific pause/resume (xmrig, etc.)

If you also run a heavy background process on the build host — a
miner, a video encoder, a CI worker — you'll want `remote_build.sh`
to pause it for the build and resume it afterwards via
`RANCH_BUILD_PRE_HOOK` and `RANCH_BUILD_POST_HOOK`. Those hooks
shell out to whatever command you set; for `sudo`-fronted commands
to run non-interactively, the specific systemctl invocations need
their own sudoers entries.

`setup_remote_builder.sh` deliberately does **not** write these for
you — they're operator policy, not build-pipeline policy. Add them
yourself alongside `/etc/sudoers.d/ranch-build`. For an xmrig host:

```
<user> ALL=(root) NOPASSWD: /usr/bin/systemctl stop xmrig, \
                           /usr/bin/systemctl start xmrig
```

Then your `remote_build.sh` invocation chains the hooks:

```bash
RANCH_BUILD_HOST=builder.lan \
RANCH_BUILD_USER=<user> \
RANCH_BUILD_PRE_HOOK='sudo systemctl stop xmrig && sudo systemctl start docker containerd' \
RANCH_BUILD_POST_HOOK='sudo systemctl stop docker.socket docker containerd; sudo systemctl start xmrig' \
./remote_build.sh
```

The post-hook fires from a `trap EXIT` in `remote_build_inner.sh`,
so xmrig restarts whether the build succeeded, failed, or was
interrupted with Ctrl-C.

### Passwordless image verification

If you skip configuring the verification half (`losetup`, `mount`,
`umount`, `udevadm`, `cat`), `remote_build.sh` still completes —
it just skips the verify step with a logged warning rather than
failing the build. Same for measurement (`du`).

---

## Factory scripts stay stdlib-only

`flash_base_station.py` and `provision_gate.py` deliberately import
nothing outside the Python standard library. The operator should be
able to run them on any fresh laptop with just the system `python3`
— no `pip install`, no venv, no `requirements.txt`.

The Fernet key minted by `provision_gate.py` is generated with
`base64.urlsafe_b64encode(secrets.token_bytes(32))`, which is what
`cryptography.fernet.Fernet.generate_key()` does internally. The
actual encryption happens on the gate device, where Buildroot
installs the real `cryptography` library — but the operator's laptop
never needs it.

The pre-commit hook enforces this. To run the check manually:

```bash
python3 scripts/check_factory_deps.py
```

The script walks each factory tool's AST and flags any top-level
import that isn't a stdlib name. Local sibling modules
(`factory_sticker`) are allow-listed in the script's `LOCAL_MODULES`
set.

If a factory script ever genuinely needs a third-party package,
prefer PEP 723 inline script metadata + [uv](https://github.com/astral-sh/uv)
over a `requirements.txt` — it keeps the "no setup step on a fresh
laptop" property:

```python
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["some-library>=1.0"]
# ///
```

---

## Build pipeline at a glance

```
                   ┌─────────────────────────┐
                   │ git clone --recursive   │
                   │ docker build .          │
                   └────────────┬────────────┘
                                │
                ┌───────────────┼───────────────┐
                ▼               ▼               ▼
        ./run_build.sh    ./remote_build.sh   docker run …
        (local Docker)    (build over SSH)    (manual)
                │               │               │
                └───────────────┼───────────────┘
                                ▼
                ┌──────────────────────────────┐
                │ releases/base_station_pi3.img│
                │ releases/gate_client_pi0w.img│
                └──────────────┬───────────────┘
                               │
                ┌──────────────┼──────────────┐
                ▼              ▼              ▼
       verify_image.sh   measure_image.sh   flash_base_station.py
       (~80 invariants)  (rootfs du)        provision_gate.py
                                            (writes to SD card +
                                             prints sticker block)
```
