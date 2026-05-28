"""LoRa-attached gate monitor.

Reports gate state changes to the base station and accepts authenticated
remote-actuation commands. All radio traffic is encrypted and authenticated
with a per-device Fernet key. Outbound packets carry a monotonic sequence
number which the base persists per-gate, so replays are rejected even if
the gate has no wall-clock time (Pi Zero has no RTC and the gate has no
NTP source in the field). Inbound commands are bound to a single-use
challenge nonce whose lifetime is measured against time.monotonic(), so
command-flow security does not depend on wall-clock time either.
"""

import json
import logging
import os
import secrets
import threading
import time

import serial
from cryptography.fernet import Fernet, InvalidToken
from gpiozero import Button, OutputDevice

# Note: no `from dotenv import load_dotenv` — gate-client.service uses
# systemd's EnvironmentFile=/var/lib/gate-client/gate_config.env to load
# GATE_ID, LORA_SECRET_KEY, etc. before this process starts. The env file
# is placed in /var/lib/gate-client (ext4, mode 0600) by the oneshot unit
# gate-config-migrate.service, which moves it off the FAT32 /boot copy
# the factory flash writes — see ranch-gate-config-migrate.sh for the
# rationale and crash-safety story.

STATE_DIR = "/var/lib/gate-client"
CONFIG_PATH = f"{STATE_DIR}/gate_config.env"
SEQ_PATH = f"{STATE_DIR}/last_seq"

NONCE_LIFETIME_SECONDS = 15
CHALLENGE_MIN_INTERVAL_SECONDS = 2
RELAY_PULSE_SECONDS = 1.0
SERIAL_READ_TIMEOUT_SECONDS = 1.0
LOOP_TICK_SECONDS = 0.1

STATE_OPEN = "open"
STATE_CLOSED = "closed"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def _load_seq() -> int:
    """Read the last-used outbound sequence number from disk.

    Persisted so a gate reboot doesn't restart the counter at zero, which
    would cause the base station to reject every packet up to whatever seq
    the gate had reached before the reboot.
    """
    try:
        with open(SEQ_PATH, encoding="utf-8") as handle:
            return int(handle.read().strip())
    except (FileNotFoundError, ValueError):
        return 0


def _save_seq(seq: int) -> None:
    """Atomically persist the most recent outbound sequence number."""
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = f"{SEQ_PATH}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(str(seq))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, SEQ_PATH)


class RanchGateMonitor:
    def __init__(
        self,
        *,
        gate_id: str,
        serial_port: str,
        baud_rate: int,
        sensor_pin: int,
        relay_pin: int,
        lora_key: bytes,
    ) -> None:
        self.gate_id = gate_id.upper()
        self.serial_port = serial_port
        self.baud_rate = baud_rate
        self.sensor_pin = sensor_pin
        self.relay_pin = relay_pin
        self.cipher = Fernet(lora_key)

        self.lora: serial.Serial | None = None
        self.gate_sensor: Button | None = None
        self.gate_relay: OutputDevice | None = None

        self._tx_lock = threading.Lock()
        self._seq = _load_seq()

        self._challenge_nonce: str | None = None
        self._challenge_issued_at: float = 0.0
        self._last_challenge_served_at: float = 0.0

    def setup(self) -> None:
        logger.info("Initializing gate %s", self.gate_id)
        self.lora = serial.Serial(
            self.serial_port, self.baud_rate, timeout=SERIAL_READ_TIMEOUT_SECONDS
        )
        self.gate_sensor = Button(self.sensor_pin, pull_up=True, bounce_time=0.5)
        self.gate_sensor.when_released = self._on_gate_opened
        self.gate_sensor.when_pressed = self._on_gate_closed
        self.gate_relay = OutputDevice(
            self.relay_pin, active_high=True, initial_value=False
        )

    def run(self) -> None:
        logger.info("Gate monitor active")
        try:
            while True:
                self._process_incoming()
                time.sleep(LOOP_TICK_SECONDS)
        except KeyboardInterrupt:
            logger.info("Manual shutdown requested")
        finally:
            if self.lora is not None:
                self.lora.close()

    def _send(self, message: dict) -> None:
        if self.lora is None or not self.lora.is_open:
            return
        with self._tx_lock:
            # Persist before send so a power loss during transmission can
            # never cause the next boot to reuse this seq — only ever skip it.
            self._seq += 1
            _save_seq(self._seq)
            message = {**message, "seq": self._seq}
            payload = self.cipher.encrypt(json.dumps(message).encode("utf-8"))
            framed = self.gate_id.encode("utf-8") + b":" + payload + b"\n"
            self.lora.write(framed)

    def _send_state(self, state: str, *, is_alert: bool) -> None:
        self._send({"type": "alert" if is_alert else "status", "state": state})

    def _on_gate_opened(self) -> None:
        self._send_state(STATE_OPEN, is_alert=True)

    def _on_gate_closed(self) -> None:
        self._send_state(STATE_CLOSED, is_alert=False)

    def _current_state(self) -> str:
        return STATE_CLOSED if self.gate_sensor.is_pressed else STATE_OPEN

    def _pulse_relay(self) -> None:
        logger.info("Activating relay")
        self.gate_relay.on()
        time.sleep(RELAY_PULSE_SECONDS)
        self.gate_relay.off()
        logger.info("Relay released")

    def _trigger_relay_async(self) -> None:
        threading.Thread(target=self._pulse_relay, daemon=True).start()

    def _process_incoming(self) -> None:
        if self.lora.in_waiting <= 0:
            return
        try:
            raw = self.lora.readline().strip()
        except serial.SerialException as exc:
            logger.warning("Serial read failure: %s", exc)
            return
        if not raw or b":" not in raw:
            return

        target_bytes, encrypted = raw.split(b":", 1)
        try:
            if target_bytes.decode("utf-8") != self.gate_id:
                return
        except UnicodeDecodeError:
            return

        try:
            # No ttl= here: the gate has no NTP source in the field, so a
            # wall-clock-based check would either always pass or always fail.
            # Replay protection comes from the single-use challenge nonce
            # (lifetime measured against time.monotonic()) and the
            # CHALLENGE_REQ rate limit. The base used to enforce a 30-second
            # Fernet ttl on its receive side as belt-and-braces, but that
            # broke when this gate (no RTC) sat 16 months behind the base's
            # wall clock — the base rejected every packet as "expired" even
            # with a matching key. Per-gate seq counters in the base's
            # GateRegistry are the actual replay defense on both sides.
            decrypted = self.cipher.decrypt(encrypted)
        except InvalidToken:
            logger.debug("Dropped packet: invalid token or expired TTL")
            return

        try:
            message = json.loads(decrypted.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.debug("Dropped packet: malformed JSON")
            return

        self._handle_message(message)

    def _handle_message(self, message: dict) -> None:
        msg_type = message.get("type")

        if msg_type == "challenge_req":
            self._handle_challenge_request()
        elif msg_type == "status_req":
            self._send_state(self._current_state(), is_alert=False)
        elif msg_type == "command":
            self._handle_command(message)
        else:
            logger.debug("Ignoring unknown message type: %s", msg_type)

    def _handle_challenge_request(self) -> None:
        now = time.monotonic()
        if now - self._last_challenge_served_at < CHALLENGE_MIN_INTERVAL_SECONDS:
            logger.debug("Rate-limiting challenge request")
            return
        self._last_challenge_served_at = now
        self._challenge_nonce = secrets.token_hex(16)
        self._challenge_issued_at = now
        self._send({"type": "challenge_resp", "nonce": self._challenge_nonce})

    def _handle_command(self, message: dict) -> None:
        action = message.get("action")
        received_nonce = message.get("nonce", "")
        now = time.monotonic()

        nonce_valid = (
            self._challenge_nonce is not None
            and secrets.compare_digest(received_nonce, self._challenge_nonce)
            and now - self._challenge_issued_at <= NONCE_LIFETIME_SECONDS
        )
        # Single-use nonce: clear it regardless of outcome so a captured
        # challenge_resp can never be replayed against a future command.
        self._challenge_nonce = None
        if not nonce_valid:
            logger.warning("Rejected command with bad/expired nonce")
            return

        current = self._current_state()
        if action == "open" and current == STATE_CLOSED:
            self._trigger_relay_async()
        elif action == "close" and current == STATE_OPEN:
            self._trigger_relay_async()
        else:
            self._send({"type": "ack", "result": f"already_{current}"})


def main() -> None:
    # CONFIG_PATH (GATE_ID, LORA_SECRET_KEY) is loaded into our environment
    # by systemd via EnvironmentFile= in gate-client.service. On first
    # boot, gate-config-migrate.service moves the file off FAT32 /boot
    # (where mode bits don't protect against a stolen SD card) onto
    # ext4 /var/lib/gate-client. The migration is idempotent.
    gate_id = os.getenv("GATE_ID")
    lora_key = os.getenv("LORA_SECRET_KEY")
    if not gate_id or not lora_key:
        logger.critical("Missing GATE_ID or LORA_SECRET_KEY in %s", CONFIG_PATH)
        raise SystemExit(1)

    # Default to /dev/ttyAMA0 — see base_station.py for the explanation.
    # Override via LORA_PORT= in /boot/gate_config.env if needed.
    monitor = RanchGateMonitor(
        gate_id=gate_id,
        serial_port=os.getenv("LORA_PORT", "/dev/ttyAMA0"),
        baud_rate=int(os.getenv("LORA_BAUD", "9600")),
        sensor_pin=int(os.getenv("SENSOR_GPIO", "16")),
        relay_pin=int(os.getenv("RELAY_GPIO", "17")),
        lora_key=lora_key.encode("utf-8"),
    )
    monitor.setup()
    monitor.run()


if __name__ == "__main__":
    main()
