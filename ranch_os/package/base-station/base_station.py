"""LoRa base station.

Listens for events from registered gate monitors, persists them to a local
SQLite store, and forwards alerts to a Telegram chat. All radio traffic is
encrypted per-gate with a Fernet key that is registered via the captive
portal provisioner.
"""

import dataclasses
import json
import logging
import math
import os
import re
import secrets
import shlex
import socket
import sqlite3
import struct
import subprocess
import threading
import time
from typing import Callable

import requests
import serial
from cryptography.fernet import Fernet, InvalidToken

# Note: no `from dotenv import load_dotenv` — base-station.service uses
# systemd's EnvironmentFile= to load /var/lib/base_station/base_config.env
# before this process starts, so the values are already in os.environ.

STATE_DIR = "/var/lib/base_station"
CONFIG_PATH = f"{STATE_DIR}/base_config.env"
DB_PATH = f"{STATE_DIR}/events.db"
# Touched after the first online ping returns 2xx. While this file is
# missing, a 4xx response (bot token / chat ID rejected by Telegram) is
# treated as "the operator just typed bad credentials in the captive
# portal" and we flip back into setup mode so they can retry without
# waiting for the watchdog. After the sentinel is in place, the same
# 4xx is treated as a steady-state failure (token revoked, etc.) and
# only logged — the operator is expected to intervene manually rather
# than have an outage automatically reset their device.
SETUP_VALIDATED_PATH = f"{STATE_DIR}/.setup_validated"
# Written here for provision.py to read on the next captive-portal
# cycle. Kept in sync with provision.py:SETUP_ERROR_PATH.
SETUP_ERROR_PATH = f"{STATE_DIR}/setup_error.txt"

TELEGRAM_TIMEOUT_SECONDS = 5
SERIAL_READ_TIMEOUT_SECONDS = 1.0
LOOP_TICK_SECONDS = 0.1

# LoRa send-and-wait timeouts. The default covers a challenge_req →
# challenge_resp round-trip with comfortable headroom: at 9600 baud a
# ~100-byte Fernet payload is ~80ms on the wire, the gate's processing
# tick is 100ms, and the reply takes another 80ms.
LORA_DEFAULT_REPLY_TIMEOUT_SECONDS = 5

# Adaptive grace period for /open and /close. The post-ack wait is
# dominated by physical actuation (1s relay pulse + motor swing + reed
# switch trip), which is a relatively stable physical process per
# (gate, action), so a rolling buffer of recent successful cycles
# gives a meaningful upper bound. Open and close are tracked
# separately because they aren't symmetric on a hinged gate (gravity,
# sag, drag).
#
#     threshold = min(ABSOLUTE_CEILING,
#                     max(mean + K * stdev, mean + MIN_SLACK))
#
# During warmup (n < MIN_SAMPLES) we use the absolute ceiling — better
# to wait too long than to time out a healthy gate while still
# learning. Numbers below are tuned for a residential driveway gate
# (swing or slide, 10–25s actuation). Tunable from the field; values
# only affect timing-out-too-early, not correctness.
ACTUATION_BUFFER_SAMPLES = 20
ACTUATION_MIN_SAMPLES = 5
ACTUATION_K_MULTIPLIER = 3.0
ACTUATION_MIN_SLACK_SECONDS = 3.0
ACTUATION_ABSOLUTE_CEILING_SECONDS = 30.0
# Hard cap on rows kept per (gate_id, action) in `actuation_cycles`.
# 100 keeps the table at a few KB per gate while still giving the
# buffer plenty of headroom over BUFFER_SAMPLES.
ACTUATION_RETENTION_PER_BUCKET = 100

# Per-gate relay pulse duration: how long the gate holds its relay
# closed to trigger the opener. Configurable from Telegram via /relay,
# stored in `registered_gates.relay_ms`, and sent in every command
# frame so the gate firmware needs no per-install change. The default
# matches the gate's own fallback (1s) — a freshly-paired gate behaves
# exactly as before until the operator tunes it. MIN/MAX bound an
# operator typo (the gate clamps too, as defence in depth); the ceiling
# lines up with ACTUATION_ABSOLUTE_CEILING_SECONDS so a press can never
# outlast the grace period we'd wait for it.
RELAY_PULSE_DEFAULT_MS = 1000
RELAY_PULSE_MIN_MS = 100
RELAY_PULSE_MAX_MS = 30000

# Hard cap on rows kept per gate in `gate_events`. Several months of
# forensic history for a residential gate (~10 events/day) and a
# bounded steady-state DB size regardless of how long the device
# runs. Trimmed inside `log_event` so the prune is amortized over the
# insert rate rather than a separate sweep.
EVENT_LOG_RETENTION_PER_GATE = 1000

# Telegram command channel (long-poll + state machine for /pair, /unpair,
# /rename, /confirm, /cancel, /status, /help). Numbers below are the
# defaults; bump them with care — the rate-limit window is what stops a
# spammed /pair from filling the chat history with leaked keys.
COMMAND_LONGPOLL_TIMEOUT_SECONDS = 25
COMMAND_HTTP_TIMEOUT_SECONDS = COMMAND_LONGPOLL_TIMEOUT_SECONDS + 5
COMMAND_LOOP_BACKOFF_SECONDS = 5
PENDING_ACTION_TTL_SECONDS = 60
PAIR_RATE_LIMIT_WINDOW_SECONDS = 3600
PAIR_RATE_LIMIT_MAX_ATTEMPTS = 5
# 4 hex chars ≈ 16 bits of token space, refreshed per command, one
# active per user. Brute force inside the 60-second window would
# require ~65k /confirm guesses through Telegram, which the API's own
# per-bot rate limit makes impossible. See docs/TELEGRAM.md
# "Confirmation flow for destructive commands" for the full reasoning.
PENDING_TOKEN_BYTES = 2
# Gate IDs come from provision_gate.py: GATE- + 6-char alphabet.
# Accept slightly broader ranges (4-12 chars) so legacy 4-char IDs from
# earlier builds still pair and a future bump doesn't immediately
# break us.
GATE_ID_RE = re.compile(r"^GATE-[A-Z0-9]{4,12}$")
GATE_NAME_MAX_LEN = 64

# Best-effort wait for systemd-timesyncd to sync the kernel clock before
# we try the online-ping HTTPS request. Pi 3 has no RTC; without NTP the
# clock is months in the past on first boot, so TLS cert validation fails
# with "certificate not yet valid". If sync never happens we still come
# up — LoRa is time-independent — we just skip the online ping.
CLOCK_SYNC_TIMEOUT_SECONDS = 60
CLOCK_SYNC_POLL_INTERVAL_SECONDS = 2
TIMESYNC_SYNCHRONIZED_MARKER = "/run/systemd/timesync/synchronized"

# Anything before this epoch is "definitely the pre-NTP clock" — either
# the Pi's persisted last-known time (months in the past) or the image
# build time. If `time.time()` is past this sentinel, the clock is
# plausibly close enough to "now" for TLS cert validation to succeed,
# regardless of whether systemd-timesyncd has officially marked the
# clock as NTPSynchronized. Bump this every few years.
PLAUSIBILITY_SENTINEL_EPOCH = 1_735_689_600  # 2025-01-01 UTC

# Public NTP servers, used by our manual fallback when systemd-timesyncd
# fails to sync (a known issue on NetworkManager-only Buildroot images:
# timesyncd binds its activation logic to systemd-networkd RTNETLINK
# events that don't arrive the same way under NM).
NTP_FALLBACK_SERVERS = (
    "time.cloudflare.com",
    "time.google.com",
    "pool.ntp.org",
)
NTP_QUERY_TIMEOUT_SECONDS = 5
# Sanity floor for an NTP reply: anything earlier than ~Nov 2023 is
# almost certainly garbage and we reject it rather than set the clock
# backwards.
NTP_REPLY_FLOOR_EPOCH = 1_700_000_000

EVENT_GATE_STATE = "gate_state"

# Telegram bot tokens look like `<digits>:<alphanumeric-and-dash-underscore>`
# (BotFather format: 8–10 digits, colon, ~35 base64-url chars). They get
# embedded in the request URL the `requests` library uses, and the library
# happily includes that URL in connection-error messages like
# "HTTPSConnectionPool(host='api.telegram.org', ...): Max retries exceeded
# with url: /bot1234567:ABC-...-xyz/sendMessage". If the journal ever leaves
# the device (a support bundle, journalctl --upload, a screenshot in a bug
# report) the token leaves with it. The regex matches the token wherever it
# appears in stringified exception text and we replace it before logging.
_BOT_TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{20,}")


def _redact_token(text: str) -> str:
    return _BOT_TOKEN_RE.sub("<TELEGRAM_TOKEN_REDACTED>", text)


def _format_relay_seconds(relay_ms: int) -> str:
    """Render a relay pulse duration for the operator, e.g. 1000 -> "1s",
    1500 -> "1.5s". The `g` format strips trailing zeros so whole-second
    values read cleanly."""
    return f"{relay_ms / 1000:g}s"


def _compute_actuation_threshold_seconds(durations_ms: list[int]) -> float:
    """Adaptive grace period for /open and /close, in seconds.

    Takes the most recent successful actuation durations for a single
    (gate, action) bucket. Returns the upper bound to wait before
    declaring the command unfinished. See the constants block above
    for the formula and tuning rationale.

    During warmup (fewer than `ACTUATION_MIN_SAMPLES` real samples)
    we return the absolute ceiling — there's no data to fit a
    distribution against, so we err toward not timing-out a healthy
    gate. Once warmed, the threshold is `mean + K*stdev` with two
    bounds: a floor of `mean + MIN_SLACK` (so a tight distribution
    can't shrink the slack below physical-jitter levels) and the
    same ceiling (so a slowly-degrading gate can't drag the
    threshold up to comically large values without the operator
    noticing).
    """
    if len(durations_ms) < ACTUATION_MIN_SAMPLES:
        return ACTUATION_ABSOLUTE_CEILING_SECONDS
    n = len(durations_ms)
    mean_s = sum(durations_ms) / n / 1000.0
    # Population stdev — we have the whole sample, not an estimator
    # for an unseen population. Sqrt(variance), no Bessel correction.
    variance = sum((d / 1000.0 - mean_s) ** 2 for d in durations_ms) / n
    stdev_s = variance ** 0.5
    raw = max(
        mean_s + ACTUATION_K_MULTIPLIER * stdev_s,
        mean_s + ACTUATION_MIN_SLACK_SECONDS,
    )
    return min(ACTUATION_ABSOLUTE_CEILING_SECONDS, raw)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


class GateRegistry:
    """Persistent store for gate keys, event history, and replay-protection state."""

    def __init__(self, db_path: str) -> None:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS gate_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    gate_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS registered_gates (
                    gate_id TEXT PRIMARY KEY,
                    lora_key TEXT NOT NULL,
                    last_seq INTEGER NOT NULL DEFAULT 0,
                    registered_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS actuation_cycles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    gate_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS
                    idx_actuation_cycles_gate_action_id
                    ON actuation_cycles (gate_id, action, id);
                """
            )
            # Columns added after the table first shipped. SQLite has no
            # IF NOT EXISTS form for ADD COLUMN, so we attempt each ALTER
            # and swallow the "duplicate column name" OperationalError on
            # already-migrated databases. Idempotent across reboots.
            #   name     — friendly label (added in Session 10)
            #   relay_ms — per-gate relay pulse duration; NOT NULL DEFAULT
            #              so legacy rows backfill to the 1s firmware
            #              default automatically.
            self._add_column_if_missing("name", "TEXT")
            self._add_column_if_missing(
                "relay_ms", f"INTEGER NOT NULL DEFAULT {RELAY_PULSE_DEFAULT_MS}"
            )

    def _add_column_if_missing(self, name: str, decl: str) -> None:
        """Idempotent ALTER TABLE ADD COLUMN for registered_gates.

        Must be called with `self._lock` and an open `self._conn`
        transaction already held (i.e. from `_init_schema`)."""
        try:
            self._conn.execute(
                f"ALTER TABLE registered_gates ADD COLUMN {name} {decl}"
            )
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise

    def cipher_for(self, gate_id: str) -> Fernet | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT lora_key FROM registered_gates WHERE gate_id = ?",
                (gate_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            return Fernet(row[0].encode("utf-8"))
        except ValueError:
            logger.error("Invalid Fernet key on file for gate %s", gate_id)
            return None

    def accept_seq(self, gate_id: str, seq: int) -> bool:
        """Atomically record the sequence number if it advances; reject replays."""
        with self._lock, self._conn:
            updated = self._conn.execute(
                "UPDATE registered_gates SET last_seq = ? "
                "WHERE gate_id = ? AND last_seq < ?",
                (seq, gate_id, seq),
            )
            return updated.rowcount > 0

    def display_name(self, gate_id: str) -> str | None:
        """Friendly name for a registered gate, or None if unset.

        Used by the alert formatter to render "South Pasture (GATE-A1B2)"
        instead of just the opaque gate ID. The lookup runs on every
        alert so we keep it tiny: single-row SELECT, no JOIN.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT name FROM registered_gates WHERE gate_id = ?",
                (gate_id,),
            ).fetchone()
        if row is None or row[0] is None or row[0] == "":
            return None
        return row[0]

    def get_gate(self, gate_id: str) -> dict | None:
        """Full row for a registered gate, plus the latest event timestamp.

        Used by /unpair's confirmation prompt and /status. The
        `last_event_at` value is best-effort — `gate_events` is appended
        from `_dispatch` so a freshly-paired gate that hasn't sent
        anything yet returns None for that field. Returns None if the
        gate isn't registered.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT lora_key, last_seq, registered_at, name, relay_ms "
                "FROM registered_gates WHERE gate_id = ?",
                (gate_id,),
            ).fetchone()
            if row is None:
                return None
            last_event = self._conn.execute(
                "SELECT MAX(timestamp) FROM gate_events WHERE gate_id = ?",
                (gate_id,),
            ).fetchone()
        return {
            "gate_id": gate_id,
            "lora_key": row[0],
            "last_seq": row[1],
            "registered_at": row[2],
            "name": row[3],
            "relay_ms": row[4],
            "last_event_at": last_event[0] if last_event else None,
        }

    def list_gates(self) -> list[dict]:
        """All registered gates, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT gate_id, last_seq, registered_at, name, relay_ms "
                "FROM registered_gates ORDER BY registered_at ASC"
            ).fetchall()
        return [
            {
                "gate_id": r[0],
                "last_seq": r[1],
                "registered_at": r[2],
                "name": r[3],
                "relay_ms": r[4],
            }
            for r in rows
        ]

    def register_gate(
        self, gate_id: str, lora_key: str, name: str | None
    ) -> dict:
        """UPSERT a gate. Resets last_seq to 0 if the key actually changed.

        Returns a result dict with keys:
          existed (bool)           — was the gate already registered?
          key_changed (bool)       — did we replace the Fernet key?
          previous_name (str|None) — name before the call (only if existed)
        Caller (the /pair handler) decides what to say in chat based on
        these — e.g. a key change wipes the gate's seq counter, which
        the operator deserves to know.
        """
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT lora_key, name FROM registered_gates WHERE gate_id = ?",
                (gate_id,),
            ).fetchone()
            existed = existing is not None
            key_changed = existed and existing[0] != lora_key
            previous_name = existing[1] if existed else None
            if existed:
                if key_changed:
                    self._conn.execute(
                        "UPDATE registered_gates "
                        "SET lora_key = ?, name = ?, last_seq = 0 "
                        "WHERE gate_id = ?",
                        (lora_key, name, gate_id),
                    )
                else:
                    self._conn.execute(
                        "UPDATE registered_gates SET name = ? WHERE gate_id = ?",
                        (name, gate_id),
                    )
            else:
                self._conn.execute(
                    "INSERT INTO registered_gates (gate_id, lora_key, name) "
                    "VALUES (?, ?, ?)",
                    (gate_id, lora_key, name),
                )
        return {
            "existed": existed,
            "key_changed": key_changed,
            "previous_name": previous_name,
        }

    def unregister_gate(self, gate_id: str) -> bool:
        """Remove a gate. Returns True if a row was deleted."""
        with self._lock, self._conn:
            result = self._conn.execute(
                "DELETE FROM registered_gates WHERE gate_id = ?",
                (gate_id,),
            )
            return result.rowcount > 0

    def rename_gate(self, gate_id: str, name: str | None) -> bool:
        """Update just the name. Returns True if the gate existed."""
        with self._lock, self._conn:
            result = self._conn.execute(
                "UPDATE registered_gates SET name = ? WHERE gate_id = ?",
                (name, gate_id),
            )
            return result.rowcount > 0

    def get_relay_ms(self, gate_id: str) -> int | None:
        """Configured relay pulse duration (ms) for a gate, or None if
        the gate isn't registered. Falls back to the default if the
        column is somehow NULL (shouldn't happen — the column is NOT
        NULL DEFAULT — but a hand-edited DB might surprise us)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT relay_ms FROM registered_gates WHERE gate_id = ?",
                (gate_id,),
            ).fetchone()
        if row is None:
            return None
        return row[0] if row[0] is not None else RELAY_PULSE_DEFAULT_MS

    def set_relay_ms(self, gate_id: str, relay_ms: int) -> bool:
        """Set a gate's relay pulse duration (ms). Returns True if the
        gate existed. Caller is responsible for clamping to the
        RELAY_PULSE_MIN_MS/MAX_MS range before calling."""
        with self._lock, self._conn:
            result = self._conn.execute(
                "UPDATE registered_gates SET relay_ms = ? WHERE gate_id = ?",
                (relay_ms, gate_id),
            )
            return result.rowcount > 0

    def log_event(self, gate_id: str, event_type: str, message: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO gate_events (gate_id, event_type, message) "
                "VALUES (?, ?, ?)",
                (gate_id, event_type, message),
            )
            # Trim oldest rows per gate to keep the DB bounded over years
            # of operation. Same connection + transaction so the prune
            # is atomic with the insert.
            self._conn.execute(
                "DELETE FROM gate_events "
                "WHERE gate_id = ? AND id NOT IN ("
                "  SELECT id FROM gate_events "
                "  WHERE gate_id = ? "
                "  ORDER BY id DESC LIMIT ?"
                ")",
                (gate_id, gate_id, EVENT_LOG_RETENTION_PER_GATE),
            )

    def record_actuation(
        self, gate_id: str, action: str, duration_ms: int
    ) -> None:
        """Append one successful actuation cycle and prune to retention.

        Called by `BaseStation.lora_command` after a /open or /close
        produces a confirming state-change frame. `duration_ms` is the
        wall-time gap between the command leaving the base and the
        gate's reed-switch transition arriving back — measured by the
        caller against `time.monotonic()` so NTP-step events during the
        cycle can't corrupt the value.

        Only the success path records; noop, send_failed, no_challenge,
        and timeout do not — those samples don't represent actuation.
        """
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO actuation_cycles (gate_id, action, duration_ms) "
                "VALUES (?, ?, ?)",
                (gate_id, action, duration_ms),
            )
            self._conn.execute(
                "DELETE FROM actuation_cycles "
                "WHERE gate_id = ? AND action = ? AND id NOT IN ("
                "  SELECT id FROM actuation_cycles "
                "  WHERE gate_id = ? AND action = ? "
                "  ORDER BY id DESC LIMIT ?"
                ")",
                (gate_id, action, gate_id, action,
                 ACTUATION_RETENTION_PER_BUCKET),
            )

    def recent_actuation_durations_ms(
        self,
        gate_id: str,
        action: str,
        limit: int = ACTUATION_BUFFER_SAMPLES,
    ) -> list[int]:
        """Most recent successful actuation durations for stats input.

        Returns up to `limit` durations in milliseconds, newest first.
        Order doesn't matter for the mean/stdev math, but newest-first
        keeps the implementation cheap (DESC limit + index hit).
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT duration_ms FROM actuation_cycles "
                "WHERE gate_id = ? AND action = ? "
                "ORDER BY id DESC LIMIT ?",
                (gate_id, action, limit),
            ).fetchall()
        return [int(r[0]) for r in rows]

    def last_recorded_state(self, gate_id: str) -> str | None:
        """Most recent open/closed state recorded for this gate, or None.

        Used by _dispatch to decide whether a `status` message represents
        a real state transition worth notifying about, or just an idempotent
        ping (e.g. a `/status GATE-X` reply when the gate has been closed
        the whole time). Queries the gate_events table for the latest
        EVENT_GATE_STATE row and extracts the state from the
        "{msg_type}:{state}" summary format that log_event stores.

        Returns the bare state string ("open"/"closed"), or None if this
        gate has never logged a state event.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT message FROM gate_events "
                "WHERE gate_id = ? AND event_type = ? "
                "ORDER BY id DESC LIMIT 1",
                (gate_id, EVENT_GATE_STATE),
            ).fetchone()
        if row is None or not row[0] or ":" not in row[0]:
            return None
        # Stored as e.g. "alert:open" or "status:closed" — keep just the state.
        return row[0].split(":", 1)[1]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class TelegramSendResult:
    """Outcome of one `sendMessage` attempt, with enough categorization
    that callers can distinguish "operator credentials are wrong"
    (actionable, deserves a setup-mode flip on first boot) from
    "network is flaky right now" (transient, do not flip)."""

    __slots__ = ("ok", "status_code", "reason", "transient")

    def __init__(
        self,
        *,
        ok: bool,
        status_code: int | None = None,
        reason: str | None = None,
        transient: bool = False,
    ) -> None:
        self.ok = ok
        self.status_code = status_code
        # Human-readable, safe-to-show-in-captive-portal explanation.
        # Never contains the bot token (we use `_redact_token` upstream).
        self.reason = reason
        # True when the failure looks like a network blip, not a
        # credential rejection. Callers should not treat transient
        # failures as a reason to flip back to setup mode.
        self.transient = transient


# Telegram's documented error responses for the credential-class of
# failures. Anything in here means "your token / chat ID / bot
# membership is wrong" — i.e. the operator can fix it by re-entering
# values in the captive portal.
_CREDENTIAL_REJECTION_CODES = frozenset({400, 401, 403, 404})


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id

    @property
    def configured(self) -> bool:
        return bool(self._bot_token) and bool(self._chat_id)

    def send(self, message: str) -> TelegramSendResult:
        if not self.configured:
            return TelegramSendResult(ok=False, reason="Telegram credentials not configured", transient=True)
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        try:
            resp = requests.post(
                url,
                json={"chat_id": self._chat_id, "text": message},
                timeout=TELEGRAM_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            redacted = _redact_token(str(exc))
            logger.error("Telegram alert failed: %s", redacted)
            return TelegramSendResult(
                ok=False,
                reason=f"Could not reach Telegram: {redacted}",
                transient=True,
            )

        if resp.status_code == 200:
            return TelegramSendResult(ok=True, status_code=200)

        # Try to lift the human-readable description out of Telegram's
        # JSON error envelope. Falls back to the raw status if the body
        # isn't JSON (e.g. an HTML error page from a captive portal
        # somewhere along the path).
        description = None
        try:
            body = resp.json()
            if isinstance(body, dict):
                description = body.get("description")
        except ValueError:
            description = None
        reason_text = description or f"HTTP {resp.status_code}"
        is_credential_rejection = resp.status_code in _CREDENTIAL_REJECTION_CODES
        logger.error(
            "Telegram alert rejected: %s (status=%s)",
            _redact_token(reason_text),
            resp.status_code,
        )
        return TelegramSendResult(
            ok=False,
            status_code=resp.status_code,
            reason=reason_text,
            transient=not is_credential_rejection,
        )


class _LoRaRequestSlot:
    """Per-gate slot for an in-flight base→gate request.

    `BaseStation.lora_command` / `lora_status_request` populate one of
    these in `_lora_waiters`, send the outbound frame, then block on
    `event` until either the dispatcher routes a matching reply or the
    timeout fires. Single-pending-request-per-gate keeps the routing
    simple — concurrent /open calls against the same gate would race
    the nonce anyway, so we'd want to serialize them at this layer
    regardless.
    """

    __slots__ = ("event", "reply", "expected_types")

    def __init__(self, expected_types: set[str]) -> None:
        self.event = threading.Event()
        self.reply: dict | None = None
        self.expected_types = expected_types


@dataclasses.dataclass(frozen=True)
class PendingAction:
    """One operator's in-flight confirmation-required command.

    The handler stashes a closure here when it issues a `/confirm`
    prompt, and runs it from the `/confirm` path. The closure captures
    every argument the destructive action needs, so the confirm handler
    stays a small generic dispatcher instead of a giant switch on
    `command`. See docs/TELEGRAM.md "Confirmation flow for destructive
    commands".
    """

    token: str
    summary: str                  # human-readable description for the reply
    issued_at: float              # time.monotonic()
    execute: Callable[[], str]    # returns the success reply text


class TelegramCommandChannel:
    """Long-poll thread that turns inbound Telegram commands into device actions.

    Started by `BaseStation.run()` once Telegram is configured. Listens
    for `/pair`, `/unpair`, `/rename`, `/confirm`, `/cancel`, `/status`,
    and `/help` from the configured chat, with the additional rule that
    `/pair` must come in via a 1:1 DM (the Fernet key transits the chat,
    so a group would broadcast it to every member — see TELEGRAM.md
    "The chat-history problem").

    Authorization is "chat-ID only": any message whose `chat.id` matches
    `TELEGRAM_CHAT_ID` is accepted. The operator owns the chat —
    whether they DM the bot 1:1 or invite trusted users to a group is
    their call. There's no separate user-ID allow-list; the chat *is*
    the boundary. `/pair` is still DM-only regardless, because the
    Fernet key transits the chat (see [The chat-history problem] in
    TELEGRAM.md).
    """

    def __init__(
        self,
        *,
        bot_token: str,
        configured_chat_id: str,
        registry: "GateRegistry",
        lora_command: Callable[[str, str], dict] | None = None,
        lora_status_request: Callable[[str], dict] | None = None,
        factory_reset_callback: Callable[[int, str], None] | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._configured_chat_id = configured_chat_id
        self.registry = registry
        # LoRa-driving callbacks come from BaseStation. They're typed as
        # Optional so the channel still functions without the radio
        # (smoke tests, the brief window during boot before
        # `BaseStation.run` opens the port). Handlers that need them
        # surface a clear message when they're absent rather than
        # crashing.
        self._lora_command = lora_command
        self._lora_status_request = lora_status_request
        self._factory_reset_callback = factory_reset_callback
        # Pending /confirm actions, one per user_id. Cleared on confirm,
        # on cancel, or lazily on the next inbound command after the TTL
        # elapses. No external store — process-local memory is fine for
        # 60-second one-shots.
        self._pending: dict[int, PendingAction] = {}
        # Rolling per-user attempt log for /pair rate limiting.
        self._pair_attempts: dict[int, list[float]] = {}
        self._update_offset = 0

    # ------------------------------------------------------------------
    # Thread entry point
    # ------------------------------------------------------------------

    def run_forever(self) -> None:
        logger.info("Telegram command channel started")
        while True:
            try:
                updates = self._poll_once()
            except Exception as exc:
                logger.warning(
                    "Command channel poll failed, backing off %ds: %s",
                    COMMAND_LOOP_BACKOFF_SECONDS,
                    _redact_token(str(exc)),
                )
                time.sleep(COMMAND_LOOP_BACKOFF_SECONDS)
                continue
            for update in updates:
                try:
                    self._process_update(update)
                except Exception as exc:
                    logger.exception(
                        "Unhandled error processing update %s: %s",
                        update.get("update_id"),
                        _redact_token(str(exc)),
                    )

    # ------------------------------------------------------------------
    # Telegram API (intentionally small surface)
    # ------------------------------------------------------------------

    def _poll_once(self) -> list[dict]:
        resp = requests.get(
            f"https://api.telegram.org/bot{self._bot_token}/getUpdates",
            params={
                "timeout": COMMAND_LONGPOLL_TIMEOUT_SECONDS,
                "offset": self._update_offset,
                "allowed_updates": json.dumps(["message"]),
            },
            timeout=COMMAND_HTTP_TIMEOUT_SECONDS,
        )
        body = resp.json()
        if not body.get("ok"):
            raise RuntimeError(
                f"getUpdates returned ok=false: {body.get('description')}"
            )
        updates = body.get("result", []) or []
        if updates:
            self._update_offset = max(u["update_id"] for u in updates) + 1
        return updates

    def _send(self, chat_id: int, text: str) -> None:
        try:
            requests.post(
                f"https://api.telegram.org/bot{self._bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=TELEGRAM_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            logger.warning(
                "sendMessage failed: %s", _redact_token(str(exc))
            )

    def _delete_message(self, chat_id: int, message_id: int) -> None:
        """Best-effort delete of an inbound message. Used to redact the
        operator's `/pair` line containing the Fernet key. Failure is
        logged but never raises — the key is already in Telegram's
        cleartext store regardless."""
        try:
            requests.post(
                f"https://api.telegram.org/bot{self._bot_token}/deleteMessage",
                json={"chat_id": chat_id, "message_id": message_id},
                timeout=TELEGRAM_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            logger.warning(
                "deleteMessage failed: %s", _redact_token(str(exc))
            )

    # ------------------------------------------------------------------
    # Update dispatch + authorization
    # ------------------------------------------------------------------

    def _process_update(self, update: dict) -> None:
        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            return  # ignore plain chat
        if not self._is_authorized(message):
            user = message.get("from", {})
            logger.warning(
                "Ignored command from unauthorized user_id=%s chat_id=%s",
                user.get("id"),
                (message.get("chat") or {}).get("id"),
            )
            return
        self._purge_expired_pending()
        self._handle_command(message, text)

    def _is_authorized(self, message: dict) -> bool:
        chat = message.get("chat") or {}
        # configured_chat_id is loaded from env as a string; getUpdates
        # returns it as a JSON number. Compare as strings to dodge the
        # type mismatch and the negative-group-chat-id wraparound.
        return str(chat.get("id")) == str(self._configured_chat_id)

    def _handle_command(self, message: dict, text: str) -> None:
        chat_id = (message.get("chat") or {}).get("id")
        try:
            parts = shlex.split(text)
        except ValueError:
            # Unclosed quote etc. — surface to the operator instead of
            # silently dropping their input.
            self._send(
                chat_id,
                "❌ Could not parse that command (unmatched quote?). "
                "Send /help for examples.",
            )
            return
        if not parts:
            return
        cmd = parts[0].lower().lstrip("/")
        # Telegram appends the bot username in groups, e.g. /pair@RanchBot.
        cmd = cmd.split("@", 1)[0]
        args = parts[1:]
        handler = {
            "pair": self._cmd_pair,
            "unpair": self._cmd_unpair,
            "rename": self._cmd_rename,
            "relay": self._cmd_relay,
            "open": self._cmd_open,
            "close": self._cmd_close,
            "factory_reset": self._cmd_factory_reset,
            "confirm": self._cmd_confirm,
            "cancel": self._cmd_cancel,
            "status": self._cmd_status,
            "gates": self._cmd_status,  # alias
            "help": self._cmd_help,
            "start": self._cmd_help,    # Telegram's standard onboarding entry
        }.get(cmd)
        if handler is None:
            self._send(
                chat_id,
                f"❓ Unknown command /{cmd}. Send /help for the list.",
            )
            return
        handler(message, args)

    # ------------------------------------------------------------------
    # Pending-action helpers
    # ------------------------------------------------------------------

    def _purge_expired_pending(self) -> None:
        now = time.monotonic()
        expired = [
            uid for uid, action in self._pending.items()
            if now - action.issued_at > PENDING_ACTION_TTL_SECONDS
        ]
        for uid in expired:
            del self._pending[uid]

    def _stash_pending(
        self,
        user_id: int,
        summary: str,
        execute: Callable[[], str],
    ) -> str:
        token = secrets.token_hex(PENDING_TOKEN_BYTES)
        self._pending[user_id] = PendingAction(
            token=token,
            summary=summary,
            issued_at=time.monotonic(),
            execute=execute,
        )
        return token

    # ------------------------------------------------------------------
    # /pair
    # ------------------------------------------------------------------

    def _cmd_pair(self, message: dict, args: list[str]) -> None:
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        user_id = (message.get("from") or {}).get("id")
        message_id = message.get("message_id")
        # DM-only: the Fernet key transits the chat. A group chat would
        # broadcast it to every member, even after a deleteMessage.
        if chat.get("type") != "private":
            self._send(
                chat_id,
                "🚫 /pair must be sent in a private DM with the bot, "
                "not a group chat — the Fernet key would leak to every "
                "group member. DM me directly and try again.",
            )
            return
        if len(args) < 2:
            self._send(
                chat_id,
                '❓ Usage: /pair GATE-XXXX <fernet-key> ["Custom Name"]\n'
                "The key is from the gate's factory sticker. Quote the "
                "name if it contains spaces. Name is optional and "
                "defaults to the gate ID.",
            )
            return
        if not self._rate_limit_pair(user_id):
            self._send(
                chat_id,
                f"⚠️ Too many /pair attempts. Wait an hour and try again "
                f"(max {PAIR_RATE_LIMIT_MAX_ATTEMPTS} per "
                f"{PAIR_RATE_LIMIT_WINDOW_SECONDS // 60} minutes).",
            )
            return
        gate_id = args[0].upper()
        lora_key = args[1]
        name = " ".join(args[2:]).strip() if len(args) > 2 else None
        # Redact the operator's /pair line first — even on validation
        # failure, the key is still sitting in chat history.
        if message_id is not None:
            self._delete_message(chat_id, message_id)
        if not GATE_ID_RE.match(gate_id):
            self._send(
                chat_id,
                f"❌ '{gate_id}' doesn't look like a gate ID. Expected "
                "GATE- followed by 4–12 alphanumerics (see the gate's "
                "factory sticker).",
            )
            return
        try:
            Fernet(lora_key.encode("utf-8"))
        except (ValueError, TypeError):
            self._send(
                chat_id,
                "❌ Invalid Fernet key — keys are 44 url-safe-base64 "
                "characters. Check the gate's factory sticker and "
                "try /pair again. The key was redacted from your "
                "message but is still visible to anyone with access "
                "to Telegram backups for up to 48 hours.",
            )
            return
        if name and len(name) > GATE_NAME_MAX_LEN:
            self._send(
                chat_id,
                f"❌ Name too long ({len(name)} chars; max "
                f"{GATE_NAME_MAX_LEN}). Try /pair again with a "
                "shorter name.",
            )
            return
        existing = self.registry.get_gate(gate_id)
        if existing is None:
            self._do_pair(chat_id, gate_id, lora_key, name)
            return
        # Overwriting an existing gate is destructive (wipes last_seq if
        # the key changes), so require /confirm.
        label = name or existing.get("name") or gate_id
        last_seq = existing.get("last_seq", 0)
        captured_name = name  # closure-bind
        captured_key = lora_key

        def _execute() -> str:
            outcome = self.registry.register_gate(
                gate_id, captured_key, captured_name
            )
            if outcome["key_changed"]:
                return (
                    f"✅ Re-paired {label} ({gate_id}). Fernet key "
                    f"replaced; sequence counter reset from {last_seq} "
                    "to 0."
                )
            return (
                f"✅ Updated {label} ({gate_id}) "
                "(same key, name refreshed)."
            )

        token = self._stash_pending(
            user_id,
            summary=f"/pair {gate_id} (overwrite)",
            execute=_execute,
        )
        self._send(
            chat_id,
            f"⚠️ {gate_id} is already paired (last_seq={last_seq}).\n"
            f"Confirm with `/confirm {token}` within "
            f"{PENDING_ACTION_TTL_SECONDS}s to overwrite. The Fernet "
            "key has already been redacted from your /pair line; "
            "this prompt does not echo it. Send /cancel to abort.",
        )

    def _do_pair(
        self,
        chat_id: int,
        gate_id: str,
        lora_key: str,
        name: str | None,
    ) -> None:
        self.registry.register_gate(gate_id, lora_key, name)
        label = f"{name} ({gate_id})" if name else gate_id
        self._send(chat_id, f"✅ Paired {label}.")

    def _rate_limit_pair(self, user_id: int) -> bool:
        """True if the user has room for one more /pair within the window."""
        now = time.monotonic()
        attempts = self._pair_attempts.setdefault(user_id, [])
        # Drop attempts older than the window in-place.
        attempts[:] = [
            t for t in attempts
            if now - t < PAIR_RATE_LIMIT_WINDOW_SECONDS
        ]
        if len(attempts) >= PAIR_RATE_LIMIT_MAX_ATTEMPTS:
            return False
        attempts.append(now)
        return True

    # ------------------------------------------------------------------
    # /unpair
    # ------------------------------------------------------------------

    def _cmd_unpair(self, message: dict, args: list[str]) -> None:
        chat_id = (message.get("chat") or {}).get("id")
        user_id = (message.get("from") or {}).get("id")
        if len(args) != 1:
            self._send(chat_id, "❓ Usage: /unpair GATE-XXXX")
            return
        gate_id = args[0].upper()
        if not GATE_ID_RE.match(gate_id):
            self._send(chat_id, f"❌ '{gate_id}' isn't a valid gate ID.")
            return
        existing = self.registry.get_gate(gate_id)
        if existing is None:
            self._send(chat_id, f"❌ {gate_id} is not registered.")
            return
        name = existing.get("name") or gate_id
        last_seen = existing.get("last_event_at") or "never"

        def _execute() -> str:
            removed = self.registry.unregister_gate(gate_id)
            if not removed:
                return f"⚠️ {gate_id} was already gone (race with another /unpair)."
            return (
                f"✅ Removed {name} ({gate_id}). Event history "
                "kept; re-pair anytime with /pair."
            )

        token = self._stash_pending(
            user_id,
            summary=f"/unpair {gate_id}",
            execute=_execute,
        )
        self._send(
            chat_id,
            f"⚠️ Confirm with `/confirm {token}` within "
            f"{PENDING_ACTION_TTL_SECONDS}s.\n"
            f"This will remove {name} ({gate_id}) "
            f"(last seen {last_seen}, last_seq="
            f"{existing.get('last_seq', 0)}). Gate hardware will be "
            "unaffected; you can re-pair anytime with /pair. Send "
            "/cancel to abort.",
        )

    # ------------------------------------------------------------------
    # /rename
    # ------------------------------------------------------------------

    def _cmd_rename(self, message: dict, args: list[str]) -> None:
        chat_id = (message.get("chat") or {}).get("id")
        if len(args) < 2:
            self._send(
                chat_id,
                '❓ Usage: /rename GATE-XXXX "New Name"\n'
                "Send the name without quotes for a single word.",
            )
            return
        gate_id = args[0].upper()
        if not GATE_ID_RE.match(gate_id):
            self._send(chat_id, f"❌ '{gate_id}' isn't a valid gate ID.")
            return
        name = " ".join(args[1:]).strip()
        if not name:
            self._send(chat_id, "❌ Name cannot be empty. Use /rename "
                                "GATE-XXXX \"New Name\".")
            return
        if len(name) > GATE_NAME_MAX_LEN:
            self._send(
                chat_id,
                f"❌ Name too long ({len(name)} chars; max "
                f"{GATE_NAME_MAX_LEN}).",
            )
            return
        if self.registry.rename_gate(gate_id, name):
            self._send(chat_id, f"✅ Renamed {gate_id} → {name}.")
        else:
            self._send(
                chat_id,
                f"❌ {gate_id} is not registered. Pair it first with /pair.",
            )

    # ------------------------------------------------------------------
    # /relay — per-gate relay pulse duration
    # ------------------------------------------------------------------

    def _cmd_relay(self, message: dict, args: list[str]) -> None:
        """Show or set how long the gate holds its relay closed.

        `/relay GATE-XXXX`           → report the current press time
        `/relay GATE-XXXX <seconds>` → set it (e.g. 1.5)

        Kept separate from /pair so the pairing command stays focused on
        the key + name. Not a destructive action — applies immediately
        like /rename, no /confirm. The value is stored per-gate and
        ships in the next /open or /close command frame.
        """
        chat_id = (message.get("chat") or {}).get("id")
        min_s = _format_relay_seconds(RELAY_PULSE_MIN_MS)
        max_s = _format_relay_seconds(RELAY_PULSE_MAX_MS)
        default_s = _format_relay_seconds(RELAY_PULSE_DEFAULT_MS)
        if not args or len(args) > 2:
            self._send(
                chat_id,
                "❓ Usage: /relay GATE-XXXX [seconds]\n"
                "  /relay GATE-XXXX        show the gate's relay press time\n"
                "  /relay GATE-XXXX 1.5    set it to 1.5 seconds\n"
                f"Range {min_s}–{max_s}; gate default {default_s}.",
            )
            return
        gate_id = args[0].upper()
        if not GATE_ID_RE.match(gate_id):
            self._send(chat_id, f"❌ '{gate_id}' isn't a valid gate ID.")
            return
        existing = self.registry.get_gate(gate_id)
        if existing is None:
            self._send(
                chat_id,
                f"❌ {gate_id} is not registered. Pair it first with /pair.",
            )
            return
        label = existing.get("name") or gate_id
        if len(args) == 1:
            current = existing.get("relay_ms") or RELAY_PULSE_DEFAULT_MS
            self._send(
                chat_id,
                f"🔘 {label} ({gate_id}) relay press time is "
                f"{_format_relay_seconds(current)}. Change it with "
                f"/relay {gate_id} <seconds>.",
            )
            return
        try:
            seconds = float(args[1])
            # Reject nan/inf here: they parse as floats but blow up the
            # int(round(...)) below with ValueError/OverflowError, which
            # would otherwise escape to the dispatch loop and leave the
            # operator with no reply at all.
            if not math.isfinite(seconds):
                raise ValueError
        except ValueError:
            self._send(
                chat_id,
                f"❌ '{args[1]}' isn't a number. Give the press time in "
                f"seconds, e.g. /relay {gate_id} 1.5.",
            )
            return
        relay_ms = int(round(seconds * 1000))
        if relay_ms < RELAY_PULSE_MIN_MS or relay_ms > RELAY_PULSE_MAX_MS:
            self._send(
                chat_id,
                f"❌ Press time must be between {min_s} and {max_s}.",
            )
            return
        if self.registry.set_relay_ms(gate_id, relay_ms):
            self._send(
                chat_id,
                f"🔘 {label} ({gate_id}) relay press time set to "
                f"{_format_relay_seconds(relay_ms)}. Takes effect on the "
                "next /open or /close.",
            )
        else:
            self._send(chat_id, f"❌ {gate_id} is not registered.")

    # ------------------------------------------------------------------
    # /confirm + /cancel
    # ------------------------------------------------------------------

    def _cmd_confirm(self, message: dict, args: list[str]) -> None:
        chat_id = (message.get("chat") or {}).get("id")
        user_id = (message.get("from") or {}).get("id")
        if len(args) != 1:
            self._send(
                chat_id,
                "❓ Usage: /confirm <token>  (token from the "
                "confirmation prompt)",
            )
            return
        pending = self._pending.get(user_id)
        if pending is None:
            self._send(
                chat_id,
                "ℹ️ Nothing to confirm — no pending action for you (or "
                "it already expired).",
            )
            return
        if time.monotonic() - pending.issued_at > PENDING_ACTION_TTL_SECONDS:
            del self._pending[user_id]
            self._send(
                chat_id,
                f"⚠️ That token expired (60s limit on {pending.summary}). "
                "Re-issue the command if you still want to run it.",
            )
            return
        if not secrets.compare_digest(args[0], pending.token):
            # Don't echo the expected token. Treat as an honest typo;
            # only outright timeout / cancel removes the pending state.
            self._send(
                chat_id,
                "⚠️ Token doesn't match the most recent prompt. Re-check "
                "the 4-char code, or /cancel to start over.",
            )
            return
        # Single-use: clear before running so a slow execute() can't be
        # replayed by a quick second /confirm.
        del self._pending[user_id]
        try:
            reply = pending.execute()
        except Exception as exc:
            logger.exception(
                "Pending action %s failed: %s",
                pending.summary,
                _redact_token(str(exc)),
            )
            reply = (
                f"❌ {pending.summary} failed. See the device log "
                "for details."
            )
        # A closure may return None to mean "I already sent my own
        # reply and the dispatcher shouldn't double-send" — e.g.
        # /factory_reset, which fires its ack *before* the disruptive
        # work so the operator sees the ack before Wi-Fi drops.
        if reply is not None:
            self._send(chat_id, reply)

    def _cmd_cancel(self, message: dict, args: list[str]) -> None:
        chat_id = (message.get("chat") or {}).get("id")
        user_id = (message.get("from") or {}).get("id")
        pending = self._pending.pop(user_id, None)
        if pending is None:
            self._send(chat_id, "ℹ️ Nothing pending to cancel.")
            return
        self._send(chat_id, f"🛑 Cancelled {pending.summary}.")

    # ------------------------------------------------------------------
    # /status + /help
    # ------------------------------------------------------------------

    def _cmd_status(self, message: dict, args: list[str]) -> None:
        chat_id = (message.get("chat") or {}).get("id")
        # /status with no arg = registry dump. /status GATE-XXXX = live
        # status_req over LoRa (rate-limited by the gate's 2s per-gate
        # challenge floor, but we don't double-rate-limit here).
        if args:
            self._cmd_status_one(chat_id, args[0].upper())
            return
        # Header carries device identity. With multiple base stations
        # paged into the same Telegram chat (a possibility the operator
        # chooses; we don't enforce one base per chat), the device ID
        # and SSID disambiguate which one is replying. The SSID also
        # makes "is this base still on the right Wi-Fi?" answerable
        # without SSHing in.
        base_id = socket.gethostname()
        ssid = _current_wifi_ssid() or "(unknown)"
        header = f"📋 Base: {base_id}  •  Wi-Fi: {ssid}"

        gates = self.registry.list_gates()
        if not gates:
            self._send(
                chat_id,
                f"{header}\n\nNo gates registered. Pair one with "
                "/pair GATE-XXXX <key> [name].",
            )
            return
        # Per-gate state via live LoRa status_req. Blocks for up to
        # LORA_DEFAULT_REPLY_TIMEOUT_SECONDS per gate worst case (all
        # gates offline). Sequential because the LoRa serial is single-
        # threaded and _lora_tx_lock would serialize concurrent attempts
        # anyway. Typical 1-3 gates per install → 3-15s round trip.
        lines = [header, "", f"{len(gates)} gate(s) registered:"]
        for g in gates:
            label = g["name"] or g["gate_id"]
            gate_id = g["gate_id"]
            id_part = f" ({gate_id})" if g["name"] else ""
            state_text = self._format_gate_state(gate_id)
            lines.append(f"  • {label}{id_part}: {state_text}")
            lines.append(f"      {self._format_gate_timeouts(gate_id)}")
        self._send(chat_id, "\n".join(lines))

    def _format_gate_state(self, gate_id: str) -> str:
        """Render the per-gate state cell for /status.

        Live LoRa query first; on success returns the canonical truth
        ("🔓 OPEN" / "🔒 CLOSED"). On any failure (no LoRa, send
        failure, timeout, gate offline), falls back to the latest
        state from the event log clearly marked as not-live ("last
        seen") so the operator doesn't mistake stale data for fresh
        truth. Returns "❓ no data" when there's neither a live reply
        nor any prior event-log state.
        """
        live: str | None = None
        if self._lora_status_request is not None:
            result = self._lora_status_request(gate_id)
            if result.get("outcome") == "ok":
                state = (result.get("reply") or {}).get("state")
                if isinstance(state, str):
                    live = state
        if live in ("open", "closed"):
            emoji = "🔓" if live == "open" else "🔒"
            return f"{emoji} {live.upper()}"
        # No live truth — show the latest event-log state with a
        # qualifier so the operator knows it might be stale.
        prior = self.registry.last_recorded_state(gate_id)
        if prior in ("open", "closed"):
            emoji = "🔓" if prior == "open" else "🔒"
            return f"{emoji} last seen {prior.upper()} (no live reply)"
        return "❓ no data (no live reply)"

    def _format_gate_timeouts(self, gate_id: str) -> str:
        """Render the per-gate timing metadata for /status.

        Returns a single line like
            "⏱ open ~15s (n=14) · close ~16s (n=18) · 🔘 press 1.5s"
        The grace periods are adaptive — "(warmup)" replaces "(n=X)"
        when fewer than ACTUATION_MIN_SAMPLES real samples back the
        threshold, so the operator can tell whether the value was
        learned from this gate's history or is the default ceiling. The
        trailing "🔘 press" cell is the configured relay pulse duration
        (set via /relay), which the operator tunes rather than the
        device learning it.
        """
        parts = []
        for action in ("open", "close"):
            durations = self.registry.recent_actuation_durations_ms(
                gate_id, action
            )
            threshold = _compute_actuation_threshold_seconds(durations)
            if len(durations) < ACTUATION_MIN_SAMPLES:
                tag = "warmup"
            else:
                tag = f"n={len(durations)}"
            parts.append(f"{action} ~{int(round(threshold))}s ({tag})")
        relay_ms = self.registry.get_relay_ms(gate_id) or RELAY_PULSE_DEFAULT_MS
        parts.append(f"🔘 press {_format_relay_seconds(relay_ms)}")
        return "⏱ " + " · ".join(parts)

    def _cmd_status_one(self, chat_id: int, gate_id: str) -> None:
        if not GATE_ID_RE.match(gate_id):
            self._send(chat_id, f"❌ '{gate_id}' isn't a valid gate ID.")
            return
        if self._lora_status_request is None:
            self._send(
                chat_id,
                "❌ LoRa radio not ready; live status is unavailable.",
            )
            return
        existing = self.registry.get_gate(gate_id)
        if existing is None:
            self._send(chat_id, f"❌ {gate_id} is not registered.")
            return
        result = self._lora_status_request(gate_id)
        label = existing.get("name") or gate_id
        outcome = result.get("outcome")
        if outcome == "ok":
            state = (result.get("reply") or {}).get("state", "unknown")
            # Lead with the state emoji so /status replies match the
            # look of unsolicited state-change pings from _dispatch.
            emoji = "🔓" if state == "open" else "🔒" if state == "closed" else "ℹ️"
            header = f"{emoji} {label} ({gate_id}): {state.upper()} (live)."
        else:
            header = self._lora_failure_text(label, gate_id, outcome)
        # The adaptive grace periods are stable metadata about the gate,
        # not live data — show them in both success and failure replies
        # so the operator always knows where their commands stand and
        # how long they'd wait next time.
        self._send(
            chat_id,
            f"{header}\n{self._format_gate_timeouts(gate_id)}",
        )

    # ------------------------------------------------------------------
    # /open + /close (LoRa-driven)
    # ------------------------------------------------------------------

    def _cmd_open(self, message: dict, args: list[str]) -> None:
        self._cmd_drive_gate(message, args, action="open")

    def _cmd_close(self, message: dict, args: list[str]) -> None:
        self._cmd_drive_gate(message, args, action="close")

    def _cmd_drive_gate(
        self, message: dict, args: list[str], *, action: str
    ) -> None:
        chat_id = (message.get("chat") or {}).get("id")
        if len(args) > 1:
            self._send(
                chat_id,
                f"❓ Usage: /{action} [GATE-XXXX]  "
                "(gate ID optional when only one gate is paired)",
            )
            return
        if self._lora_command is None:
            self._send(
                chat_id,
                "❌ LoRa radio not ready; gate control is unavailable.",
            )
            return
        if args:
            gate_id = args[0].upper()
            if not GATE_ID_RE.match(gate_id):
                self._send(chat_id, f"❌ '{gate_id}' isn't a valid gate ID.")
                return
            existing = self.registry.get_gate(gate_id)
            if existing is None:
                self._send(
                    chat_id,
                    f"❌ {gate_id} is not registered. "
                    "Pair it first with /pair.",
                )
                return
        else:
            # No gate specified — auto-pick the registered gate if there
            # is exactly one. With zero or many, we can't safely guess
            # which the operator meant (and /open the wrong gate would
            # actually move a physical thing), so we surface a list.
            gates = self.registry.list_gates()
            if not gates:
                self._send(
                    chat_id,
                    f"❌ No gates registered. Pair one with "
                    f"/pair GATE-XXXX <key> [name] before /{action}.",
                )
                return
            if len(gates) > 1:
                names = ", ".join(
                    (g["name"] or g["gate_id"]) for g in gates
                )
                self._send(
                    chat_id,
                    f"❓ Multiple gates paired ({names}). Specify "
                    f"which: /{action} GATE-XXXX",
                )
                return
            existing = gates[0]
            gate_id = existing["gate_id"]
            # list_gates returns a row without last_event_at; that's
            # only consulted by /unpair's prompt, so we don't need it.
        label = existing.get("name") or gate_id
        result = self._lora_command(gate_id, action)
        outcome = result.get("outcome")
        if outcome == "ok":
            # Use the state emoji that matches what just happened, so
            # the command reply looks like the corresponding state
            # notification from _dispatch.
            emoji = "🔓" if action == "open" else "🔒"
            verb = "Opened" if action == "open" else "Closed"
            self._send(chat_id, f"{emoji} {verb} {label} ({gate_id}).")
            return
        if outcome == "noop":
            current = (
                (result.get("reply") or {}).get("result", "")
                .replace("already_", "")
                or "unchanged"
            )
            self._send(
                chat_id,
                f"ℹ️ {label} ({gate_id}) was already {current}; "
                "no relay pulse fired.",
            )
            return
        self._send(
            chat_id,
            self._lora_failure_text(label, gate_id, outcome),
        )

    @staticmethod
    def _lora_failure_text(label: str, gate_id: str, outcome: str) -> str:
        """Human-readable message for the non-success LoRa outcomes."""
        if outcome == "not_registered":
            return f"❌ {gate_id} is not registered."
        if outcome == "no_challenge":
            return (
                f"❌ {label} ({gate_id}) did not answer the challenge. "
                "Is the gate powered on and in LoRa range?"
            )
        if outcome == "timeout":
            return (
                f"⚠️ {label} ({gate_id}) accepted the challenge but "
                f"did not confirm. The action may still have fired — "
                f"send `/status {gate_id}` to check."
            )
        if outcome == "send_failed":
            return (
                f"❌ Could not transmit to {gate_id} — the LoRa serial "
                "write failed. Check the device log; this is a "
                "base-side problem, not the gate."
            )
        return f"❌ Unknown LoRa outcome '{outcome}' for {gate_id}."

    # ------------------------------------------------------------------
    # /factory_reset
    # ------------------------------------------------------------------

    def _cmd_factory_reset(self, message: dict, args: list[str]) -> None:
        chat_id = (message.get("chat") or {}).get("id")
        user_id = (message.get("from") or {}).get("id")
        if args:
            self._send(
                chat_id,
                "❓ /factory_reset takes no arguments. Send /factory_reset "
                "and then /confirm <token> to proceed.",
            )
            return
        if self._factory_reset_callback is None:
            self._send(
                chat_id,
                "❌ Factory reset is not available (callback not wired). "
                "Re-flash the SD card manually.",
            )
            return
        ssid = _current_wifi_ssid()
        gates = self.registry.list_gates()
        gate_lines = ", ".join(
            (g["name"] or g["gate_id"]) for g in gates
        ) if gates else "(none)"
        ssid_text = ssid or "(unknown)"
        captured_chat = chat_id

        def _execute() -> str | None:
            ack = (
                "🔄 Resetting now. You will lose this chat until the "
                "device joins a new Wi-Fi via the BaseStation_Setup "
                "captive portal."
            )
            # Fire the ack synchronously so Telegram sees it before the
            # disrupt thread takes Wi-Fi down. We don't gate the reset
            # on send success: the operator already confirmed the
            # intent, and aborting on a transient send failure would
            # silently strand them. They asked for the reset.
            self._send(captured_chat, ack)
            self._factory_reset_callback(captured_chat, ssid or "")
            return None  # dispatcher: do not double-reply

        token = self._stash_pending(
            user_id,
            summary="/factory_reset",
            execute=_execute,
        )
        body = (
            f"⚠️ Confirm with `/confirm {token}` within "
            f"{PENDING_ACTION_TTL_SECONDS}s.\n\n"
            "This will wipe:\n"
            f"  • Wi-Fi credentials (currently: \"{ssid_text}\")\n"
            "  • Telegram bot token and chat ID\n"
            f"  • {len(gates)} paired gate(s): {gate_lines}\n"
            "  • Event history\n\n"
            "The device will then reboot the captive portal AP "
            "(BaseStation_Setup) and you'll need to re-enter all of "
            "the above. Send /cancel to abort."
        )
        self._send(chat_id, body)

    def _cmd_help(self, message: dict, args: list[str]) -> None:
        chat_id = (message.get("chat") or {}).get("id")
        text = (
            "❓ Ranch base station commands:\n\n"
            "Gate management\n"
            "  /pair GATE-XXXX <fernet-key> [\"Name\"]\n"
            "      Register a gate. DM only — the key is sensitive. "
            "Name is optional.\n"
            "  /unpair GATE-XXXX\n"
            "      Remove a gate. Requires /confirm.\n"
            "  /rename GATE-XXXX \"New Name\"\n"
            "      Change a gate's display name.\n"
            "  /relay GATE-XXXX [seconds]\n"
            "      Show or set how long the gate holds its relay closed "
            "to trigger the opener (default 1s). Affects /open and "
            "/close.\n"
            "  /status\n"
            "      List registered gates with their adaptive open/close "
            "grace periods.\n"
            "  /status GATE-XXXX\n"
            "      Live state query for one gate (over LoRa), with its "
            "current grace periods.\n\n"
            "Gate control\n"
            "  /open [GATE-XXXX]\n"
            "      Open a gate. Drives the LoRa challenge / command "
            "sequence. Gate ID is optional when only one gate is "
            "paired.\n"
            "  /close [GATE-XXXX]\n"
            "      Close a gate. Gate ID is optional when only one "
            "gate is paired.\n\n"
            "Confirmation flow\n"
            "  /confirm <token>\n"
            "      Run the most recently prompted destructive action.\n"
            "  /cancel\n"
            "      Abort any pending /confirm.\n\n"
            "Device-wide\n"
            "  /factory_reset\n"
            "      Wipe Wi-Fi, Telegram, and gate state; reboot into "
            "the captive portal. Requires /confirm.\n"
            "  /help\n"
            "      Show this text.\n\n"
            "Pairing keys transit Telegram in cleartext and may persist "
            "in Telegram's backups for up to 48 hours. Re-flash any "
            "gate whose key was paired this way if you ever believe "
            "this chat is compromised. Anyone in this chat can issue "
            "any of these commands — the chat itself is the auth "
            "boundary."
        )
        self._send(chat_id, text)


def _current_wifi_ssid() -> str | None:
    """Best-effort lookup of the currently-active Wi-Fi connection name.

    Used by /factory_reset to show the operator which network is about
    to be forgotten and to feed `nmcli connection delete`. Returns None
    on any kind of failure (no NM, no active connection, parse error) —
    the caller falls back to "(unknown)" in the prompt rather than
    aborting the reset.
    """
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show", "--active"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("nmcli active-connection probe failed: %s", exc)
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        # NM's -t output is colon-delimited but allows escaped colons
        # inside NAME with a backslash. We only need the first two
        # fields and don't expect Wi-Fi SSIDs to contain colons in
        # practice; fall back to a simple rsplit on the type suffix.
        if line.endswith(":802-11-wireless"):
            return line[: -len(":802-11-wireless")].replace("\\:", ":")
    return None


def _clock_is_plausible() -> bool:
    """True if the system clock looks like real wall time, not a stale
    persisted value. This is the only condition TLS actually cares about
    — it doesn't matter HOW the clock got correct, only that it is."""
    return time.time() > PLAUSIBILITY_SENTINEL_EPOCH


def _clock_synced_via_systemd() -> bool:
    """Cheap check for systemd's view of clock-sync state. Kept as a
    secondary signal; the primary check is _clock_is_plausible()."""
    if os.path.exists(TIMESYNC_SYNCHRONIZED_MARKER):
        return True
    try:
        result = subprocess.run(
            ["timedatectl", "show", "--property=NTPSynchronized", "--value"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0 and result.stdout.strip() == "yes"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _force_ntp_sync() -> bool:
    """Manually query NTP and set the system clock with `date -s`.

    We speak NTP wire format directly — stdlib socket + struct, no
    extra packages, no dependency on glibc's getaddrinfo or NSS or
    /etc/resolv.conf or any of systemd's machinery.

    WHY THIS EXISTS AS A FALLBACK
    -----------------------------
    Buildroot images using NetworkManager (not systemd-networkd) have
    a known DNS-resolution failure inside systemd-timesyncd's sandbox.
    The root cause: Buildroot symlinks /etc/resolv.conf → /tmp/resolv.conf,
    while timesyncd runs with PrivateTmp=yes (its own empty /tmp), so
    the symlink dangles inside the service's namespace. Every hostname
    lookup fails with "Temporary failure in name resolution" and
    timesyncd never contacts a server.

    The primary fix lives in:
      ranch_os/rootfs-overlay/etc/systemd/system/systemd-timesyncd.service.d/ranch-os.conf
    (a drop-in that sets PrivateTmp=no so the symlink resolves).

    This function is the secondary fix — if the drop-in ever stops
    working (Buildroot symlink layout changes, systemd version bump,
    something else lurking), we still get a correct clock. Belt and
    braces; the cost is ~30 lines and one extra UDP round-trip on
    first boot.
    """
    for server in NTP_FALLBACK_SERVERS:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(NTP_QUERY_TIMEOUT_SECONDS)
            # NTP v3, mode=client (LI=0, VN=3, Mode=3 → 0x1b), 47 zero bytes.
            packet = b"\x1b" + 47 * b"\x00"
            sock.sendto(packet, (server, 123))
            data, _ = sock.recvfrom(1024)
            sock.close()
        except (OSError, socket.timeout) as exc:
            logger.debug("NTP query to %s failed: %s", server, exc)
            continue

        # The transmit-timestamp field is at offset 40 (4 bytes seconds
        # since 1900-01-01). Convert to Unix epoch by subtracting the
        # 70-year offset.
        ntp_secs = struct.unpack("!I", data[40:44])[0]
        epoch = ntp_secs - 2208988800
        if epoch < NTP_REPLY_FLOOR_EPOCH:
            logger.warning("NTP %s returned implausible time epoch=%d; skipping",
                           server, epoch)
            continue

        try:
            subprocess.run(
                ["date", "-u", "-s", f"@{epoch}"],
                capture_output=True, check=True, timeout=5,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            logger.error("date -s failed: %s", exc)
            return False

        logger.info("System clock set via manual NTP (server=%s, epoch=%d)",
                    server, epoch)
        return True
    return False


def _wait_for_clock_sync() -> bool:
    """Get the system clock to a plausible wall time before TLS work.

    The success condition is the wall-clock time itself, not systemd's
    NTPSynchronized flag — TLS doesn't care which tool moved the clock,
    only that it lands in roughly-now. systemd-timesyncd has a known
    bug on NetworkManager-only systems where it stays Idle forever; the
    plausibility check lets us trust a `date -s`-style fix.

    Three-step strategy:
      1. If the clock is already plausible, we're done.
      2. Kick systemd-timesyncd and give it a short window.
      3. If still no, manually speak NTP ourselves and `date -s` the
         result. timesyncd stays running for long-term drift correction.
    """
    if _clock_is_plausible():
        logger.info("Clock is already plausibly correct")
        return True

    try:
        subprocess.run(
            ["systemctl", "restart", "systemd-timesyncd.service"],
            capture_output=True, timeout=10, check=False,
        )
        logger.info("Kicked systemd-timesyncd to start syncing")
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Could not restart systemd-timesyncd: %s", exc)

    # Short window for timesyncd to do its thing.
    timesyncd_deadline = time.monotonic() + 15
    while time.monotonic() < timesyncd_deadline:
        if _clock_is_plausible() or _clock_synced_via_systemd():
            logger.info("Clock is NTP-synced via systemd-timesyncd")
            return True
        time.sleep(CLOCK_SYNC_POLL_INTERVAL_SECONDS)

    logger.warning(
        "systemd-timesyncd didn't sync in 15s — falling back to manual NTP"
    )
    if _force_ntp_sync():
        # _force_ntp_sync ran `date -s`. Confirm the clock now looks
        # plausible — if NTP returned a sane value, this will pass.
        return _clock_is_plausible()
    return False


def _is_first_setup_attempt() -> bool:
    """True if no Telegram ping has ever succeeded on this device.

    Drives the "fail fast back into the captive portal" behavior: only
    the very first online ping after captive-portal completion can flip
    us back into setup mode. After one good round-trip we treat
    Telegram failures as steady-state issues that need operator
    attention, not automatic device resets.
    """
    return not os.path.exists(SETUP_VALIDATED_PATH)


def _mark_setup_validated() -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    # touch(); content irrelevant, only existence is checked.
    with open(SETUP_VALIDATED_PATH, "w", encoding="utf-8") as handle:
        handle.write("ok\n")


def _flip_to_setup_mode(reason: str) -> None:
    """Hand the device back to the captive portal with a banner-ready reason.

    Called when Telegram rejects credentials on the very first ping
    after setup. Equivalent to what ranch-wifi-watchdog does when Wi-Fi
    is broken for too long, but for the credential-typo case.

    The order matters: write the reason file *first*, then unlink the
    config (which arms the provisioner's ConditionPathExists), then
    enqueue the provisioner start, then exit cleanly so systemd doesn't
    try to restart us.
    """
    logger.warning(
        "First-time Telegram ping was rejected (%s) — flipping back to setup mode",
        reason,
    )
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(SETUP_ERROR_PATH, "w", encoding="utf-8") as handle:
            handle.write(reason.strip()[:500] + "\n")
    except OSError as exc:
        logger.error("Could not write %s: %s", SETUP_ERROR_PATH, exc)
    try:
        os.unlink(CONFIG_PATH)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.error("Could not remove %s: %s — aborting flip", CONFIG_PATH, exc)
        return
    # --no-block so systemctl doesn't wait for base-provision to come up
    # before returning — and so it doesn't try to stop us synchronously
    # while we're still running.
    subprocess.run(
        ["systemctl", "--no-block", "start", "base-provision.service"],
        check=False, capture_output=True,
    )
    # Clean exit — Restart=on-failure won't fire on code 0, so systemd
    # will simply mark us inactive and the provisioner takes over.
    raise SystemExit(0)


# Tunable so the smoke test can shrink the post-ack flush. The default
# is generous (2s) because Telegram's sendMessage HTTP call ends well
# before the receiving client renders the message, and we want the
# operator to see "✓ Resetting now..." before Wi-Fi drops.
FACTORY_RESET_ACK_FLUSH_SECONDS = 2.0


def _perform_factory_reset(ssid: str) -> None:
    """Wipe device state and hand wlan0 back to the captive portal AP.

    Ordering is load-bearing — see TELEGRAM.md "The disconnect-before-
    reset ordering problem":
      1. Sleep briefly so the ack message that the operator just saw
         actually flushes through Telegram before we drop Wi-Fi.
      2. Unlink events.db and base_config.env. Doing this first means
         that even if step 4 partially fails, base-station.service
         can't come back (its ConditionPathExists= falls through) —
         the AP path is the only safe restart.
      3. Delete the active Wi-Fi connection profile via nmcli so the
         station mode releases wlan0 cleanly. If the SSID lookup
         failed earlier we skip this; base-provision's AP-up will
         still evict the active connection, just less tidily.
      4. Kick base-provision.service via --no-block. Its
         ConditionPathExists=!base_config.env now passes; it claims
         wlan0 in AP mode.
      5. os._exit(0) the whole process. systemd sees a clean exit and
         doesn't restart us; base-provision is already up.

    Runs in a daemon thread spawned from `BaseStation._begin_factory_reset`.
    """
    logger.warning("Factory reset starting (ssid=%r)", ssid)
    time.sleep(FACTORY_RESET_ACK_FLUSH_SECONDS)
    for path in (DB_PATH, CONFIG_PATH):
        try:
            os.unlink(path)
            logger.info("Removed %s", path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.error("Could not remove %s: %s", path, exc)
    if ssid:
        # nmcli accepts the connection NAME; it's what we stored when
        # the captive portal called `nmcli device wifi connect`. Best
        # effort — if the profile is already gone, that's fine.
        subprocess.run(
            ["nmcli", "connection", "delete", ssid],
            check=False, capture_output=True, timeout=10,
        )
        logger.info("Deleted nmcli connection profile %r", ssid)
    subprocess.run(
        ["systemctl", "--no-block", "start", "base-provision.service"],
        check=False, capture_output=True,
    )
    logger.info("Factory reset complete; exiting so systemd marks us inactive")
    # os._exit because we're in a daemon thread — sys.exit would only
    # raise SystemExit on this thread, leaving the main listen loop
    # running. _exit terminates the whole process immediately.
    os._exit(0)


class BaseStation:
    def __init__(
        self,
        *,
        serial_port: str,
        baud_rate: int,
        registry: GateRegistry,
        notifier: TelegramNotifier,
    ) -> None:
        self.serial_port = serial_port
        self.baud_rate = baud_rate
        self.registry = registry
        self.notifier = notifier
        self.lora: serial.Serial | None = None
        # Serial port is single-writer (LoRa modules expect coherent
        # frames). The alert-reading thread reads from the same fd, but
        # only writes happen under the lock — reads don't conflict.
        self._lora_tx_lock = threading.Lock()
        # Pending request slots, keyed by gate_id. Populated by
        # lora_command / lora_status_request before sending; cleared by
        # the same caller in its finally. Mutated under the waiters
        # lock so _dispatch can route safely from the listen thread.
        self._lora_waiters: dict[str, _LoRaRequestSlot] = {}
        self._lora_waiters_lock = threading.Lock()

    def run(self) -> None:
        try:
            self.lora = serial.Serial(
                self.serial_port, self.baud_rate, timeout=SERIAL_READ_TIMEOUT_SECONDS
            )
        except serial.SerialException as exc:
            logger.critical("Could not open LoRa radio: %s", exc)
            raise SystemExit(1)

        logger.info("Base station listening on %s", self.serial_port)

        # Wait briefly for NTP before the Telegram ping so TLS can verify
        # the api.telegram.org certificate (which would otherwise look
        # "not yet valid" on a freshly-booted Pi). If NTP never syncs we
        # just skip the ping — the rest of the service is time-independent.
        if _wait_for_clock_sync():
            # Include the device ID (hostname is set to BASE-XXXX by
            # ranch-set-hostname at boot) so an operator with multiple
            # base stations on multiple Telegram chats can tell at a
            # glance which device just came online.
            result = self.notifier.send(
                f"\N{satellite antenna} Gate Monitor Base Station "
                f"{socket.gethostname()} is Online."
            )
            if result.ok:
                # First successful ping on this device → arm the
                # sentinel so future Telegram failures don't
                # automatically reset us back to setup mode.
                if _is_first_setup_attempt():
                    _mark_setup_validated()
                    logger.info(
                        "First-time Telegram ping succeeded; setup confirmed"
                    )
            elif _is_first_setup_attempt() and not result.transient:
                # Operator typed bad credentials in the captive portal.
                # Flip back into setup mode with a banner so they don't
                # wait the 10-minute watchdog window to find out.
                # NOTE: this call raises SystemExit and does not return.
                _flip_to_setup_mode(
                    result.reason
                    or f"Telegram returned HTTP {result.status_code}"
                )
            # Transient failures or steady-state rejections both fall
            # through without touching the sentinel — the device keeps
            # running and the operator can intervene at their leisure.
        else:
            logger.warning(
                "Clock not synced after %ds; skipping online ping. "
                "LoRa events that fire alerts will still attempt Telegram.",
                CLOCK_SYNC_TIMEOUT_SECONDS,
            )

        self._start_command_channel()

        try:
            self._listen_forever()
        finally:
            self.lora.close()

    # ----------------------------------------------------------------
    # Outbound LoRa: drives /open, /close, /status GATE-XXXX.
    #
    # The wire protocol is documented in docs/TELEGRAM.md "Bidirectional
    # commands". gate_client.py:_handle_message is the canonical
    # implementation of the gate-side state machine — keep both sides
    # in sync if the frame shape changes.
    # ----------------------------------------------------------------

    # Sentinel value `_lora_request` returns when the serial write
    # itself failed (as opposed to "gate didn't reply in time"). The
    # operator-visible message for these is different — write failures
    # are a base-side problem, not a "gate is out of range" — so we
    # surface them distinctly through `lora_command` /
    # `lora_status_request`.
    _LORA_SEND_FAILED = object()

    def lora_command(self, gate_id: str, action: str) -> dict:
        """Run the challenge → command → reply sequence for /open or /close.

        Returns a result dict with at least an `outcome` key. Distinct
        outcomes the channel cares about:
          - "ok"              command accepted; reply contains the gate's
                              alert/status payload showing the new state
          - "noop"            gate was already in the target state
                              ("already_open" / "already_closed")
          - "not_registered"  no Fernet key for this gate_id
          - "no_challenge"    gate didn't reply to challenge_req in time
          - "send_failed"     serial write blew up before the gate could
                              even hear us (base-side problem)
          - "timeout"         command sent, no reply observed

        The post-command grace period is adaptive: we look up the last
        N successful actuations for this (gate, action) bucket and
        compute a threshold via `_compute_actuation_threshold_seconds`.
        A snappy gate gets a short wait; a slow one gets the headroom
        it needs; an actually-broken one trips the threshold faster
        once enough samples accumulate. On success we record the
        observed duration so future commands tune toward reality.
        """
        cipher = self.registry.cipher_for(gate_id)
        if cipher is None:
            return {"outcome": "not_registered"}

        nonce_reply = self._lora_request(
            gate_id,
            {"type": "challenge_req"},
            expected_types={"challenge_resp"},
            cipher=cipher,
        )
        if nonce_reply is self._LORA_SEND_FAILED:
            return {"outcome": "send_failed"}
        if nonce_reply is None:
            return {"outcome": "no_challenge"}
        nonce = nonce_reply.get("nonce")
        if not isinstance(nonce, str):
            return {"outcome": "no_challenge"}

        # Adaptive grace period from this (gate, action) bucket's
        # rolling buffer. `time.monotonic()` for the duration: NTP can
        # step the wall clock mid-cycle, and a `time.time()` delta
        # would record nonsense if that happened.
        durations = self.registry.recent_actuation_durations_ms(
            gate_id, action
        )
        timeout = _compute_actuation_threshold_seconds(durations)
        started_at = time.monotonic()

        # Per-gate relay pulse duration travels in the command frame so
        # the operator can tune it from Telegram without re-flashing the
        # gate. The gate clamps it to its own safe bounds and falls back
        # to its 1s firmware default if the field is missing or invalid.
        relay_ms = self.registry.get_relay_ms(gate_id) or RELAY_PULSE_DEFAULT_MS

        reply = self._lora_request(
            gate_id,
            {
                "type": "command",
                "action": action,
                "nonce": nonce,
                "relay_ms": relay_ms,
            },
            # The gate ack's immediately for a no-op; otherwise the
            # async relay pulse triggers a normal state-change packet
            # (alert on open, status on close).
            expected_types={"ack", "alert", "status"},
            cipher=cipher,
            timeout=timeout,
        )
        if reply is self._LORA_SEND_FAILED:
            return {"outcome": "send_failed"}
        if reply is None:
            return {"outcome": "timeout"}
        if reply.get("type") == "ack":
            # No-op: gate was already in the requested state, no relay
            # pulse, nothing physical to time. Don't pollute the
            # rolling buffer with what is effectively a 0ms cycle.
            return {"outcome": "noop", "reply": reply}
        duration_ms = int((time.monotonic() - started_at) * 1000)
        try:
            self.registry.record_actuation(gate_id, action, duration_ms)
        except Exception as exc:  # noqa: BLE001 — DB error must not fail the user-facing /open
            logger.warning(
                "Could not record actuation for %s/%s: %s",
                gate_id, action, exc,
            )
        return {"outcome": "ok", "reply": reply}

    def lora_status_request(self, gate_id: str) -> dict:
        """Ask the gate for its current state. Same outcome vocabulary
        as `lora_command`, minus the noop case."""
        cipher = self.registry.cipher_for(gate_id)
        if cipher is None:
            return {"outcome": "not_registered"}
        reply = self._lora_request(
            gate_id,
            {"type": "status_req"},
            expected_types={"status"},
            cipher=cipher,
        )
        if reply is self._LORA_SEND_FAILED:
            return {"outcome": "send_failed"}
        if reply is None:
            return {"outcome": "timeout"}
        return {"outcome": "ok", "reply": reply}

    def _lora_request(
        self,
        gate_id: str,
        message: dict,
        *,
        expected_types: set[str],
        cipher: Fernet,
        timeout: float = LORA_DEFAULT_REPLY_TIMEOUT_SECONDS,
    ):
        """Send `message` to `gate_id` and wait for a matching reply.

        Returns the decrypted reply dict on success, `None` on timeout,
        or the `_LORA_SEND_FAILED` sentinel if the serial write itself
        failed. Callers (`lora_command`, `lora_status_request`) map
        each case onto a distinct user-facing outcome.
        """
        slot = _LoRaRequestSlot(expected_types)
        with self._lora_waiters_lock:
            # Refuse to overlap requests against the same gate: the
            # gate's single-slot challenge nonce can only have one
            # outstanding flow, and a parallel attempt would race the
            # nonce out of slot.
            if gate_id in self._lora_waiters:
                logger.warning(
                    "LoRa request to %s refused: another is already in flight",
                    gate_id,
                )
                return None
            self._lora_waiters[gate_id] = slot
        try:
            if not self._lora_send(gate_id, message, cipher):
                return self._LORA_SEND_FAILED
            if not slot.event.wait(timeout):
                return None
            return slot.reply
        finally:
            with self._lora_waiters_lock:
                self._lora_waiters.pop(gate_id, None)

    def _lora_send(self, gate_id: str, message: dict, cipher: Fernet) -> bool:
        """Frame-encrypt-write a single message to the LoRa port.

        Holds `_lora_tx_lock` for the duration of the write so it can't
        interleave with another sender (today there's only one, but the
        invariant is cheap to keep).
        """
        if self.lora is None or not self.lora.is_open:
            return False
        try:
            payload = cipher.encrypt(json.dumps(message).encode("utf-8"))
            framed = gate_id.encode("utf-8") + b":" + payload + b"\n"
            with self._lora_tx_lock:
                self.lora.write(framed)
                self.lora.flush()
            return True
        except (serial.SerialException, OSError) as exc:
            logger.warning("LoRa send to %s failed: %s", gate_id, exc)
            return False

    def _route_to_waiter(self, gate_id: str, message: dict) -> None:
        """Hand a decrypted reply to a pending lora_command/status caller.

        Called from `_dispatch` after the existing alert/status side
        effects. If no one is waiting, this is a no-op — unsolicited
        gate traffic continues to flow through the normal alert path.
        """
        msg_type = message.get("type")
        with self._lora_waiters_lock:
            slot = self._lora_waiters.get(gate_id)
        if slot is None or msg_type not in slot.expected_types:
            return
        slot.reply = message
        slot.event.set()

    def _begin_factory_reset(self, chat_id: int, ssid: str) -> None:
        """Spawn the disrupt-after-ack thread for /factory_reset.

        The command channel has already sent the operator-facing ack
        synchronously; this just starts the wipe + AP-bringup in the
        background. Daemon thread so a hung subprocess can't keep
        the process alive past `os._exit(0)`.
        """
        logger.warning(
            "Factory reset requested via Telegram (chat_id=%s, ssid=%r)",
            chat_id, ssid,
        )
        threading.Thread(
            target=_perform_factory_reset,
            args=(ssid,),
            name="factory-reset",
            daemon=True,
        ).start()

    def _start_command_channel(self) -> None:
        """Spawn the long-poll command channel as a daemon thread.

        Daemon so a hung Telegram long-poll can't keep the process
        running on shutdown — the main thread's serial-read loop is the
        only thing that should hold the service open. Skipped silently
        when Telegram isn't configured.
        """
        if not self.notifier.configured:
            logger.info(
                "Telegram not configured; command channel not started"
            )
            return
        channel = TelegramCommandChannel(
            bot_token=os.getenv("TELEGRAM_TOKEN", ""),
            configured_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            registry=self.registry,
            lora_command=self.lora_command,
            lora_status_request=self.lora_status_request,
            factory_reset_callback=self._begin_factory_reset,
        )
        thread = threading.Thread(
            target=channel.run_forever,
            name="telegram-command-channel",
            daemon=True,
        )
        thread.start()

    def _listen_forever(self) -> None:
        while True:
            if self.lora.in_waiting > 0:
                self._handle_one_packet()
            time.sleep(LOOP_TICK_SECONDS)

    def _handle_one_packet(self) -> None:
        try:
            raw = self.lora.readline().strip()
        except serial.SerialException as exc:
            logger.warning("Serial read failure: %s", exc)
            return
        if not raw or b":" not in raw:
            return

        try:
            gate_id_bytes, encrypted = raw.split(b":", 1)
            gate_id = gate_id_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return

        cipher = self.registry.cipher_for(gate_id)
        if cipher is None:
            logger.debug("Dropped packet from unregistered gate %s", gate_id)
            return

        try:
            # No ttl= argument: the gate has no RTC and no NTP path
            # (LoRa-only, by design), so its wall clock is whatever the
            # kernel build epoch happens to be — typically months behind
            # the base. A wall-clock TTL check therefore rejects every
            # packet from a freshly-booted gate, regardless of whether
            # the key matches. Replay protection is the per-gate seq
            # counter in self.registry.accept_seq() below, which is the
            # actual defense; the TTL was always a belt-and-braces
            # complement that has now been retired as actively harmful.
            decrypted = cipher.decrypt(encrypted)
        except InvalidToken:
            logger.warning(
                "Dropped packet from %s: invalid token (key mismatch?)", gate_id
            )
            return

        try:
            message = json.loads(decrypted.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Dropped packet from %s: malformed JSON", gate_id)
            return

        self._dispatch(gate_id, message)

    def _dispatch(self, gate_id: str, message: dict) -> None:
        seq = message.get("seq")
        if not isinstance(seq, int) or not self.registry.accept_seq(gate_id, seq):
            logger.warning("Replay or out-of-order packet from %s (seq=%r)", gate_id, seq)
            return

        msg_type = message.get("type")
        if msg_type in ("alert", "status"):
            state = message.get("state", "unknown")
            summary = f"{msg_type}:{state}"
            # Read the prior state BEFORE log_event writes the new one,
            # otherwise the comparison always finds the same state.
            prior_state = self.registry.last_recorded_state(gate_id)
            logger.info("Gate %s -> %s", gate_id, summary)
            self.registry.log_event(gate_id, EVENT_GATE_STATE, summary)

            # Notify rules:
            #   - `alert` always notifies (gate decided this is page-worthy,
            #     e.g. _on_gate_opened). Preserved for backwards compat
            #     with the existing protocol and as a "force-notify"
            #     escape hatch the gate can use in future.
            #   - `status` notifies only on a real state transition
            #     (prior_state != current state). That makes the routine
            #     close-after-open case visible in Telegram without
            #     spamming the operator on every status_req reply from
            #     `/status GATE-X`, which sends a `status` for the
            #     current state regardless of whether anything changed.
            should_notify = msg_type == "alert" or (
                msg_type == "status" and prior_state != state
            )
            if should_notify:
                name = self.registry.display_name(gate_id)
                label = f"{name} ({gate_id})" if name else gate_id
                # Lead with a state-emoji so the message is scannable at
                # a glance in the operator's notification tray.
                emoji = "🔓" if state == "open" else "🔒"
                self.notifier.send(f"{emoji} {label}: {state.upper()}")
        else:
            logger.debug("Ignoring message type %s from %s", msg_type, gate_id)
        # Hand the same message off to any /open, /close, /status caller
        # waiting on this gate. Runs after the alert path so a
        # /open-triggered state change still produces the normal Telegram
        # alert; the command channel adds its own follow-up reply.
        self._route_to_waiter(gate_id, message)


def main() -> None:
    # CONFIG_PATH (TELEGRAM_TOKEN, TELEGRAM_CHAT_ID) is loaded into our
    # environment by systemd via EnvironmentFile=/var/lib/base_station/...
    # in base-station.service — no need to parse it ourselves.
    registry = GateRegistry(DB_PATH)
    notifier = TelegramNotifier(
        bot_token=os.getenv("TELEGRAM_TOKEN", ""),
        chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
    )
    if not notifier.configured:
        logger.warning("Telegram credentials missing; alerts will be logged only")

    # Default to /dev/ttyAMA0 (the PL011 UART that `dtoverlay=disable-bt`
    # routes to the GPIO header where the LoRa radio is wired). /dev/serial0
    # is a Raspberry Pi OS convenience symlink that Buildroot doesn't ship,
    # so we name the real device directly. Override via LORA_PORT= if your
    # wiring is different.
    station = BaseStation(
        serial_port=os.getenv("LORA_PORT", "/dev/ttyAMA0"),
        baud_rate=int(os.getenv("LORA_BAUD", "9600")),
        registry=registry,
        notifier=notifier,
    )
    try:
        station.run()
    finally:
        registry.close()


if __name__ == "__main__":
    main()
