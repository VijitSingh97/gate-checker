"""Tests for `/factory_reset` — the confirmation flow + the
disconnect-after-ack daemon thread that wipes device state and hands
wlan0 back to the captive-portal AP.

Ordering matters here: the operator-visible ack must fire *before*
the disruptive work begins. The `/confirm` dispatcher must not
double-reply since the closure already sent its own ack. And the
daemon thread's wipe order (unlink → nmcli delete → systemctl start
base-provision → os._exit) is load-bearing for the disconnect-after-ack
property documented in TELEGRAM.md.

We mock `os._exit`, `os.unlink`, and `subprocess.run` so the wipe
doesn't actually nuke the developer's machine, then assert on the
recorded call sequence.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from tests import _helpers


class FactoryResetConfirmFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bs = _helpers.import_base_station()
        self.db_path = tempfile.mktemp(suffix=".db")
        self.registry = self.bs.GateRegistry(self.db_path)
        key = _helpers.fresh_fernet_key()
        self.registry.register_gate("GATE-AAAAAA", key, "Front")
        self.registry.register_gate("GATE-BBBBBB", key, None)

        self.reset_calls: list = []
        self.channel = self.bs.TelegramCommandChannel(
            bot_token=_helpers.DEFAULT_BOT_TOKEN,
            configured_chat_id=str(_helpers.DEFAULT_CHAT_ID),
            registry=self.registry,
            factory_reset_callback=lambda c, s: self.reset_calls.append((c, s)),
        )
        self.cap = _helpers.CapturingChannel(self.channel)

        # _current_wifi_ssid normally hits real nmcli. Stub at the
        # module level so the test is deterministic.
        self.bs._current_wifi_ssid = lambda: "home-2.4G"

    def tearDown(self) -> None:
        self.registry.close()
        try:
            os.unlink(self.db_path)
        except FileNotFoundError:
            pass

    def test_rejects_extra_args(self):
        self.channel._process_update(
            _helpers.make_message("/factory_reset GATE-AAAAAA")
        )
        self.assertIn("takes no arguments", self.cap.last_reply)
        self.assertEqual(self.reset_calls, [])

    def test_unavailable_when_callback_missing(self):
        """If somehow the channel was constructed without a callback
        (smoke test, broken plumbing), the command must refuse rather
        than crash."""
        self.channel._factory_reset_callback = None
        self.channel._process_update(_helpers.make_message("/factory_reset"))
        self.assertIn("not available", self.cap.last_reply)

    def test_prompt_lists_ssid_gates_and_event_count(self):
        self.channel._process_update(_helpers.make_message("/factory_reset"))
        prompt = self.cap.last_reply
        # SSID is quoted so the operator can see escape sequences clearly.
        self.assertIn('"home-2.4G"', prompt)
        self.assertIn("2 paired gate(s)", prompt)
        # Named gate uses name; unnamed gate uses its id.
        self.assertIn("Front", prompt)
        self.assertIn("GATE-BBBBBB", prompt)
        # Confirmation token is issued.
        self.assertIn("/confirm", prompt)

    def test_prompt_when_ssid_lookup_fails(self):
        self.bs._current_wifi_ssid = lambda: None
        self.channel._process_update(_helpers.make_message("/factory_reset"))
        self.assertIn('"(unknown)"', self.cap.last_reply)

    def test_confirm_fires_ack_then_callback_once(self):
        """The closure sends the ack itself and returns None so the
        /confirm dispatcher doesn't double-send. The callback then
        gets the chat_id and (possibly empty) ssid."""
        self.channel._process_update(_helpers.make_message("/factory_reset"))
        token = self.channel._pending[_helpers.DEFAULT_OPERATOR_ID].token
        self.cap.reset()
        self.channel._process_update(_helpers.make_message(f"/confirm {token}"))

        self.assertEqual(
            self.reset_calls, [(_helpers.DEFAULT_CHAT_ID, "home-2.4G")],
            "callback fires exactly once with chat_id + ssid",
        )
        # Exactly one ack arrived, from the closure's own _send call —
        # the dispatcher must not have double-sent.
        acks = [r for r in self.cap.replies if "Resetting now" in r[1]]
        self.assertEqual(len(acks), 1, f"ack should be sent exactly once; got {acks}")

    def test_cancel_clears_pending_factory_reset(self):
        self.channel._process_update(_helpers.make_message("/factory_reset"))
        self.assertIn(_helpers.DEFAULT_OPERATOR_ID, self.channel._pending)
        self.channel._process_update(_helpers.make_message("/cancel"))
        self.assertNotIn(_helpers.DEFAULT_OPERATOR_ID, self.channel._pending)
        self.assertEqual(self.reset_calls, [])


class PerformFactoryResetTests(unittest.TestCase):
    """Direct test for `_perform_factory_reset` — the daemon-thread
    body that actually wipes state. We replace every system-mutating
    call with a recording stub so the test doesn't touch the real
    machine.

    The point of these tests is the call ORDER: unlink before nmcli
    delete, nmcli delete before systemctl start, systemctl start
    before os._exit. Misordering any of these breaks the
    disconnect-after-ack property documented in TELEGRAM.md.
    """

    def setUp(self) -> None:
        self.bs = _helpers.import_base_station()
        # `_perform_factory_reset` sleeps 2s for ack flush; the test
        # patches that down so the test runs in milliseconds.
        self.bs.FACTORY_RESET_ACK_FLUSH_SECONDS = 0.01

        self.calls: list = []

        def _fake_unlink(path):
            self.calls.append(("unlink", path))

        def _fake_run(argv, **kwargs):
            self.calls.append(("run", tuple(argv)))
            class _Result:
                returncode = 0
            return _Result()

        def _fake_exit(code):
            self.calls.append(("exit", code))
            raise SystemExit(code)  # let the test's try/except catch

        self._restore = [
            (self.bs.os, "unlink", self.bs.os.unlink),
            (self.bs.subprocess, "run", self.bs.subprocess.run),
            (self.bs.os, "_exit", self.bs.os._exit),
        ]
        self.bs.os.unlink = _fake_unlink
        self.bs.subprocess.run = _fake_run
        self.bs.os._exit = _fake_exit

    def tearDown(self) -> None:
        for module, name, original in self._restore:
            setattr(module, name, original)

    def test_wipe_order_is_load_bearing(self):
        with self.assertRaises(SystemExit):
            self.bs._perform_factory_reset("home-2.4G")

        # Extract op names in order.
        ops = [c[0] for c in self.calls]
        self.assertEqual(
            ops,
            ["unlink", "unlink", "run", "run", "exit"],
            f"unexpected wipe order: {self.calls}",
        )

        unlinks = [c[1] for c in self.calls if c[0] == "unlink"]
        self.assertIn(self.bs.DB_PATH, unlinks)
        self.assertIn(self.bs.CONFIG_PATH, unlinks)

        runs = [c[1] for c in self.calls if c[0] == "run"]
        # First subprocess call: nmcli delete the station profile.
        self.assertEqual(runs[0][:3], ("nmcli", "connection", "delete"))
        self.assertEqual(runs[0][3], "home-2.4G")
        # Second subprocess call: systemctl --no-block start base-provision.
        self.assertEqual(
            runs[1],
            ("systemctl", "--no-block", "start", "base-provision.service"),
        )
        # Final call: os._exit(0).
        self.assertEqual(self.calls[-1], ("exit", 0))

    def test_skips_nmcli_when_ssid_unknown(self):
        """If `_current_wifi_ssid` returned None, the caller passes the
        empty string and we must NOT call `nmcli connection delete ''`
        — that would either delete some random connection named ""
        or just fail noisily."""
        with self.assertRaises(SystemExit):
            self.bs._perform_factory_reset("")

        runs = [c[1] for c in self.calls if c[0] == "run"]
        self.assertEqual(len(runs), 1, "only systemctl should run, no nmcli")
        self.assertEqual(runs[0][0], "systemctl")

    def test_unlink_missing_file_is_tolerated(self):
        """Reset must complete even if one of the wiped paths is
        already gone — partial wipes mid-reset are common."""
        def _missing_unlink(path):
            self.calls.append(("unlink", path))
            raise FileNotFoundError(path)
        self.bs.os.unlink = _missing_unlink

        with self.assertRaises(SystemExit):
            self.bs._perform_factory_reset("home-2.4G")
        # Still proceeds through nmcli delete + systemctl + os._exit.
        ops = [c[0] for c in self.calls]
        self.assertEqual(ops.count("run"), 2)
        self.assertEqual(ops[-1], "exit")


class CurrentWifiSsidTests(unittest.TestCase):
    """`_current_wifi_ssid` parses `nmcli connection show --active`
    output. We stub subprocess.run to feed it scripted outputs."""

    def setUp(self) -> None:
        self.bs = _helpers.import_base_station()
        self._orig_run = self.bs.subprocess.run

    def tearDown(self) -> None:
        self.bs.subprocess.run = self._orig_run

    def _stub_run(self, stdout: str, returncode: int = 0):
        class _Result:
            pass
        result = _Result()
        result.stdout = stdout
        result.returncode = returncode
        self.bs.subprocess.run = lambda *a, **k: result

    def test_picks_wireless_connection(self):
        self._stub_run("home-2.4G:802-11-wireless\neth0:802-3-ethernet\n")
        self.assertEqual(self.bs._current_wifi_ssid(), "home-2.4G")

    def test_no_active_wireless_returns_none(self):
        self._stub_run("eth0:802-3-ethernet\n")
        self.assertIsNone(self.bs._current_wifi_ssid())

    def test_nmcli_nonzero_exit_returns_none(self):
        self._stub_run("", returncode=1)
        self.assertIsNone(self.bs._current_wifi_ssid())

    def test_handles_escaped_colon_in_name(self):
        """NM escapes a literal colon in the connection NAME with a
        backslash in -t output. We unescape it."""
        self._stub_run("home\\:net:802-11-wireless\n")
        self.assertEqual(self.bs._current_wifi_ssid(), "home:net")


if __name__ == "__main__":
    unittest.main()
