#!/usr/bin/env python3
"""Wi-Fi connectivity watchdog for the base station.

After successful setup the device joins the operator's Wi-Fi and stops
broadcasting BaseStation_Setup. NetworkManager handles routine reconnects
(brief router reboots, ISP hiccups, etc.) on its own. But it cannot
recover from:

  - operator changed their Wi-Fi password
  - operator's SSID was renamed or replaced (new router)
  - sustained outage longer than the operator's patience

This watchdog runs alongside base-station.service. If it sees the device
disconnected for longer than DISCONNECTED_THRESHOLD_SECONDS, it deletes
/var/lib/base_station/base_config.env (which is the file that gates
base-station vs. base-provision via systemd ConditionPathExists=) and
asks systemd to start the provisioner. The captive portal AP comes back
up so the operator can re-enter credentials without physical access to
the device.

"Connected" requires two independent signals to agree:

  1. NetworkManager reports a `connected*` global state.
  2. A short TCP probe to 1.1.1.1:53 succeeds.

The TCP probe exists because `nmcli STATE` flips to "connected" the
moment the link-layer association completes, well before the upstream
is actually reachable. A failed-but-not-removed Wi-Fi profile (stale
DHCP lease, modem-side captive portal, ISP outage) leaves nmcli happy
while the device is effectively offline. Either signal failing starts
the disconnected countdown; both need to recover before it resets.

The watchdog is started automatically by base-station.service via
`Wants=ranch-wifi-watchdog.service`. After a successful re-provision the
provisioner exits, base-station starts again, and the cycle repeats —
watchdog included.
"""

import logging
import os
import socket
import subprocess
import sys
import time

CONFIG_PATH = "/var/lib/base_station/base_config.env"

CHECK_INTERVAL_SECONDS = 30
# 30 minutes — long enough to absorb a slow ISP modem reboot (10 min was
# observed to be on the edge during a real router-power-cycle event),
# short enough that an operator who's actually locked the device out of
# their Wi-Fi doesn't wait forever for the AP to come back.
DISCONNECTED_THRESHOLD_SECONDS = 1800

# Reachability probe. Cloudflare's 1.1.1.1:53 is well-known to accept
# TCP DNS and is what mobile OSes use for the same "is the upstream
# actually up" question. TCP rather than UDP so we get a real handshake
# (UDP would silently succeed on a NAT'd black hole). 3s is long enough
# to tolerate a momentarily-loaded uplink without falsely tripping.
UPSTREAM_PROBE_HOST = "1.1.1.1"
UPSTREAM_PROBE_PORT = 53
UPSTREAM_PROBE_TIMEOUT_SECONDS = 3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def _nm_global_state() -> str:
    """Return NetworkManager's overall state, or 'unknown' if nmcli fails."""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "STATE", "general"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("nmcli probe failed: %s", exc)
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    return result.stdout.strip()


def _nm_says_connected() -> bool:
    """True if NM reports any 'connected*' state.

    We accept 'connected', 'connected (site)', 'connected (local)' — the
    point of this watchdog is to detect a *broken* Wi-Fi association, not
    a transient DNS hiccup. Being conservative here avoids spurious flips
    into setup mode.
    """
    return _nm_global_state().startswith("connected")


def _upstream_reachable() -> bool:
    """True if we can complete a TCP handshake to UPSTREAM_PROBE_HOST.

    Catches the "associated but no internet" failure modes nmcli can't
    see: stale DHCP lease, modem-side captive portal, ISP outage. We
    deliberately don't resolve a hostname — picking a literal IP keeps
    a broken DNS chain from masquerading as a broken upstream.
    """
    try:
        with socket.create_connection(
            (UPSTREAM_PROBE_HOST, UPSTREAM_PROBE_PORT),
            timeout=UPSTREAM_PROBE_TIMEOUT_SECONDS,
        ):
            return True
    except OSError as exc:
        logger.debug("Upstream probe to %s:%d failed: %s",
                     UPSTREAM_PROBE_HOST, UPSTREAM_PROBE_PORT, exc)
        return False


def _have_internet() -> bool:
    """True only if both signals agree the device is on the internet."""
    return _nm_says_connected() and _upstream_reachable()


def _force_setup_mode() -> None:
    """Tear down the main service and bring the captive portal back up."""
    logger.warning(
        "Connectivity lost for >%ds — forcing setup AP back up",
        DISCONNECTED_THRESHOLD_SECONDS,
    )
    try:
        os.unlink(CONFIG_PATH)
        logger.info("Removed %s (re-arms ConditionPathExists for provisioner)",
                    CONFIG_PATH)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.error("Could not remove %s: %s", CONFIG_PATH, exc)
        return

    # Stop the main service first. With config gone its ConditionPathExists
    # check would fail on the next restart anyway, but stopping it cleanly
    # avoids the Restart=on-failure backoff loop in the journal. Generous
    # timeout because systemd's default TimeoutStopSec is 90s; we'd rather
    # log a warning and keep the watchdog responsive than wedge it forever.
    try:
        subprocess.run(
            ["systemctl", "stop", "base-station.service"],
            check=False, capture_output=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        logger.error("systemctl stop base-station.service timed out")
    # Start the provisioner. Its ConditionPathExists=!base_config.env now
    # passes since we just deleted the file.
    try:
        subprocess.run(
            ["systemctl", "start", "base-provision.service"],
            check=False, capture_output=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        logger.error("systemctl start base-provision.service timed out")


def main() -> None:
    logger.info(
        "Wi-Fi watchdog up (poll=%ds, threshold=%ds, probe=%s:%d)",
        CHECK_INTERVAL_SECONDS,
        DISCONNECTED_THRESHOLD_SECONDS,
        UPSTREAM_PROBE_HOST,
        UPSTREAM_PROBE_PORT,
    )
    disconnected_since: float | None = None
    while True:
        if _have_internet():
            if disconnected_since is not None:
                logger.info("Connectivity restored")
                disconnected_since = None
        else:
            if disconnected_since is None:
                disconnected_since = time.monotonic()
                logger.warning(
                    "Connectivity lost; %ds countdown to setup-mode flip started",
                    DISCONNECTED_THRESHOLD_SECONDS,
                )
            elif time.monotonic() - disconnected_since > DISCONNECTED_THRESHOLD_SECONDS:
                _force_setup_mode()
                # Exit cleanly. After the provisioner completes a new cycle
                # systemd will start base-station again, which Wants= us
                # back into existence.
                sys.exit(0)
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
