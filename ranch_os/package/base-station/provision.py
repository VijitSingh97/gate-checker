"""First-boot captive-portal provisioner for the base station.

Runs when the main base-station service has not yet been configured. Brings
up an open Wi-Fi access point (intentional — Pi 3 brcmfmac firmware can't
reliably negotiate WPA2 in AP mode; see _start_access_point), serves an
HTTP form for the operator to supply Wi-Fi + Telegram credentials, and
exits so systemd hands off to the long-running base station once the
operator's Wi-Fi join succeeds.

The captive portal is deliberately limited to the credentials the device
needs to come online. Everything else — pairing gates, unpairing gates,
querying state, factory-reset — happens over Telegram once the base
station is on the operator's home network. The captive portal isn't
reachable after the AP tears down, so cramming gate-management UI into
it would just mean operators can only manage gates when something has
gone wrong enough to flip the device back into setup mode.

The provisioner runs as root only long enough to start the access point
and bind port 80. It then drops to the `basesetup` user and serves HTTP
requests with reduced privileges. The privileged operations the
unprivileged process still needs — tearing down the AP, joining the
operator's Wi-Fi, and (on failure) restarting the AP — are delegated to
/usr/bin/ranch-wifi-connect via a narrow sudoers rule.

The actual join happens in a background thread spawned by /save_network
so the "Settings saved" HTTP response flushes to the operator's phone
*before* we tear down the AP underneath them. On Wi-Fi-join failure we
bring the AP back up so the operator can retry from the same portal
without power-cycling.
"""

import logging
import os
import pwd
import re
import secrets
import subprocess
import threading
import time
from functools import wraps
from wsgiref.simple_server import make_server

from flask import Flask, Response, render_template_string, request

STATE_DIR = "/var/lib/base_station"
CONFIG_PATH = f"{STATE_DIR}/base_config.env"
# Written by base_station.py when the first Telegram ping after captive
# portal completion returns a 4xx — i.e. the operator's credentials are
# shaped like valid input but Telegram rejects them. The captive portal
# reads this on startup, renders it as a banner above the form, and
# deletes it after the next successful save. Plain-text, single line.
SETUP_ERROR_PATH = f"{STATE_DIR}/setup_error.txt"
# The factory flash writes provision_creds.env to FAT32 /boot (easy to
# inject from a host laptop). FAT32 mode bits are advisory, so on first
# run we migrate it to ext4 (0600) — same rationale and crash-safe
# ordering as the gate's ranch-gate-config-migrate.sh. The /boot path
# is only ever read if migration hasn't happened yet.
PROVISION_CREDS_BOOT_PATH = "/boot/provision_creds.env"
PROVISION_CREDS_PATH = f"{STATE_DIR}/provision_creds.env"
WIFI_CONNECT_HELPER = "/usr/bin/ranch-wifi-connect"

UNPRIVILEGED_USER = "basesetup"
AP_INTERFACE = "wlan0"
AP_SSID = "BaseStation_Setup"
PORTAL_PORT = 80

WIFI_CONNECT_TIMEOUT_SECONDS = 45
WIFI_POLL_INTERVAL_SECONDS = 2

# NetworkManager is "started" before the wireless device is fully adopted.
# We poll until nmcli reports wlan0 as a managed wifi device before issuing
# the hotspot command, with a generous timeout to cover slow firmware loads.
NM_DEVICE_READY_TIMEOUT_SECONDS = 60
NM_DEVICE_POLL_INTERVAL_SECONDS = 2

# nmcli's `device wifi rescan` is async; we sleep briefly so the in-kernel
# scan can populate before we read results.
WIFI_SCAN_WAIT_SECONDS = 6

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# --- Form validation -------------------------------------------------------
#
# We can't actually reach api.telegram.org from inside the captive portal
# (wlan0 can't be AP and station simultaneously), so a "send a real test
# alert" button isn't possible. What we *can* do is catch typo-class
# errors at submit time so the operator doesn't have to wait for the
# join + watchdog cycle to find out their bot token has a missing colon.
# Anything that isn't catchable here gets caught by base_station.py's
# post-join check, which falls back into setup mode with an error banner.
#
# Telegram bot token format from @BotFather: 8–10 digits, a colon, then
# ~35 URL-safe base64 chars. Keep the regex generous on the random
# portion (length ≥ 30, alphabet [A-Za-z0-9_-]) so a future Telegram
# rotation doesn't break us, but tight enough to reject "ABC", whitespace,
# half-pastes, and missing-colon mistakes.
_TELEGRAM_TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")
# Chat IDs are integers. User DMs are positive (8–10 digits); groups and
# channels are negative (e.g. -1001234567890 for supergroups). We accept
# any optionally-signed integer.
_TELEGRAM_CHAT_ID_RE = re.compile(r"^-?\d+$")
# 802.11 SSIDs are 1–32 octets. In practice they're text; we reject NUL
# but otherwise accept whatever the operator typed.
SSID_MAX_LEN = 32
# WPA2 PSK passwords are 8–63 ASCII characters (or 64 hex for a raw PMK,
# which we don't bother supporting). An empty password means "open
# network" — uncommon for a home router but not invalid.
WIFI_PSK_MIN_LEN = 8
WIFI_PSK_MAX_LEN = 63


def _validate_setup_form(ssid: str, password: str, token: str, chat_id: str) -> str | None:
    """Return an operator-readable error message, or None if all fields pass.

    Failures shaped like "shape is wrong" are caught here. Failures
    shaped like "shape is fine but Telegram disagrees" (token revoked,
    chat ID for a chat the bot isn't in, bot blocked by user) are
    caught post-join by base_station.py and surfaced through
    SETUP_ERROR_PATH.
    """
    if not ssid:
        return "Wi-Fi SSID is required."
    if len(ssid) > SSID_MAX_LEN:
        return f"Wi-Fi SSID must be {SSID_MAX_LEN} characters or fewer."
    if "\x00" in ssid:
        return "Wi-Fi SSID contains an invalid character."

    if password and not (WIFI_PSK_MIN_LEN <= len(password) <= WIFI_PSK_MAX_LEN):
        return (
            f"Wi-Fi password must be {WIFI_PSK_MIN_LEN}–{WIFI_PSK_MAX_LEN} "
            "characters (or leave blank for an open network)."
        )

    if not _TELEGRAM_TOKEN_RE.fullmatch(token):
        return (
            "Telegram bot token looks malformed. Expected the format "
            "<digits>:<random-characters> from @BotFather, e.g. "
            "123456789:ABCdef-GhIjKlmNopqrstuvwxyz0123456789."
        )

    if not _TELEGRAM_CHAT_ID_RE.fullmatch(chat_id):
        return (
            "Telegram chat ID must be a number. Look it up with "
            "@userinfobot — your user ID for DMs (positive) or the "
            "group ID for groups (negative, e.g. -1001234567890)."
        )

    return None


def _read_setup_error() -> str | None:
    """Pop the post-join error file if one exists. Single-shot read."""
    try:
        with open(SETUP_ERROR_PATH, encoding="utf-8") as handle:
            text = handle.read().strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("Could not read %s: %s", SETUP_ERROR_PATH, exc)
        return None
    # Clear the file so the banner only renders once per failed attempt.
    # We don't unlink on first save success because the operator might
    # reload the page before submitting — the banner is the "last thing
    # that happened" signal, which they may want to see again.
    try:
        os.unlink(SETUP_ERROR_PATH)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("Could not remove %s: %s", SETUP_ERROR_PATH, exc)
    return text or None


HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Ranch OS Base Station Setup</title>
    <style>
        body { font-family: sans-serif; padding: 20px; max-width: 480px; margin: auto; }
        .card { background: #f4f4f4; padding: 15px; margin-bottom: 20px; border-radius: 8px; }
        .err { color: #b00; font-weight: bold; }
        .banner { background: #fdecea; border-left: 4px solid #b00; padding: 10px 12px; margin-bottom: 20px; }
        .banner b { color: #b00; }
        input { width: 100%; padding: 8px; margin: 5px 0 15px 0; box-sizing: border-box; }
        button { background: #0056b3; color: white; border: none; padding: 10px; width: 100%; cursor: pointer; }
    </style>
</head>
<body>
    <h2>Ranch OS Base Station</h2>

    {% if setup_error %}
        <div class="banner">
            <b>Last setup attempt failed.</b><br>
            {{ setup_error }}<br>
            <span style="font-size: 0.9em;">Re-enter your credentials below and try again.</span>
        </div>
    {% endif %}

    {% if error %}<p class="err">{{ error }}</p>{% endif %}

    <div class="card">
        <p style="font-size: 0.9em; color: #555; margin-top: 0;">
            Enter your home Wi-Fi and Telegram details below. After you
            save, this setup network will disappear and the Base Station
            will join your home Wi-Fi. Pairing gates and all other
            controls happen over Telegram once the device is online.
        </p>
        <form method="POST" action="/save_network">
            <label>Wi-Fi SSID:</label>
            <input type="text" name="ssid" list="ssids" required
                   value="{{ form.ssid|default('') }}"
                   placeholder="Pick a nearby network or type one">
            <datalist id="ssids">
                {% for s in networks %}
                <option value="{{ s }}">
                {% endfor %}
            </datalist>
            <label>Wi-Fi Password:</label>
            <input type="password" name="password">
            <label>Telegram Bot Token:</label>
            <input type="text" name="token" required
                   placeholder="123456789:ABC-DEF..."
                   value="{{ form.token|default('') }}">
            <label>Telegram Chat ID:</label>
            <input type="text" name="chat_id" required
                   placeholder="e.g. 123456789 or -1001234567890"
                   value="{{ form.chat_id|default('') }}">
            <button type="submit" style="background: #28a745;">Save &amp; Connect</button>
        </form>
    </div>
</body>
</html>
"""


def _migrate_provision_creds() -> None:
    """Move provision_creds.env off FAT32 /boot onto ext4 STATE_DIR.

    Anyone who pulls the SD card can read the FAT32 copy regardless of
    its mode bits, so the portal password must not live there past the
    first boot. Crash-safe, mirroring ranch-gate-config-migrate.sh:
    copy → fsync → atomic replace → only then remove the /boot copy.
    An interruption at any point leaves at least one intact copy and
    the next run finishes the job. Must run as root (before
    _drop_privileges).
    """
    if not os.path.exists(PROVISION_CREDS_BOOT_PATH):
        return
    try:
        if not os.path.exists(PROVISION_CREDS_PATH):
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = f"{PROVISION_CREDS_PATH}.tmp"
            with open(PROVISION_CREDS_BOOT_PATH, "rb") as src, \
                    open(tmp, "wb") as dst:
                dst.write(src.read())
                dst.flush()
                os.fsync(dst.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, PROVISION_CREDS_PATH)
        os.unlink(PROVISION_CREDS_BOOT_PATH)
        logger.info(
            "Migrated %s -> %s (portal password now ext4-protected)",
            PROVISION_CREDS_BOOT_PATH, PROVISION_CREDS_PATH,
        )
    except OSError as exc:
        # Never block setup on the migration — _load_provision_credentials
        # falls back to whichever copy is still readable.
        logger.error("provision_creds migration failed: %s", exc)


def _load_provision_credentials() -> str:
    """Read the per-device portal auth password.

    Written by the factory flash script so every device has a unique
    portal admin password printed on its product sticker. Returns the
    portal password; ignores any other keys (e.g. an AP_PASSWORD from
    older flash scripts — we intentionally run the setup AP open, see
    _start_access_point for why).
    """
    _migrate_provision_creds()
    creds: dict[str, str] = {}
    for path in (PROVISION_CREDS_PATH, PROVISION_CREDS_BOOT_PATH):
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line or "=" not in line or line.startswith("#"):
                        continue
                    key, value = line.split("=", 1)
                    creds[key.strip()] = value.strip()
            break
        except OSError:
            # Missing or unreadable — try the other copy.
            continue
    else:
        logger.critical(
            "Provisioning credentials not found at %s or %s",
            PROVISION_CREDS_PATH, PROVISION_CREDS_BOOT_PATH,
        )
        raise SystemExit(1)

    portal_password = creds.get("PORTAL_PASSWORD")
    if not portal_password:
        logger.critical("Provisioning credentials file is missing PORTAL_PASSWORD")
        raise SystemExit(1)
    return portal_password


def _prepare_state_dir(owner: str) -> None:
    """Create the persistent state dir, owned by the given user.

    Runs while we still have root. The captive portal needs to be able to
    write base_config.env after dropping privileges, so we chown the dir
    to `basesetup`. The events.db file (registered_gates + gate_events
    tables) is created by base_station.py:GateRegistry._init_schema() on
    its first start — provision.py no longer touches gate state, since
    pairing happens over Telegram once the device is online.
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    pwnam = pwd.getpwnam(owner)
    os.chown(STATE_DIR, pwnam.pw_uid, pwnam.pw_gid)
    os.chmod(STATE_DIR, 0o750)


def _drop_privileges(username: str) -> None:
    """Switch the process to running as `username`. Must be called as root."""
    if os.getuid() != 0:
        return
    pwnam = pwd.getpwnam(username)
    os.setgroups([])
    os.setgid(pwnam.pw_gid)
    os.setuid(pwnam.pw_uid)
    if os.geteuid() == 0 or os.getuid() == 0:
        raise SystemExit("Failed to drop privileges; refusing to continue as root")


def _wait_for_wifi_device() -> None:
    """Block until NetworkManager reports the AP interface as a wifi device.

    `After=NetworkManager.service` only guarantees the daemon has started,
    not that it has finished discovering hardware. A fresh Pi 3 sometimes
    needs ~10 seconds after NM starts before wlan0 is fully adopted.
    """
    deadline = time.monotonic() + NM_DEVICE_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device", "status"],
            capture_output=True, text=True,
        )
        for line in result.stdout.splitlines():
            parts = line.split(":")
            if len(parts) >= 3 and parts[0] == AP_INTERFACE and parts[1] == "wifi":
                state = parts[2]
                if state not in ("unmanaged", "unavailable"):
                    logger.info("NetworkManager has adopted %s (state=%s)",
                                AP_INTERFACE, state)
                    return
        time.sleep(NM_DEVICE_POLL_INTERVAL_SECONDS)
    raise SystemExit(
        f"NetworkManager never reported {AP_INTERFACE} as a managed wifi "
        f"device within {NM_DEVICE_READY_TIMEOUT_SECONDS}s. Check that "
        f"firmware is installed (dmesg | grep -i brcm) and that rfkill is "
        f"not blocking the radio."
    )


def _scan_networks() -> list[str]:
    """One-shot Wi-Fi scan, BEFORE we hijack wlan0 for AP mode.

    Returned to the captive portal so the operator can pick their home
    Wi-Fi from a dropdown instead of typing the SSID by hand. Best-effort:
    if the scan fails, we just return an empty list and the form falls
    back to plain typing.
    """
    try:
        subprocess.run(
            ["nmcli", "device", "wifi", "rescan", "ifname", AP_INTERFACE],
            capture_output=True, timeout=10, check=False,
        )
        time.sleep(WIFI_SCAN_WAIT_SECONDS)
        result = subprocess.run(
            ["nmcli", "-t", "-f", "SSID", "device", "wifi", "list",
             "ifname", AP_INTERFACE],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Wi-Fi pre-scan failed: %s", exc)
        return []

    if result.returncode != 0:
        return []

    seen: set[str] = set()
    for line in result.stdout.splitlines():
        ssid = line.strip()
        # nmcli prints "--" for hidden networks and "" for the empty SSID
        # placeholder; we filter both.
        if ssid and ssid != "--":
            seen.add(ssid)
    logger.info("Pre-scan found %d nearby Wi-Fi networks", len(seen))
    return sorted(seen, key=str.casefold)


def _unblock_wifi() -> None:
    """Best-effort rfkill unblock. Pi 3 onboard WiFi often boots soft-blocked."""
    result = subprocess.run(
        ["rfkill", "unblock", "wifi"], capture_output=True, text=True,
    )
    if result.returncode != 0:
        logger.warning(
            "rfkill unblock returned non-zero (%s); continuing anyway",
            result.stderr.strip() or "no error message",
        )


def _start_access_point(ssid: str) -> None:
    """Bring up an open captive-portal AP.

    Why open and not WPA2: the Pi 3's brcmfmac firmware reliably fails
    to set the RSN Information Element in AP-mode beacons (kernel logs
    `brcmf_vif_set_mgmt_ie: vndr ie set error : -52`), so clients can
    see the SSID but their WPA2 4-way handshake never completes. Open
    APs work fine on the same hardware. This was reproduced even with
    explicit `proto=rsn`, `pairwise/group=ccmp`, an explicit channel,
    the regulatory domain set, and brcmfmac power-save disabled.

    For the threat model — a first-boot setup AP that only exists for
    seconds while the operator is physically next to the device — an
    open AP is acceptable. The portal still requires HTTP Basic auth
    with the per-device PORTAL_PASSWORD (16 chars, ~95 bits), which is
    the real auth boundary. If a Pi variant with working WPA2 AP mode
    is ever used (or hostapd is swapped in), reinstate the wifi-sec.*
    arguments and the AP_PASSWORD plumbing in flash_base_station.py.

    Prerequisite: _unblock_wifi() and _wait_for_wifi_device() have been
    called by main() so wlan0 is unblocked and adopted by NetworkManager.
    """
    # Tear down any prior incarnation. Idempotent — `nmcli connection
    # delete` returns non-zero if the connection doesn't exist; we don't
    # care.
    subprocess.run(
        ["nmcli", "connection", "delete", ssid],
        capture_output=True, check=False,
    )

    logger.info("Creating open hotspot connection (ssid=%s)", ssid)
    create = subprocess.run(
        [
            "nmcli", "connection", "add",
            "type", "wifi",
            "ifname", AP_INTERFACE,
            "con-name", ssid,
            "ssid", ssid,
            "wifi.mode", "ap",
            "wifi.band", "bg",
            "ipv4.method", "shared",
        ],
        capture_output=True, text=True,
    )
    if create.returncode != 0:
        raise SystemExit(
            f"Failed to create hotspot connection: {create.stderr.strip() or create.stdout.strip()}"
        )

    logger.info("Activating hotspot on %s", AP_INTERFACE)
    activate = subprocess.run(
        ["nmcli", "connection", "up", ssid],
        capture_output=True, text=True,
    )
    if activate.returncode != 0:
        raise SystemExit(
            f"Failed to activate hotspot: {activate.stderr.strip() or activate.stdout.strip()}"
        )
    logger.info("Open access point active: %s", activate.stdout.strip())


def _connect_to_wifi(ssid: str, password: str) -> bool:
    """Tear down the setup AP, then associate with the operator's Wi-Fi.

    Runs after the privilege drop, so it goes through the sudoers-allowed
    wrapper script. The password is piped via stdin so it never appears in
    the process argument list. The helper tears down the AP before scanning
    because wlan0 can't be in AP and station modes simultaneously.
    """
    helper = subprocess.run(
        ["sudo", "-n", WIFI_CONNECT_HELPER, ssid, str(WIFI_CONNECT_TIMEOUT_SECONDS)],
        input=password,
        text=True,
        capture_output=True,
    )
    if helper.returncode != 0:
        logger.error("ranch-wifi-connect failed: %s", helper.stderr.strip())
        return False

    deadline = time.monotonic() + WIFI_CONNECT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        check = subprocess.run(
            ["nmcli", "-t", "-f", "ACTIVE,SSID", "device", "wifi"],
            capture_output=True,
            text=True,
        )
        for line in check.stdout.splitlines():
            if line.startswith("yes:") and line.split(":", 1)[1] == ssid:
                return True
        time.sleep(WIFI_POLL_INTERVAL_SECONDS)
    return False


def _restart_access_point() -> None:
    """Bring the setup AP back up after a failed Wi-Fi join attempt."""
    result = subprocess.run(
        ["sudo", "-n", WIFI_CONNECT_HELPER, "--restart-ap"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        logger.error("Failed to restart setup AP: %s", result.stderr.strip())


def _write_base_config(token: str, chat_id: str) -> None:
    fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"TELEGRAM_TOKEN={token}\n")
        handle.write(f"TELEGRAM_CHAT_ID={chat_id}\n")


def create_app(portal_password: str, networks: list[str], setup_error: str | None) -> Flask:
    app = Flask(__name__)
    expected_password = portal_password
    # Wrap the setup_error in a mutable container so the index handler can
    # clear it after rendering once. A bare `nonlocal` reassignment doesn't
    # work cleanly here because Flask creates the closure during
    # registration; a one-element list is the simplest mutable cell.
    setup_error_cell = [setup_error]

    def _requires_auth(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            auth = request.authorization
            authorized = (
                auth is not None
                and auth.username == "admin"
                and secrets.compare_digest(auth.password or "", expected_password)
            )
            if not authorized:
                # Log so the operator can tell typos from real bugs in the
                # journal, but don't rate-limit: a 16-char random password
                # is uncrackable within the few-minute setup window, and
                # locking out a frustrated operator who fat-fingered three
                # times is worse UX than the marginal brute-force defense.
                logger.warning(
                    "Captive portal: failed auth from %s",
                    request.remote_addr or "unknown",
                )
                return Response(
                    "Authentication required.",
                    status=401,
                    headers={"WWW-Authenticate": 'Basic realm="Ranch OS Setup"'},
                )
            return view(*args, **kwargs)

        return wrapper

    @app.route("/", defaults={"path": ""})
    @app.route("/<path:path>")
    @_requires_auth
    def index(path):  # noqa: ARG001 — path is the captive-portal catch-all
        # Pop the post-join error on first render of the page; subsequent
        # reloads see no banner. Operators who reload mid-typing wouldn't
        # benefit from a sticky banner — they already read it.
        current_error = setup_error_cell[0]
        setup_error_cell[0] = None
        return render_template_string(
            HTML_TEMPLATE,
            error=None,
            networks=networks,
            setup_error=current_error,
            form={},
        )

    @app.route("/save_network", methods=["POST"])
    @_requires_auth
    def save_network():
        ssid = request.form["ssid"].strip()
        password = request.form["password"]
        token = request.form["token"].strip()
        chat_id = request.form["chat_id"].strip()

        # Format-only validation — catches the typo class of mistakes
        # without burning the AP teardown. Real "your token is wrong"
        # errors are caught post-join in base_station.py and surfaced
        # through SETUP_ERROR_PATH on the next captive-portal cycle.
        validation_error = _validate_setup_form(ssid, password, token, chat_id)
        if validation_error is not None:
            logger.info("Captive portal: rejected submission (%s)", validation_error)
            return render_template_string(
                HTML_TEMPLATE,
                error=validation_error,
                networks=networks,
                setup_error=None,
                # Pre-fill what the operator already typed so they don't
                # have to start over. Password deliberately omitted — same
                # convention browsers use for type=password fields.
                form={"ssid": ssid, "token": token, "chat_id": chat_id},
            )

        # Do the disruptive work (tear down AP, join Wi-Fi) in a background
        # thread so the operator's HTTP response can flush *before* the AP
        # drops out from under their phone. If the join succeeds we commit
        # the Telegram config and exit; if it fails we bring the AP back up
        # so the operator can retry from the same portal.
        threading.Thread(
            target=_complete_setup,
            args=(ssid, password, token, chat_id),
            daemon=True,
        ).start()

        return render_template_string(
            """<!DOCTYPE html>
<html><head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Ranch OS Setup</title>
    <style>
        body { font-family: sans-serif; padding: 20px; max-width: 520px; margin: auto;
               color: #222; line-height: 1.45; }
        h2 { color: #2a6b2a; margin-top: 0; }
        ol { padding-left: 22px; }
        ol li { margin-bottom: 14px; }
        ol li b { display: block; margin-bottom: 2px; }
        .next { background: #eef6ee; border-left: 4px solid #2a6b2a;
                padding: 12px 14px; border-radius: 4px; margin-top: 20px; }
        .hint { font-size: 0.9em; color: #555; margin-top: 16px; }
        code { background: #f4f4f4; padding: 1px 4px; border-radius: 3px; }
    </style>
</head><body>
    <h2>✓ Settings saved</h2>
    <p>The Base Station is switching over to your home Wi-Fi now. You can
       close this page — this setup network is about to disappear.</p>

    <ol>
        <li><b>~5 seconds — Wi-Fi switch</b>
            Your phone will lose <code>BaseStation_Setup</code>. That's
            expected; reconnect to your home Wi-Fi (<b>{{ ssid }}</b>) on
            your phone manually if it doesn't switch back on its own.</li>
        <li><b>~10–30 seconds — joining your network</b>
            The Base Station associates with <b>{{ ssid }}</b> and syncs
            its clock from the internet.</li>
        <li><b>Within ~60 seconds — Telegram online ping</b>
            You'll get a message in your Telegram chat that reads
            <code>📡 Gate Monitor Base Station is Online.</code> from your
            bot. That's the green light — setup is complete.</li>
    </ol>

    <div class="next">
        <b>Next steps (over Telegram):</b><br>
        Once the online ping arrives, every other action — pairing gates,
        unpairing, status checks, factory reset — happens by sending
        commands to your bot. The bot will respond to <code>/help</code>
        with the full list once the command channel is enabled.
    </div>

    <p class="hint">
        <b>Don't see the ping after a minute?</b> The Base Station will
        flip back into setup mode on its own — reconnect to
        <code>BaseStation_Setup</code> and the portal will show what went
        wrong with a banner above the form. (Wrong Wi-Fi password takes
        a few seconds to detect; wrong Telegram credentials take up to a
        minute.)
    </p>
</body></html>""",
            ssid=ssid,
        )

    return app


def _complete_setup(ssid: str, password: str, token: str, chat_id: str) -> None:
    """Background task: switch wlan0 from AP to station, then commit config.

    Runs after `/save_network` has already returned its response to the
    operator. On success: writes /var/lib/base_station/base_config.env (which
    is the file `base-station.service` keys off of via ConditionPathExists)
    and exits the process so systemd hands off to the main service. On
    failure: brings the setup AP back up so the operator can retry without
    a power-cycle, and stays in setup mode.
    """
    # Give the HTTP response a moment to flush over the AP before we tear
    # it down underneath the operator's phone.
    time.sleep(2)

    if not _connect_to_wifi(ssid, password):
        logger.error(
            "Wi-Fi join failed for SSID=%s; restarting setup AP for retry", ssid
        )
        _restart_access_point()
        return

    _write_base_config(token, chat_id)
    logger.info("Wi-Fi joined and config committed; handing off to base-station")
    os._exit(0)


def main() -> None:
    portal_password = _load_provision_credentials()

    _prepare_state_dir(UNPRIVILEGED_USER)

    # Scan BEFORE we hijack wlan0 for the AP — wlan0 can't scan while it's
    # broadcasting a hotspot, so this is our only chance to see the nearby
    # networks the operator might want to join. The result feeds the SSID
    # dropdown in the captive portal.
    _unblock_wifi()
    _wait_for_wifi_device()
    networks = _scan_networks()

    # If base_station.py flipped us back into setup mode after a failed
    # first-time Telegram ping, it left a one-line reason here. Read it
    # *before* the AP comes up so the very first request to the portal
    # renders the banner.
    setup_error = _read_setup_error()
    if setup_error:
        logger.info("Surfacing prior setup error to operator: %s", setup_error)

    _start_access_point(AP_SSID)

    app = create_app(portal_password, networks, setup_error)
    server = make_server("0.0.0.0", PORTAL_PORT, app)

    _drop_privileges(UNPRIVILEGED_USER)
    logger.info(
        "Serving captive portal on port %d as %s (uid=%d)",
        PORTAL_PORT, UNPRIVILEGED_USER, os.getuid(),
    )
    logger.info(
        "Operator: connect to open Wi-Fi '%s' and browse to http://10.42.0.1/",
        AP_SSID,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
