"""Tests for `BaseStation.lora_command`, `lora_status_request`, and
the `_route_to_waiter` plumbing — the layer that turns a Telegram
command into a challenge_req → command(nonce) → ack/alert wire
sequence over the LoRa serial port.

The real serial port is mocked with a `_RecordingSerial` that lets a
test inject "decrypted" replies into `_dispatch` directly, sidestepping
the listen loop. That lets us drive scenarios end-to-end (challenge,
timeout, send-failure) without a real radio.

What this file does NOT cover:
  - The handler-side mapping of outcomes to user-facing strings —
    that's in test_lora_commands.py against a mocked transport.
  - Real Fernet decryption of replies — the request/reply correlation
    happens *after* `_dispatch` decrypts, so the transport layer only
    sees post-decrypt dicts.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest

import serial  # the stub from _helpers

from tests import _helpers


class _RecordingSerial:
    """Stand-in for `serial.Serial` that records what was written so a
    test can correlate the outbound challenge_req with its scripted
    reply.

    `fail_writes`: when True, .write() raises serial.SerialException so
    the test can exercise the send_failed path.
    """

    def __init__(self) -> None:
        self.is_open = True
        self.in_waiting = 0
        self.writes: list[bytes] = []
        self.fail_writes = False

    def write(self, data: bytes) -> int:
        if self.fail_writes:
            raise serial.SerialException("simulated tx failure")
        self.writes.append(data)
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.is_open = False

    def readline(self) -> bytes:
        return b""


class _TransportCase(unittest.TestCase):
    """Sets up a BaseStation with the mocked serial port and a single
    registered gate so transport tests can drive happy/sad paths."""

    def setUp(self) -> None:
        self.bs = _helpers.import_base_station()
        self.db_path = tempfile.mktemp(suffix=".db")
        self.registry = self.bs.GateRegistry(self.db_path)
        self.key = _helpers.fresh_fernet_key()
        self.registry.register_gate("GATE-RADIO1", self.key, "Radio")

        self.station = self.bs.BaseStation(
            serial_port="/dev/fake",
            baud_rate=9600,
            registry=self.registry,
            notifier=self.bs.TelegramNotifier(bot_token="", chat_id=""),
        )
        self.station.lora = _RecordingSerial()

    def tearDown(self) -> None:
        self.registry.close()
        try:
            os.unlink(self.db_path)
        except FileNotFoundError:
            pass

    def _inject_reply_async(self, gate_id: str, message: dict, after: float = 0.05) -> None:
        """Schedule `_dispatch` to be called with `message` from a
        background thread, simulating the listen loop hearing a reply.

        Each reply needs a fresh, monotonically-advancing seq so it
        clears the per-gate replay check.
        """
        def _go():
            import time
            time.sleep(after)
            seqed = {**message, "seq": self._next_seq(gate_id)}
            self.station._dispatch(gate_id, seqed)
        threading.Thread(target=_go, daemon=True).start()

    _seq_counter: dict = {}

    def _next_seq(self, gate_id: str) -> int:
        # Start above whatever the registry holds; bump on every call.
        from_db = self.registry.get_gate(gate_id)["last_seq"]
        cur = max(self._seq_counter.get(gate_id, 0), from_db) + 1
        self._seq_counter[gate_id] = cur
        return cur


class LoraCommandTests(_TransportCase):
    def test_not_registered(self):
        result = self.station.lora_command("GATE-NOPEXX", "open")
        self.assertEqual(result["outcome"], "not_registered")
        self.assertEqual(self.station.lora.writes, [],
                         "no LoRa traffic for unregistered gate")

    def test_send_failed_on_challenge(self):
        """When the serial write itself raises, the public outcome is
        send_failed — NOT no_challenge. Operator should know it's a
        base-side problem, not a gate-out-of-range problem."""
        self.station.lora.fail_writes = True
        result = self.station.lora_command("GATE-RADIO1", "open")
        self.assertEqual(result["outcome"], "send_failed")

    def test_no_challenge_on_first_timeout(self):
        """Serial write succeeded but gate never answered challenge_req.

        Shorten the default timeout by patching the method's
        `__kwdefaults__` — the default `timeout=LORA_DEFAULT_REPLY_...`
        was captured at function-def time, so mutating the module
        constant has no effect. This keeps the test under 1s instead
        of the 5s the production default would impose.
        """
        original = self.station._lora_request.__func__.__kwdefaults__.copy()
        self.station._lora_request.__func__.__kwdefaults__["timeout"] = 0.3
        try:
            result = self.station.lora_command("GATE-RADIO1", "open")
        finally:
            self.station._lora_request.__func__.__kwdefaults__.clear()
            self.station._lora_request.__func__.__kwdefaults__.update(original)
        self.assertEqual(result["outcome"], "no_challenge")

    def test_happy_path_open(self):
        """Inject a challenge_resp then a state-change alert; the
        transport should return outcome=ok with the alert payload."""
        self._inject_reply_async(
            "GATE-RADIO1",
            {"type": "challenge_resp", "nonce": "a" * 32},
            after=0.02,
        )
        # Also schedule the post-command alert. The challenge_resp
        # closes the first _lora_request; the alert closes the second.
        self._inject_reply_async(
            "GATE-RADIO1",
            {"type": "alert", "state": "open"},
            after=0.10,
        )
        result = self.station.lora_command("GATE-RADIO1", "open")
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(result["reply"]["type"], "alert")
        # Two outbound frames: challenge_req, command.
        self.assertEqual(len(self.station.lora.writes), 2)

    def test_noop_on_already_state(self):
        """Gate returns ack(already_open) instead of an alert — transport
        reports outcome=noop."""
        self._inject_reply_async(
            "GATE-RADIO1",
            {"type": "challenge_resp", "nonce": "b" * 32},
            after=0.02,
        )
        self._inject_reply_async(
            "GATE-RADIO1",
            {"type": "ack", "result": "already_open"},
            after=0.10,
        )
        result = self.station.lora_command("GATE-RADIO1", "open")
        self.assertEqual(result["outcome"], "noop")

    def test_command_timeout_after_challenge(self):
        """Challenge ok, but no follow-up reply within the command
        window — outcome is timeout (NOT no_challenge)."""
        self._inject_reply_async(
            "GATE-RADIO1",
            {"type": "challenge_resp", "nonce": "c" * 32},
            after=0.02,
        )
        # Shrink the timeout so the test doesn't take 8s.
        self.bs.LORA_COMMAND_REPLY_TIMEOUT_SECONDS = 0.3
        try:
            result = self.station.lora_command("GATE-RADIO1", "open")
        finally:
            # Restore for other tests that may import bs again — though
            # each test loads its own module so this is belt-and-braces.
            self.bs.LORA_COMMAND_REPLY_TIMEOUT_SECONDS = 8
        self.assertEqual(result["outcome"], "timeout")


class LoraStatusRequestTests(_TransportCase):
    def test_happy_path_status(self):
        self._inject_reply_async(
            "GATE-RADIO1",
            {"type": "status", "state": "closed"},
            after=0.02,
        )
        result = self.station.lora_status_request("GATE-RADIO1")
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(result["reply"]["state"], "closed")

    def test_status_timeout(self):
        self.bs.LORA_DEFAULT_REPLY_TIMEOUT_SECONDS = 0.2
        try:
            result = self.station.lora_status_request("GATE-RADIO1")
        finally:
            self.bs.LORA_DEFAULT_REPLY_TIMEOUT_SECONDS = 5
        self.assertEqual(result["outcome"], "timeout")

    def test_status_send_failed(self):
        self.station.lora.fail_writes = True
        result = self.station.lora_status_request("GATE-RADIO1")
        self.assertEqual(result["outcome"], "send_failed")

    def test_status_not_registered(self):
        result = self.station.lora_status_request("GATE-NOPEXX")
        self.assertEqual(result["outcome"], "not_registered")


class WaiterRoutingTests(_TransportCase):
    """Direct unit tests for `_route_to_waiter` — the function that
    `_dispatch` calls to hand a decrypted reply to a pending caller."""

    def test_routes_when_slot_matches(self):
        slot = self.bs._LoRaRequestSlot(expected_types={"status"})
        with self.station._lora_waiters_lock:
            self.station._lora_waiters["GATE-RADIO1"] = slot
        self.station._route_to_waiter(
            "GATE-RADIO1", {"type": "status", "state": "open"}
        )
        self.assertTrue(slot.event.is_set())
        self.assertEqual(slot.reply["state"], "open")

    def test_ignores_when_type_doesnt_match(self):
        slot = self.bs._LoRaRequestSlot(expected_types={"challenge_resp"})
        with self.station._lora_waiters_lock:
            self.station._lora_waiters["GATE-RADIO1"] = slot
        self.station._route_to_waiter(
            "GATE-RADIO1", {"type": "alert", "state": "open"}
        )
        self.assertFalse(slot.event.is_set())
        self.assertIsNone(slot.reply)

    def test_noop_when_no_slot(self):
        """Unsolicited gate traffic — no waiter exists — must not
        raise. Just returns silently."""
        # Should be a no-op; assertion is "doesn't raise".
        self.station._route_to_waiter(
            "GATE-RADIO1", {"type": "alert", "state": "open"}
        )

    def test_parallel_request_to_same_gate_refused(self):
        """The gate has a single challenge nonce slot; we must serialize
        on the base side. A second concurrent _lora_request must
        return None without sending anything."""
        slot = self.bs._LoRaRequestSlot(expected_types={"status"})
        with self.station._lora_waiters_lock:
            self.station._lora_waiters["GATE-RADIO1"] = slot
        try:
            cipher = self.registry.cipher_for("GATE-RADIO1")
            result = self.station._lora_request(
                "GATE-RADIO1",
                {"type": "status_req"},
                expected_types={"status"},
                cipher=cipher,
                timeout=0.1,
            )
            self.assertIsNone(result)
            # No outbound write happened — the request was refused
            # before send.
            self.assertEqual(self.station.lora.writes, [])
        finally:
            with self.station._lora_waiters_lock:
                self.station._lora_waiters.pop("GATE-RADIO1", None)


class DispatchNotifyTests(_TransportCase):
    """Notify-rules covered: `alert` always pings Telegram; `status`
    pings only on a real state transition; same-state status (e.g.
    a `/status GATE-X` reply for an already-known state) is silent.
    """

    def setUp(self) -> None:
        super().setUp()
        # Replace the no-op TelegramNotifier with a recording one so we
        # can assert on what *would* have been sent.
        self.notifier = _helpers.CapturingNotifier()
        self.station.notifier = self.notifier

    def _dispatch(self, msg_type: str, state: str) -> None:
        seq = self._next_seq("GATE-RADIO1")
        self.station._dispatch(
            "GATE-RADIO1", {"type": msg_type, "state": state, "seq": seq}
        )

    def test_alert_always_notifies(self):
        self._dispatch("alert", "open")
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("🔓", self.notifier.last_sent)
        self.assertIn("OPEN", self.notifier.last_sent)
        self.assertIn("Radio (GATE-RADIO1)", self.notifier.last_sent)

    def test_status_with_state_change_notifies(self):
        # Seed prior state via a direct event-log write so dispatch
        # sees a transition.
        self.registry.log_event(
            "GATE-RADIO1", self.bs.EVENT_GATE_STATE, "alert:open"
        )
        self._dispatch("status", "closed")
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("🔒", self.notifier.last_sent)
        self.assertIn("CLOSED", self.notifier.last_sent)

    def test_status_with_same_state_is_silent(self):
        """The dedup that prevents `/status GATE-X` replies from
        double-paging — a status that doesn't change the recorded
        state must NOT fire a notification."""
        self.registry.log_event(
            "GATE-RADIO1", self.bs.EVENT_GATE_STATE, "alert:open"
        )
        self._dispatch("status", "open")
        self.assertEqual(self.notifier.sent, [],
                         "same-state status should not page")

    def test_alert_uses_open_emoji(self):
        self._dispatch("alert", "open")
        self.assertTrue(self.notifier.last_sent.startswith("🔓"))

    def test_alert_uses_closed_emoji(self):
        self._dispatch("alert", "closed")
        self.assertTrue(self.notifier.last_sent.startswith("🔒"))

    def test_first_ever_status_notifies_because_no_prior_state(self):
        """No prior event-log entry → `prior_state is None` → any
        incoming state is a transition → notify. Catches an off-by-one
        where the very first status from a freshly-paired gate gets
        silently dropped because the comparison returns False on None."""
        self._dispatch("status", "open")
        self.assertEqual(len(self.notifier.sent), 1)

    def test_prior_state_read_before_log_event(self):
        """Regression guard for the read-after-write bug — if
        log_event ran before last_recorded_state, the comparison
        would always look at the just-written row and never see a
        transition. Verified by dispatching the same state twice:
        first call must notify (no prior), second must NOT (now
        same state)."""
        self._dispatch("status", "open")
        self._dispatch("status", "open")
        self.assertEqual(len(self.notifier.sent), 1,
                         "second same-state status must NOT have re-paged")


if __name__ == "__main__":
    unittest.main()
