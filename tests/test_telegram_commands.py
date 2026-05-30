"""Tests for the `TelegramCommandChannel` command handlers that touch
only the SQLite registry — `/pair`, `/unpair`, `/rename`, `/confirm`,
`/cancel`, `/status` (registry dump), `/help`.

LoRa-driven commands (`/open`, `/close`, `/status GATE-XXXX`) live in
test_lora_commands.py because they require a richer transport mock.
`/factory_reset` lives in test_factory_reset.py for similar reasons.

Conventions:
  - One assertion focus per test method.
  - `self.channel` is the unit under test; `self.cap` records its
    outbound `_send` / `_delete_message` calls.
  - Tests drive the channel via `_process_update` with synthetic
    update payloads built by `_helpers.make_message`.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest

from tests import _helpers


class _ChannelCase(unittest.TestCase):
    """Common scaffolding: in-memory DB, channel with capturing send."""

    def setUp(self) -> None:
        self.bs = _helpers.import_base_station()
        self.db_path = tempfile.mktemp(suffix=".db")
        self.registry = self.bs.GateRegistry(self.db_path)
        self.channel = self.bs.TelegramCommandChannel(
            bot_token=_helpers.DEFAULT_BOT_TOKEN,
            configured_chat_id=str(_helpers.DEFAULT_CHAT_ID),
            registry=self.registry,
        )
        self.cap = _helpers.CapturingChannel(self.channel)
        self.key = _helpers.fresh_fernet_key()

    def tearDown(self) -> None:
        self.registry.close()
        try:
            os.unlink(self.db_path)
        except FileNotFoundError:
            pass


class AuthorizationTests(_ChannelCase):
    def test_drops_message_from_unconfigured_chat(self):
        """Chat-ID-only auth: foreign chats are silently dropped."""
        self.channel._process_update(
            _helpers.make_message("/help", chat_id=99999)
        )
        self.assertEqual(self.cap.replies, [])

    def test_ignores_plain_chat(self):
        """Non-slash messages are dropped without a reply."""
        self.channel._process_update(_helpers.make_message("hello there"))
        self.assertEqual(self.cap.replies, [])

    def test_ignores_empty_text(self):
        """Telegram updates with no text key (e.g. photo) are dropped."""
        update = {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "from": {"id": 1},
                "chat": {"id": _helpers.DEFAULT_CHAT_ID, "type": "private"},
            },
        }
        self.channel._process_update(update)
        self.assertEqual(self.cap.replies, [])

    def test_no_attribute_for_removed_allow_list(self):
        """Defence in depth: the user-ID allow-list was removed in
        favour of "the chat IS the auth boundary". Make sure nothing
        reintroduces it via a stray attribute."""
        self.assertFalse(hasattr(self.channel, "_allowed_user_ids"))


class DispatcherTests(_ChannelCase):
    def test_unknown_command_replies_with_hint(self):
        self.channel._process_update(_helpers.make_message("/blorp"))
        self.assertIn("Unknown command", self.cap.last_reply)
        self.assertIn("/help", self.cap.last_reply)

    def test_unparseable_shlex_replies_politely(self):
        """Unclosed quote returns a useful error instead of a stack trace."""
        self.channel._process_update(
            _helpers.make_message('/pair GATE-AAAAAA "broken')
        )
        self.assertIn("unmatched quote", self.cap.last_reply)

    def test_botname_suffix_is_stripped(self):
        """Telegram appends @BotUsername in group chats. We strip it."""
        self.channel._process_update(_helpers.make_message("/help@RanchBot"))
        self.assertIn("Ranch base station commands", self.cap.last_reply)


class PairTests(_ChannelCase):
    def test_pair_in_group_chat_is_rejected_dm_only(self):
        """The Fernet key transits the chat; a group would broadcast it.
        The channel must surface the DM-only requirement regardless of
        what the operator configured TELEGRAM_CHAT_ID to."""
        self.channel._configured_chat_id = str(_helpers.DEFAULT_GROUP_CHAT_ID)
        self.channel._process_update(
            _helpers.make_message(
                f"/pair GATE-AAAAAA {self.key} Group",
                chat_id=_helpers.DEFAULT_GROUP_CHAT_ID,
                chat_type="group",
            )
        )
        self.assertIn("DM", self.cap.last_reply)
        self.assertIsNone(self.registry.get_gate("GATE-AAAAAA"))

    def test_pair_with_too_few_args_returns_usage(self):
        self.channel._process_update(_helpers.make_message("/pair GATE-AAAAAA"))
        self.assertIn("Usage:", self.cap.last_reply)
        self.assertIsNone(self.registry.get_gate("GATE-AAAAAA"))

    def test_pair_new_gate_with_quoted_name(self):
        self.channel._process_update(
            _helpers.make_message(
                f'/pair GATE-AAAAAA {self.key} "Front Pasture"',
                message_id=42,
            )
        )
        # Operator's /pair line is deleted regardless of validity, to
        # minimise chat-history exposure.
        self.assertIn(
            (_helpers.DEFAULT_CHAT_ID, 42), self.cap.deletes,
            "operator's /pair line must be deleteMessage'd",
        )
        self.assertIn("Paired Front Pasture", self.cap.last_reply)
        row = self.registry.get_gate("GATE-AAAAAA")
        self.assertEqual(row["lora_key"], self.key)
        self.assertEqual(row["name"], "Front Pasture")

    def test_pair_without_name_defaults_to_gate_id_label(self):
        self.channel._process_update(
            _helpers.make_message(f"/pair GATE-AAAAAA {self.key}")
        )
        self.assertIn("Paired GATE-AAAAAA", self.cap.last_reply)
        self.assertIsNone(self.registry.get_gate("GATE-AAAAAA")["name"])

    def test_pair_invalid_fernet_key_rejects(self):
        self.channel._process_update(
            _helpers.make_message("/pair GATE-AAAAAA not-a-real-key Name")
        )
        self.assertIn("Invalid Fernet key", self.cap.last_reply)
        self.assertIsNone(self.registry.get_gate("GATE-AAAAAA"))

    def test_pair_malformed_gate_id_rejects(self):
        self.channel._process_update(
            _helpers.make_message(f"/pair bad-id {self.key} Name")
        )
        self.assertIn("doesn't look like a gate ID", self.cap.last_reply)

    def test_pair_name_too_long_rejected(self):
        long_name = "x" * (self.bs.GATE_NAME_MAX_LEN + 1)
        self.channel._process_update(
            _helpers.make_message(
                f'/pair GATE-AAAAAA {self.key} "{long_name}"'
            )
        )
        self.assertIn("Name too long", self.cap.last_reply)

    def test_pair_overwrite_requires_confirm(self):
        """Re-pairing an existing gate is destructive (last_seq resets)
        so it routes through /confirm rather than running synchronously."""
        self.registry.register_gate("GATE-AAAAAA", self.key, "Old")
        new_key = _helpers.fresh_fernet_key()
        self.channel._process_update(
            _helpers.make_message(f"/pair GATE-AAAAAA {new_key} New")
        )
        self.assertIn("/confirm", self.cap.last_reply)
        # DB still has the OLD key — overwrite hasn't happened yet.
        self.assertEqual(
            self.registry.get_gate("GATE-AAAAAA")["lora_key"], self.key
        )
        # Pending action is stashed.
        self.assertIn(_helpers.DEFAULT_OPERATOR_ID, self.channel._pending)

    def test_pair_rate_limit_fires_after_max_attempts(self):
        """5 attempts in the window are fine; the 6th gets refused.
        Defends against typo storms that would otherwise leave many
        garbage rows in chat history."""
        for i in range(self.bs.PAIR_RATE_LIMIT_MAX_ATTEMPTS):
            self.channel._process_update(
                _helpers.make_message(
                    f'/pair GATE-RT{i:04d} {self.key} "x"',
                    message_id=100 + i,
                )
            )
        self.cap.reset()
        self.channel._process_update(
            _helpers.make_message(
                f'/pair GATE-OVER01 {self.key} "x"', message_id=200,
            )
        )
        self.assertIn("Too many", self.cap.last_reply)


class UnpairTests(_ChannelCase):
    def test_unpair_unknown_gate(self):
        self.channel._process_update(_helpers.make_message("/unpair GATE-NOPEXX"))
        self.assertIn("not registered", self.cap.last_reply)
        self.assertNotIn(_helpers.DEFAULT_OPERATOR_ID, self.channel._pending)

    def test_unpair_malformed_id(self):
        self.channel._process_update(_helpers.make_message("/unpair bad"))
        self.assertIn("isn't a valid gate ID", self.cap.last_reply)

    def test_unpair_then_confirm_removes_gate(self):
        self.registry.register_gate("GATE-AAAAAA", self.key, "Front")
        self.channel._process_update(_helpers.make_message("/unpair GATE-AAAAAA"))
        self.assertIn("/confirm", self.cap.last_reply)
        token = self.channel._pending[_helpers.DEFAULT_OPERATOR_ID].token
        self.cap.reset()
        self.channel._process_update(_helpers.make_message(f"/confirm {token}"))
        self.assertIn("Removed", self.cap.last_reply)
        self.assertIsNone(self.registry.get_gate("GATE-AAAAAA"))

    def test_unpair_then_cancel_leaves_gate_alone(self):
        self.registry.register_gate("GATE-AAAAAA", self.key, "Front")
        self.channel._process_update(_helpers.make_message("/unpair GATE-AAAAAA"))
        self.channel._process_update(_helpers.make_message("/cancel"))
        self.assertIn("Cancelled", self.cap.last_reply)
        self.assertIsNotNone(self.registry.get_gate("GATE-AAAAAA"))
        self.assertNotIn(_helpers.DEFAULT_OPERATOR_ID, self.channel._pending)


class RenameTests(_ChannelCase):
    def test_rename_valid(self):
        self.registry.register_gate("GATE-AAAAAA", self.key, "Old")
        self.channel._process_update(
            _helpers.make_message('/rename GATE-AAAAAA "New Name"')
        )
        self.assertIn("Renamed", self.cap.last_reply)
        self.assertEqual(self.registry.get_gate("GATE-AAAAAA")["name"], "New Name")

    def test_rename_unknown_gate(self):
        self.channel._process_update(
            _helpers.make_message('/rename GATE-NOPEXX "x"')
        )
        self.assertIn("not registered", self.cap.last_reply)

    def test_rename_empty_name(self):
        self.registry.register_gate("GATE-AAAAAA", self.key, "Old")
        self.channel._process_update(_helpers.make_message('/rename GATE-AAAAAA ""'))
        self.assertIn("Name cannot be empty", self.cap.last_reply)

    def test_rename_too_long(self):
        self.registry.register_gate("GATE-AAAAAA", self.key, "Old")
        too_long = "x" * (self.bs.GATE_NAME_MAX_LEN + 1)
        self.channel._process_update(
            _helpers.make_message(f'/rename GATE-AAAAAA "{too_long}"')
        )
        self.assertIn("Name too long", self.cap.last_reply)

    def test_rename_malformed_id(self):
        self.channel._process_update(_helpers.make_message('/rename bad "name"'))
        self.assertIn("isn't a valid gate ID", self.cap.last_reply)


class RelayTests(_ChannelCase):
    """`/relay` shows or sets the per-gate relay pulse duration. Stored
    in registered_gates.relay_ms and shipped in the next /open or
    /close command frame; applies immediately, no /confirm (it's not
    destructive — same posture as /rename)."""

    def test_set_relay_seconds(self):
        self.registry.register_gate("GATE-AAAAAA", self.key, "Front")
        self.channel._process_update(
            _helpers.make_message("/relay GATE-AAAAAA 1.5")
        )
        self.assertIn("set to 1.5s", self.cap.last_reply)
        self.assertEqual(self.registry.get_relay_ms("GATE-AAAAAA"), 1500)

    def test_set_whole_second_renders_without_decimal(self):
        self.registry.register_gate("GATE-AAAAAA", self.key, None)
        self.channel._process_update(
            _helpers.make_message("/relay GATE-AAAAAA 2")
        )
        self.assertIn("set to 2s", self.cap.last_reply)
        self.assertEqual(self.registry.get_relay_ms("GATE-AAAAAA"), 2000)

    def test_query_shows_current_value(self):
        self.registry.register_gate("GATE-AAAAAA", self.key, "Front")
        self.registry.set_relay_ms("GATE-AAAAAA", 1800)
        self.channel._process_update(
            _helpers.make_message("/relay GATE-AAAAAA")
        )
        text = self.cap.last_reply
        self.assertIn("relay press time is 1.8s", text)
        # Query must not change the stored value.
        self.assertEqual(self.registry.get_relay_ms("GATE-AAAAAA"), 1800)

    def test_query_fresh_gate_shows_default(self):
        self.registry.register_gate("GATE-AAAAAA", self.key, None)
        self.channel._process_update(
            _helpers.make_message("/relay GATE-AAAAAA")
        )
        self.assertIn("relay press time is 1s", self.cap.last_reply)

    def test_unknown_gate(self):
        self.channel._process_update(
            _helpers.make_message("/relay GATE-NOPEXX 1.5")
        )
        self.assertIn("not registered", self.cap.last_reply)

    def test_malformed_gate_id(self):
        self.channel._process_update(_helpers.make_message("/relay bad 1.5"))
        self.assertIn("isn't a valid gate ID", self.cap.last_reply)

    def test_non_numeric_value_rejected(self):
        self.registry.register_gate("GATE-AAAAAA", self.key, None)
        self.channel._process_update(
            _helpers.make_message("/relay GATE-AAAAAA soon")
        )
        self.assertIn("isn't a number", self.cap.last_reply)
        # Value left untouched.
        self.assertEqual(
            self.registry.get_relay_ms("GATE-AAAAAA"),
            self.bs.RELAY_PULSE_DEFAULT_MS,
        )

    def test_below_minimum_rejected(self):
        self.registry.register_gate("GATE-AAAAAA", self.key, None)
        self.channel._process_update(
            _helpers.make_message("/relay GATE-AAAAAA 0.01")
        )
        self.assertIn("must be between", self.cap.last_reply)
        self.assertEqual(
            self.registry.get_relay_ms("GATE-AAAAAA"),
            self.bs.RELAY_PULSE_DEFAULT_MS,
        )

    def test_above_maximum_rejected(self):
        self.registry.register_gate("GATE-AAAAAA", self.key, None)
        self.channel._process_update(
            _helpers.make_message("/relay GATE-AAAAAA 60")
        )
        self.assertIn("must be between", self.cap.last_reply)
        self.assertEqual(
            self.registry.get_relay_ms("GATE-AAAAAA"),
            self.bs.RELAY_PULSE_DEFAULT_MS,
        )

    def test_non_finite_value_rejected(self):
        """nan/inf parse as floats but explode on int(round()). They must
        be rejected cleanly, not escape to the dispatch loop and leave the
        operator with no reply."""
        self.registry.register_gate("GATE-AAAAAA", self.key, None)
        for token in ("nan", "inf", "-inf", "infinity"):
            with self.subTest(token=token):
                self.channel._process_update(
                    _helpers.make_message(f"/relay GATE-AAAAAA {token}")
                )
                self.assertIn("isn't a number", self.cap.last_reply)
                self.assertEqual(
                    self.registry.get_relay_ms("GATE-AAAAAA"),
                    self.bs.RELAY_PULSE_DEFAULT_MS,
                )

    def test_no_args_shows_usage(self):
        self.channel._process_update(_helpers.make_message("/relay"))
        self.assertIn("Usage: /relay", self.cap.last_reply)

    def test_too_many_args_shows_usage(self):
        self.registry.register_gate("GATE-AAAAAA", self.key, None)
        self.channel._process_update(
            _helpers.make_message("/relay GATE-AAAAAA 1.5 extra")
        )
        self.assertIn("Usage: /relay", self.cap.last_reply)


class ConfirmCancelTests(_ChannelCase):
    def test_confirm_with_no_pending(self):
        self.channel._process_update(_helpers.make_message("/confirm dead"))
        self.assertIn("Nothing to confirm", self.cap.last_reply)

    def test_confirm_wrong_token_keeps_pending(self):
        self.registry.register_gate("GATE-AAAAAA", self.key, "x")
        self.channel._process_update(_helpers.make_message("/unpair GATE-AAAAAA"))
        self.cap.reset()
        self.channel._process_update(_helpers.make_message("/confirm wrong"))
        self.assertIn("doesn't match", self.cap.last_reply)
        # Pending survives so a typo doesn't waste the 60s window.
        self.assertIn(_helpers.DEFAULT_OPERATOR_ID, self.channel._pending)

    def test_confirm_expired_token(self):
        """Stash a pending action, force its issued_at into the past,
        then /confirm. Token should be reported as expired and the
        pending action evicted."""
        import dataclasses
        self.registry.register_gate("GATE-AAAAAA", self.key, "x")
        self.channel._process_update(_helpers.make_message("/unpair GATE-AAAAAA"))
        pending = self.channel._pending[_helpers.DEFAULT_OPERATOR_ID]
        self.channel._pending[_helpers.DEFAULT_OPERATOR_ID] = dataclasses.replace(
            pending, issued_at=time.monotonic() - 999
        )
        self.cap.reset()
        self.channel._process_update(_helpers.make_message(f"/confirm {pending.token}"))
        self.assertIn("expired", self.cap.last_reply)
        self.assertNotIn(_helpers.DEFAULT_OPERATOR_ID, self.channel._pending)
        # Gate was not removed — expiry must not double as confirmation.
        self.assertIsNotNone(self.registry.get_gate("GATE-AAAAAA"))

    def test_confirm_is_single_use(self):
        """After a successful /confirm, the same token cannot be replayed."""
        self.registry.register_gate("GATE-AAAAAA", self.key, "x")
        self.channel._process_update(_helpers.make_message("/unpair GATE-AAAAAA"))
        token = self.channel._pending[_helpers.DEFAULT_OPERATOR_ID].token
        self.channel._process_update(_helpers.make_message(f"/confirm {token}"))
        self.cap.reset()
        self.channel._process_update(_helpers.make_message(f"/confirm {token}"))
        self.assertIn("Nothing to confirm", self.cap.last_reply)

    def test_cancel_without_pending(self):
        self.channel._process_update(_helpers.make_message("/cancel"))
        self.assertIn("Nothing pending", self.cap.last_reply)


class StatusAndHelpTests(_ChannelCase):
    def test_status_empty(self):
        self.channel._process_update(_helpers.make_message("/status"))
        self.assertIn("No gates registered", self.cap.last_reply)

    def test_status_empty_includes_base_id_header(self):
        """Empty registry should still carry the device-id + SSID header
        so the operator can confirm which base they're talking to."""
        self.channel._process_update(_helpers.make_message("/status"))
        text = self.cap.last_reply
        self.assertIn("📋 Base:", text)
        self.assertIn("Wi-Fi:", text)

    def test_status_with_gates(self):
        self.registry.register_gate("GATE-AAAAAA", self.key, "Front Pasture")
        self.registry.register_gate("GATE-BBBBBB", self.key, None)
        self.channel._process_update(_helpers.make_message("/status"))
        text = self.cap.last_reply
        self.assertIn("2 gate(s) registered", text)
        # Named gate shows name + id; unnamed shows just id.
        self.assertIn("Front Pasture (GATE-AAAAAA)", text)
        self.assertIn("GATE-BBBBBB", text)
        self.assertNotIn("Front Pasture (GATE-BBBBBB)", text)
        # Header carries base device id and SSID — must come BEFORE the
        # gate list so the operator sees identity first.
        self.assertIn("📋 Base:", text)
        self.assertIn("Wi-Fi:", text)
        self.assertLess(text.find("📋 Base:"), text.find("Front Pasture"))

    def test_gates_alias(self):
        self.channel._process_update(_helpers.make_message("/gates"))
        self.assertIn("No gates registered", self.cap.last_reply)

    def test_help_lists_all_command_groups(self):
        self.channel._process_update(_helpers.make_message("/help"))
        text = self.cap.last_reply
        for token in (
            "/pair", "/unpair", "/rename", "/relay",
            "/status", "/open", "/close",
            "/factory_reset", "/confirm", "/cancel", "/help",
            "chat itself is the auth boundary",
        ):
            self.assertIn(token, text)

    def test_start_alias(self):
        self.channel._process_update(_helpers.make_message("/start"))
        self.assertIn("Ranch base station commands", self.cap.last_reply)


if __name__ == "__main__":
    unittest.main()
