"""Tests for the hardware-free logic in `gate_client.py`.

Covers:
  - `_relay_seconds_from` — the clamp that stops a malformed or
    malicious `relay_ms` in a command frame from latching the relay
    closed indefinitely (bool rejection, MIN/MAX bounds, fallback).
  - `_load_seq` / `_save_seq` — the persisted outbound counter that
    keeps the base's replay protection intact across gate reboots.
  - The single-use challenge nonce in `_handle_command` — cleared on
    every attempt (success or failure) and expired by lifetime.

The GPIO- and serial-driven paths (setup(), run(), _process_incoming)
stay uncovered on purpose: they're thin wrappers over hardware that
the stubbed gpiozero/serial modules can't meaningfully exercise.
"""

from __future__ import annotations

import os
import tempfile
import types
import unittest

from tests import _helpers


class RelaySecondsFromTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gc = _helpers.import_gate_client()
        self.resolve = self.gc.RanchGateMonitor._relay_seconds_from

    def test_absent_field_falls_back_to_default(self):
        self.assertEqual(self.resolve({}), self.gc.RELAY_PULSE_SECONDS)

    def test_normal_value_converts_ms_to_seconds(self):
        self.assertEqual(self.resolve({"relay_ms": 1500}), 1.5)

    def test_bool_is_rejected_despite_being_int_subclass(self):
        # JSON `true` must not read as 1ms.
        self.assertEqual(
            self.resolve({"relay_ms": True}), self.gc.RELAY_PULSE_SECONDS
        )

    def test_non_numeric_falls_back_to_default(self):
        for bad in ("1500", None, [1500], {}):
            self.assertEqual(
                self.resolve({"relay_ms": bad}), self.gc.RELAY_PULSE_SECONDS
            )

    def test_clamped_to_min(self):
        self.assertEqual(
            self.resolve({"relay_ms": 1}), self.gc.RELAY_PULSE_MIN_SECONDS
        )

    def test_clamped_to_max_so_relay_cannot_latch(self):
        self.assertEqual(
            self.resolve({"relay_ms": 10_000_000}),
            self.gc.RELAY_PULSE_MAX_SECONDS,
        )


class SeqPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gc = _helpers.import_gate_client()
        self.tmpdir = tempfile.mkdtemp()
        self.gc.STATE_DIR = self.tmpdir
        self.gc.SEQ_PATH = os.path.join(self.tmpdir, "last_seq")

    def test_missing_file_starts_at_zero(self):
        self.assertEqual(self.gc._load_seq(), 0)

    def test_roundtrip(self):
        self.gc._save_seq(42)
        self.assertEqual(self.gc._load_seq(), 42)

    def test_garbage_file_starts_at_zero(self):
        with open(self.gc.SEQ_PATH, "w", encoding="utf-8") as fh:
            fh.write("not a number")
        self.assertEqual(self.gc._load_seq(), 0)

    def test_save_replaces_atomically_leaving_no_tmp(self):
        self.gc._save_seq(1)
        self.gc._save_seq(2)
        self.assertEqual(self.gc._load_seq(), 2)
        self.assertFalse(os.path.exists(self.gc.SEQ_PATH + ".tmp"))


class ChallengeNonceTests(unittest.TestCase):
    """The nonce is the whole base→gate command authentication story;
    single-use and lifetime bounds must hold."""

    def setUp(self) -> None:
        self.gc = _helpers.import_gate_client()
        self.tmpdir = tempfile.mkdtemp()
        self.gc.STATE_DIR = self.tmpdir
        self.gc.SEQ_PATH = os.path.join(self.tmpdir, "last_seq")
        self.monitor = self.gc.RanchGateMonitor(
            gate_id="GATE-TEST01",
            serial_port="/dev/null",
            baud_rate=9600,
            sensor_pin=16,
            relay_pin=17,
            lora_key=_helpers.fresh_fernet_key().encode("utf-8"),
        )
        # Closed gate; no real GPIO.
        self.monitor.gate_sensor = types.SimpleNamespace(is_pressed=True)
        self.triggered: list[float] = []
        self.monitor._trigger_relay_async = self.triggered.append
        self.sent: list[dict] = []
        self.monitor._send = self.sent.append

    def _command(self, nonce: str, action: str = "open") -> dict:
        return {"type": "command", "action": action, "nonce": nonce}

    def _arm_nonce(self, nonce: str = "a" * 32) -> str:
        self.monitor._challenge_nonce = nonce
        self.monitor._challenge_issued_at = self.gc.time.monotonic()
        return nonce

    def test_valid_nonce_triggers_relay_once(self):
        nonce = self._arm_nonce()
        self.monitor._handle_command(self._command(nonce))
        self.assertEqual(len(self.triggered), 1)

    def test_nonce_is_single_use(self):
        nonce = self._arm_nonce()
        self.monitor._handle_command(self._command(nonce))
        self.monitor._handle_command(self._command(nonce))
        self.assertEqual(len(self.triggered), 1)

    def test_wrong_nonce_rejected_and_burns_the_real_one(self):
        nonce = self._arm_nonce()
        self.monitor._handle_command(self._command("b" * 32))
        # The real nonce was cleared by the failed attempt too.
        self.monitor._handle_command(self._command(nonce))
        self.assertEqual(self.triggered, [])

    def test_expired_nonce_rejected(self):
        nonce = self._arm_nonce()
        self.monitor._challenge_issued_at -= (
            self.gc.NONCE_LIFETIME_SECONDS + 1
        )
        self.monitor._handle_command(self._command(nonce))
        self.assertEqual(self.triggered, [])

    def test_noop_action_acks_instead_of_triggering(self):
        # Gate is closed; /close is a no-op and must not pulse the relay.
        nonce = self._arm_nonce()
        self.monitor._handle_command(self._command(nonce, action="close"))
        self.assertEqual(self.triggered, [])
        self.assertEqual(
            self.sent, [{"type": "ack", "result": "already_closed"}]
        )

    def test_relay_ms_from_frame_reaches_the_pulse(self):
        nonce = self._arm_nonce()
        self.monitor._handle_command(
            {**self._command(nonce), "relay_ms": 2500}
        )
        self.assertEqual(self.triggered, [2.5])


if __name__ == "__main__":
    unittest.main()
