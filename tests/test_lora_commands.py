"""Tests for the LoRa-driving Telegram commands: `/open`, `/close`,
`/status GATE-XXXX`.

These commands route through `TelegramCommandChannel._cmd_drive_gate`
or `_cmd_status_one`, which then call into the
`lora_command` / `lora_status_request` callables that
`BaseStation` provides. We mock those callables so the tests don't
need a real serial port — the LoRa transport itself is covered
separately in test_lora_transport.py.

What this file asserts:
  - The right outcome → operator-visible string mapping.
  - Pre-flight validation (gate-id format, registration, callback
    wired up).
  - The send_failed vs no_challenge vs timeout distinction.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from tests import _helpers


class _LoraCommandsCase(unittest.TestCase):
    def setUp(self) -> None:
        self.bs = _helpers.import_base_station()
        self.db_path = tempfile.mktemp(suffix=".db")
        self.registry = self.bs.GateRegistry(self.db_path)

        self.key = _helpers.fresh_fernet_key()
        self.registry.register_gate("GATE-PASTUR", self.key, "Pasture")
        self.registry.register_gate("GATE-DRIVE1", self.key, None)

        # Scripted LoRa outcomes — each test sets these.
        self.next_command = {"outcome": "timeout"}
        self.next_status = {"outcome": "timeout"}
        self.command_calls: list[tuple] = []
        self.status_calls: list[str] = []

        def fake_command(gate_id, action):
            self.command_calls.append((gate_id, action))
            return self.next_command

        def fake_status(gate_id):
            self.status_calls.append(gate_id)
            return self.next_status

        self.channel = self.bs.TelegramCommandChannel(
            bot_token=_helpers.DEFAULT_BOT_TOKEN,
            configured_chat_id=str(_helpers.DEFAULT_CHAT_ID),
            registry=self.registry,
            lora_command=fake_command,
            lora_status_request=fake_status,
        )
        self.cap = _helpers.CapturingChannel(self.channel)

    def tearDown(self) -> None:
        self.registry.close()
        try:
            os.unlink(self.db_path)
        except FileNotFoundError:
            pass


class OpenCloseTests(_LoraCommandsCase):
    def test_open_rejects_malformed_id(self):
        self.channel._process_update(_helpers.make_message("/open bogus"))
        self.assertIn("isn't a valid gate ID", self.cap.last_reply)
        self.assertEqual(self.command_calls, [],
                         "no LoRa traffic for invalid gate id")

    def test_open_unregistered_gate(self):
        self.channel._process_update(_helpers.make_message("/open GATE-NOPEXX"))
        self.assertIn("not registered", self.cap.last_reply)

    def test_open_too_many_args(self):
        """Extra positional args still hit the usage hint; the new no-arg
        path is covered separately."""
        self.channel._process_update(
            _helpers.make_message("/open GATE-PASTUR extra")
        )
        self.assertIn("Usage:", self.cap.last_reply)

    def test_open_unavailable_when_radio_callback_missing(self):
        """A channel created without `lora_command` (e.g. during a brief
        boot window) must surface a useful error, not crash."""
        self.channel._lora_command = None
        self.channel._process_update(_helpers.make_message("/open GATE-PASTUR"))
        self.assertIn("LoRa radio not ready", self.cap.last_reply)

    def test_open_success_uses_friendly_name(self):
        self.next_command = {
            "outcome": "ok",
            "reply": {"type": "alert", "state": "open"},
        }
        self.channel._process_update(_helpers.make_message("/open GATE-PASTUR"))
        self.assertIn("🔓 Opened Pasture (GATE-PASTUR)", self.cap.last_reply)
        self.assertEqual(self.command_calls, [("GATE-PASTUR", "open")])

    def test_open_noop_when_already_open(self):
        self.next_command = {
            "outcome": "noop",
            "reply": {"type": "ack", "result": "already_open"},
        }
        self.channel._process_update(_helpers.make_message("/open GATE-PASTUR"))
        self.assertIn("was already open", self.cap.last_reply)
        self.assertIn("no relay pulse", self.cap.last_reply)

    def test_open_no_challenge(self):
        """Gate doesn't answer challenge_req — suggests the gate is off
        or out of range."""
        self.next_command = {"outcome": "no_challenge"}
        self.channel._process_update(_helpers.make_message("/open GATE-PASTUR"))
        self.assertIn("did not answer the challenge", self.cap.last_reply)

    def test_open_timeout_after_challenge(self):
        """Gate answered challenge_req but never confirmed the command —
        the relay may have fired, suggest /status to check."""
        self.next_command = {"outcome": "timeout"}
        self.channel._process_update(_helpers.make_message("/open GATE-PASTUR"))
        text = self.cap.last_reply
        self.assertIn("did not confirm", text)
        self.assertIn("`/status GATE-PASTUR`", text)
        self.assertNotIn("{gate_id}", text,
                         "placeholder must be substituted, not literal")

    def test_open_send_failed_says_base_side(self):
        """Distinguishing fix from the review pass: serial-write
        failure is a base-side problem, not gate-side."""
        self.next_command = {"outcome": "send_failed"}
        self.channel._process_update(_helpers.make_message("/open GATE-PASTUR"))
        self.assertIn("base-side problem", self.cap.last_reply)

    def test_close_drives_command_with_action_close(self):
        self.next_command = {
            "outcome": "ok",
            "reply": {"type": "status", "state": "closed"},
        }
        self.channel._process_update(_helpers.make_message("/close GATE-PASTUR"))
        self.assertIn("🔒 Closed Pasture", self.cap.last_reply)
        self.assertEqual(self.command_calls, [("GATE-PASTUR", "close")])


class OpenCloseDefaultGateTests(_LoraCommandsCase):
    """`/open` and `/close` accept the gate ID as optional — if exactly
    one gate is paired we auto-select it, with zero or multiple we
    refuse rather than guess (acting on the wrong gate physically
    moves something).

    The shared `_LoraCommandsCase.setUp` registers two gates; tests
    that need a single-gate scenario unregister one, and the zero-gate
    scenario unregisters both.
    """

    def test_open_no_args_with_zero_gates_explains(self):
        self.registry.unregister_gate("GATE-PASTUR")
        self.registry.unregister_gate("GATE-DRIVE1")
        self.channel._process_update(_helpers.make_message("/open"))
        text = self.cap.last_reply
        self.assertIn("No gates registered", text)
        self.assertIn("/pair", text)
        self.assertEqual(self.command_calls, [],
                         "no LoRa traffic when no gate to drive")

    def test_open_no_args_with_one_gate_auto_selects(self):
        self.registry.unregister_gate("GATE-DRIVE1")
        self.next_command = {
            "outcome": "ok",
            "reply": {"type": "alert", "state": "open"},
        }
        self.channel._process_update(_helpers.make_message("/open"))
        self.assertIn("🔓 Opened Pasture (GATE-PASTUR)", self.cap.last_reply)
        self.assertEqual(self.command_calls, [("GATE-PASTUR", "open")])

    def test_open_no_args_with_many_gates_asks_which(self):
        self.channel._process_update(_helpers.make_message("/open"))
        text = self.cap.last_reply
        self.assertIn("Multiple gates", text)
        # Both gates must be named so the operator can copy/paste one.
        self.assertIn("Pasture", text)
        self.assertIn("GATE-DRIVE1", text)
        self.assertEqual(self.command_calls, [],
                         "ambiguous /open must not fire any LoRa traffic")

    def test_close_no_args_with_one_gate_auto_selects(self):
        """Same path as /open — proves the action carries through and
        the auto-select isn't accidentally hard-coded to open."""
        self.registry.unregister_gate("GATE-DRIVE1")
        self.next_command = {
            "outcome": "ok",
            "reply": {"type": "status", "state": "closed"},
        }
        self.channel._process_update(_helpers.make_message("/close"))
        self.assertIn("🔒 Closed Pasture", self.cap.last_reply)
        self.assertEqual(self.command_calls, [("GATE-PASTUR", "close")])

    def test_no_args_message_mentions_the_action_in_use(self):
        """The 'multiple gates' prompt suggests the same command the
        operator just typed — a /close hint with 'try /open' would be
        confusing."""
        self.channel._process_update(_helpers.make_message("/close"))
        self.assertIn("/close GATE-XXXX", self.cap.last_reply)


class StatusOneGateTests(_LoraCommandsCase):
    def test_status_one_malformed_id(self):
        self.channel._process_update(_helpers.make_message("/status nope"))
        self.assertIn("isn't a valid gate ID", self.cap.last_reply)
        self.assertEqual(self.status_calls, [])

    def test_status_one_unregistered(self):
        self.channel._process_update(_helpers.make_message("/status GATE-NOPEXX"))
        self.assertIn("not registered", self.cap.last_reply)

    def test_status_one_success_renders_live_state(self):
        self.next_status = {
            "outcome": "ok",
            "reply": {"type": "status", "state": "closed"},
        }
        self.channel._process_update(_helpers.make_message("/status GATE-PASTUR"))
        text = self.cap.last_reply
        self.assertIn("Pasture (GATE-PASTUR): CLOSED", text)
        self.assertIn("live", text)

    def test_status_one_timeout(self):
        self.next_status = {"outcome": "timeout"}
        self.channel._process_update(_helpers.make_message("/status GATE-PASTUR"))
        self.assertIn("did not confirm", self.cap.last_reply)

    def test_status_one_send_failed(self):
        self.next_status = {"outcome": "send_failed"}
        self.channel._process_update(_helpers.make_message("/status GATE-PASTUR"))
        self.assertIn("base-side problem", self.cap.last_reply)

    def test_status_one_unavailable_when_callback_missing(self):
        self.channel._lora_status_request = None
        self.channel._process_update(_helpers.make_message("/status GATE-PASTUR"))
        self.assertIn("LoRa radio not ready", self.cap.last_reply)


class StatusListLiveTests(_LoraCommandsCase):
    """`/status` (no arg) queries each registered gate over LoRa and
    renders the per-gate state next to the metadata, falling back to
    the latest event-log state when a gate doesn't reply.

    `_LoraCommandsCase` already wires `fake_status` into the channel,
    so /status no-arg goes through the same scripted callable as
    /status GATE-X. We can't differentiate per-gate replies with the
    single-script setUp, so these tests script one outcome and assert
    the per-gate rendering.
    """

    def test_live_open_renders_unlocked_emoji(self):
        self.next_status = {
            "outcome": "ok",
            "reply": {"type": "status", "state": "open"},
        }
        self.channel._process_update(_helpers.make_message("/status"))
        text = self.cap.last_reply
        self.assertIn("🔓 OPEN", text)
        self.assertIn("Pasture (GATE-PASTUR)", text)

    def test_live_closed_renders_locked_emoji(self):
        self.next_status = {
            "outcome": "ok",
            "reply": {"type": "status", "state": "closed"},
        }
        self.channel._process_update(_helpers.make_message("/status"))
        self.assertIn("🔒 CLOSED", self.cap.last_reply)

    def test_live_timeout_falls_back_to_event_log(self):
        """Live query times out → use the most recent event-log state,
        clearly marked 'last seen' so the operator knows it might be
        stale."""
        self.registry.log_event(
            "GATE-PASTUR", self.bs.EVENT_GATE_STATE, "alert:open"
        )
        self.next_status = {"outcome": "timeout"}
        self.channel._process_update(_helpers.make_message("/status"))
        text = self.cap.last_reply
        self.assertIn("🔓 last seen OPEN", text)
        self.assertIn("no live reply", text)

    def test_live_timeout_with_no_prior_state_shows_no_data(self):
        """First-ever /status against a freshly-paired gate that's
        offline — no live reply AND no prior event-log row. Must say
        'no data' rather than crash or show a misleading state."""
        self.next_status = {"outcome": "timeout"}
        self.channel._process_update(_helpers.make_message("/status"))
        self.assertIn("❓ no data", self.cap.last_reply)
        self.assertIn("no live reply", self.cap.last_reply)

    def test_live_send_failed_falls_back_to_event_log(self):
        """Same fallback path as timeout — any non-ok outcome surfaces
        the cached state."""
        self.registry.log_event(
            "GATE-PASTUR", self.bs.EVENT_GATE_STATE, "status:closed"
        )
        self.next_status = {"outcome": "send_failed"}
        self.channel._process_update(_helpers.make_message("/status"))
        self.assertIn("🔒 last seen CLOSED", self.cap.last_reply)

    def test_live_query_runs_for_every_registered_gate(self):
        """Two gates registered → fake_status must be called for both.
        Catches a regression where the loop accidentally short-circuits
        after the first failure."""
        self.next_status = {"outcome": "timeout"}
        self.channel._process_update(_helpers.make_message("/status"))
        self.assertEqual(
            sorted(self.status_calls),
            ["GATE-DRIVE1", "GATE-PASTUR"],
            "both registered gates must be queried",
        )

    def test_live_status_falls_back_when_lora_callback_missing(self):
        """If the channel was created without a LoRa callback (brief
        boot window) the per-gate line should still render from the
        event log, not crash with AttributeError or NoneType call."""
        self.registry.log_event(
            "GATE-PASTUR", self.bs.EVENT_GATE_STATE, "alert:open"
        )
        self.channel._lora_status_request = None
        self.channel._process_update(_helpers.make_message("/status"))
        text = self.cap.last_reply
        self.assertIn("🔓 last seen OPEN", text)
        # And the unconditional event-log row check should not have
        # incremented the LoRa-call count for the no-prior gate.
        self.assertIn("❓ no data", text)


class StatusTimeoutDisplayTests(_LoraCommandsCase):
    """The adaptive /open and /close grace periods are surfaced in both
    /status (list) and /status GATE-X. Operators get the warmup vs
    learned distinction inline so they can tell whether the displayed
    value reflects this gate's actual history."""

    def _seed_actuation(self, gate_id: str, action: str, samples: list[int]) -> None:
        for d in samples:
            self.registry.record_actuation(gate_id, action, d)

    def test_status_one_includes_timeout_line_on_success(self):
        self.next_status = {
            "outcome": "ok",
            "reply": {"type": "status", "state": "closed"},
        }
        self.channel._process_update(
            _helpers.make_message("/status GATE-PASTUR")
        )
        text = self.cap.last_reply
        self.assertIn("⏱", text)
        self.assertIn("open", text)
        self.assertIn("close", text)
        # State line comes first; timeout line second.
        state_idx = text.find("CLOSED (live)")
        timeout_idx = text.find("⏱")
        self.assertGreater(state_idx, 0)
        self.assertGreater(timeout_idx, state_idx)

    def test_status_one_warmup_label_with_no_history(self):
        """Fresh gate with no actuations recorded — both buckets
        should render as (warmup), not as (n=0)."""
        self.next_status = {
            "outcome": "ok",
            "reply": {"type": "status", "state": "open"},
        }
        self.channel._process_update(
            _helpers.make_message("/status GATE-PASTUR")
        )
        text = self.cap.last_reply
        # Two "warmup" occurrences, one for each action.
        self.assertEqual(text.count("warmup"), 2)
        self.assertNotIn("n=0", text)

    def test_status_one_uses_n_count_once_warmed(self):
        """Past MIN_SAMPLES the tag switches to n=X. The threshold
        also drops below the warmup ceiling."""
        self._seed_actuation(
            "GATE-PASTUR", "open", [12000, 12500, 11500, 12200, 12800]
        )
        self.next_status = {
            "outcome": "ok",
            "reply": {"type": "status", "state": "open"},
        }
        self.channel._process_update(
            _helpers.make_message("/status GATE-PASTUR")
        )
        text = self.cap.last_reply
        self.assertIn("open ~", text)
        self.assertIn("(n=5)", text)
        # The close bucket is still empty, so it stays in warmup.
        self.assertIn("warmup", text)

    def test_status_one_failure_still_shows_timeouts(self):
        """The grace periods are stable metadata, not live data.
        Show them even when the gate didn't reply to status_req — the
        operator can still see the wait they'd face on the next
        attempt."""
        self.next_status = {"outcome": "timeout"}
        self.channel._process_update(
            _helpers.make_message("/status GATE-PASTUR")
        )
        text = self.cap.last_reply
        self.assertIn("did not confirm", text)
        self.assertIn("⏱", text)
        self.assertIn("open", text)

    def test_status_list_shows_timeouts_per_gate(self):
        """The no-arg /status list gets a timeout line under each
        gate, indented to read as supplementary detail."""
        self._seed_actuation(
            "GATE-PASTUR", "open", [10000, 11000, 12000, 13000, 14000]
        )
        self.next_status = {
            "outcome": "ok",
            "reply": {"type": "status", "state": "open"},
        }
        self.channel._process_update(_helpers.make_message("/status"))
        text = self.cap.last_reply
        # One timeout line per gate (we have two gates in setUp).
        self.assertEqual(text.count("⏱"), 2)
        # Indented under each gate bullet, not flush-left.
        for line in text.splitlines():
            if "⏱" in line:
                self.assertTrue(
                    line.startswith(" "),
                    f"timeout line should be indented: {line!r}",
                )

    def test_status_list_distinguishes_warmed_and_warmup_gates(self):
        """One gate has open history, the other doesn't — both should
        render but with different tags so the operator can see which
        gates have learned values."""
        self._seed_actuation(
            "GATE-PASTUR", "open", [9000, 10000, 11000, 9500, 10500]
        )
        self.next_status = {"outcome": "timeout"}
        self.channel._process_update(_helpers.make_message("/status"))
        text = self.cap.last_reply
        # Pasture's open bucket is warmed → "n=5". Drive1 has no
        # history → "warmup". Both visible in the same message.
        self.assertIn("n=5", text)
        self.assertIn("warmup", text)


if __name__ == "__main__":
    unittest.main()
