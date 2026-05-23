"""Tests for `ranch-wifi-watchdog.py` — specifically the dual-signal
"connected" check introduced in Session 8.

The watchdog calls the device "connected" only when BOTH `nmcli STATE`
reports `connected*` AND a 3-second TCP probe to `1.1.1.1:53`
succeeds. The probe replaces the older single-signal check that
flipped to "connected" the moment the link-layer association
completed — well before the upstream was reachable.

We use a local TCP listener as a stand-in for `1.1.1.1:53` so the
tests don't depend on real internet access in the dev environment.
"""

from __future__ import annotations

import socket
import threading
import unittest
from contextlib import contextmanager

from tests import _helpers


@contextmanager
def _local_listener():
    """Bring up a TCP listener on an OS-chosen free port. Yields the
    (host, port) tuple. Accepts and immediately closes any incoming
    connection — enough to satisfy a TCP-handshake probe."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(5)
    host, port = sock.getsockname()
    stop = threading.Event()

    def _accept_loop():
        sock.settimeout(0.1)
        while not stop.is_set():
            try:
                client, _ = sock.accept()
                client.close()
            except socket.timeout:
                continue
            except OSError:
                return

    thread = threading.Thread(target=_accept_loop, daemon=True)
    thread.start()
    try:
        yield host, port
    finally:
        stop.set()
        sock.close()
        thread.join(timeout=1)


class UpstreamReachableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.wd = _helpers.import_watchdog()

    def test_probe_succeeds_when_target_accepts(self):
        with _local_listener() as (host, port):
            self.wd.UPSTREAM_PROBE_HOST = host
            self.wd.UPSTREAM_PROBE_PORT = port
            self.wd.UPSTREAM_PROBE_TIMEOUT_SECONDS = 1
            self.assertTrue(self.wd._upstream_reachable())

    def test_probe_fails_on_blackhole(self):
        """TEST-NET-2 (RFC 5737) is guaranteed unrouted on the public
        internet. Connect attempts time out, which is exactly the
        signal a real upstream failure would produce."""
        self.wd.UPSTREAM_PROBE_HOST = "198.51.100.1"
        self.wd.UPSTREAM_PROBE_PORT = 53
        self.wd.UPSTREAM_PROBE_TIMEOUT_SECONDS = 1
        self.assertFalse(self.wd._upstream_reachable())

    def test_probe_fails_on_closed_port(self):
        """A port that's closed on a real host returns ConnectionRefused
        immediately — not the same as a timeout, but the probe must
        treat it as a reachability failure either way."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        _, port = sock.getsockname()
        sock.close()  # release port so connect() refuses
        self.wd.UPSTREAM_PROBE_HOST = "127.0.0.1"
        self.wd.UPSTREAM_PROBE_PORT = port
        self.wd.UPSTREAM_PROBE_TIMEOUT_SECONDS = 1
        self.assertFalse(self.wd._upstream_reachable())


class HaveInternetTests(unittest.TestCase):
    """`_have_internet` AND's `_nm_says_connected` with
    `_upstream_reachable`. Both must agree."""

    def setUp(self) -> None:
        self.wd = _helpers.import_watchdog()

    def test_returns_false_when_nm_says_disconnected(self):
        self.wd._nm_says_connected = lambda: False
        self.wd._upstream_reachable = lambda: True
        self.assertFalse(self.wd._have_internet())

    def test_returns_false_when_probe_fails(self):
        self.wd._nm_says_connected = lambda: True
        self.wd._upstream_reachable = lambda: False
        self.assertFalse(self.wd._have_internet())

    def test_returns_true_when_both_agree(self):
        self.wd._nm_says_connected = lambda: True
        self.wd._upstream_reachable = lambda: True
        self.assertTrue(self.wd._have_internet())


class NmSaysConnectedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.wd = _helpers.import_watchdog()

    def _stub_run(self, stdout: str, returncode: int = 0):
        class _Result:
            pass
        result = _Result()
        result.stdout = stdout
        result.returncode = returncode
        self.wd.subprocess.run = lambda *a, **k: result

    def test_connected_accepts_simple_form(self):
        self._stub_run("connected\n")
        self.assertTrue(self.wd._nm_says_connected())

    def test_connected_accepts_qualified_forms(self):
        """NM can report `connected (site)` or `connected (local)`
        when only LAN connectivity is up. The TCP probe layer rejects
        these in practice, but the nmcli check is liberal."""
        for state in ("connected (site)\n", "connected (local)\n"):
            self._stub_run(state)
            self.assertTrue(self.wd._nm_says_connected())

    def test_disconnected_rejected(self):
        self._stub_run("disconnected\n")
        self.assertFalse(self.wd._nm_says_connected())

    def test_returns_false_when_nmcli_fails(self):
        self._stub_run("", returncode=1)
        self.assertFalse(self.wd._nm_says_connected())


if __name__ == "__main__":
    unittest.main()
