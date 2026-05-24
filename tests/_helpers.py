"""Shared fixtures and import shims for the ranch-os test suite.

`base_station.py` and `ranch-wifi-watchdog.py` import `requests` and
`serial` at module load. We never want the tests to do real HTTP or
real serial I/O, so this module installs minimal fakes for both
modules into `sys.modules` BEFORE importing the device-side code.
`cryptography` is *not* stubbed — the suite uses real Fernet keys to
exercise key-validation paths, so the dev needs it installed.

Typical test-file preamble:

    from tests import _helpers
    bs = _helpers.import_base_station()

After import, individual tests can swap in their own scripted
behaviour by reassigning attributes on `sys.modules["requests"]` or
on instances of `TelegramCommandChannel`, e.g. via
`CapturingChannel`.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BASE_STATION_DIR = os.path.join(REPO_ROOT, "ranch_os", "package", "base-station")


# --------------------------------------------------------------------------
# Module-level stubs
# --------------------------------------------------------------------------

def _install_stubs() -> None:
    """Inject fake `requests` and `serial` modules if not already loaded.

    Tests that need scripted HTTP behaviour can replace
    `sys.modules["requests"]` themselves after the stubs go in.
    """
    if "requests" not in sys.modules:
        fake_requests = types.ModuleType("requests")

        class _Response:
            status_code = 200

            def json(self):  # noqa: D401 — match real API shape
                return {"ok": True, "result": []}

        def _noop_post(url, json=None, timeout=None):
            return _Response()

        def _noop_get(url, params=None, timeout=None):
            return _Response()

        fake_requests.post = _noop_post
        fake_requests.get = _noop_get

        class RequestException(Exception):
            """Stand-in for real `requests.exceptions.RequestException`."""

        fake_requests.RequestException = RequestException
        sys.modules["requests"] = fake_requests

    if "serial" not in sys.modules:
        fake_serial = types.ModuleType("serial")

        class SerialException(Exception):
            """Stand-in for real `serial.SerialException`."""

        fake_serial.SerialException = SerialException

        class Serial:
            """Minimal stand-in. Tests that exercise the LoRa transport
            replace `BaseStation.lora` with a richer fake."""

            def __init__(self, *args, **kwargs) -> None:
                self.is_open = True
                self.in_waiting = 0

            def close(self) -> None:
                self.is_open = False

            def write(self, data: bytes) -> int:
                return len(data)

            def flush(self) -> None:
                pass

            def readline(self) -> bytes:
                return b""

        fake_serial.Serial = Serial
        sys.modules["serial"] = fake_serial


_install_stubs()


# --------------------------------------------------------------------------
# Module loaders
# --------------------------------------------------------------------------

def _load_from_path(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def import_base_station():
    """Load `base_station.py` into a fresh module named "base_station".

    Each call returns a *new* module object so tests get isolated
    state (e.g. monkey-patches don't leak between files). If a test
    needs to share state across calls within a single test method,
    it should hold onto the returned module.
    """
    return _load_from_path(
        "base_station", os.path.join(_BASE_STATION_DIR, "base_station.py")
    )


def import_watchdog():
    """Load `ranch-wifi-watchdog.py` (hyphen in filename, so we load it
    explicitly rather than rely on `import`).
    """
    return _load_from_path(
        "ranch_wifi_watchdog",
        os.path.join(_BASE_STATION_DIR, "ranch-wifi-watchdog.py"),
    )


def import_factory_sticker():
    """Load `factory_sticker.py` from the repo root."""
    return _load_from_path(
        "factory_sticker", os.path.join(REPO_ROOT, "factory_sticker.py")
    )


# --------------------------------------------------------------------------
# Test fixtures
# --------------------------------------------------------------------------

DEFAULT_OPERATOR_ID = 9001
DEFAULT_CHAT_ID = 4242
DEFAULT_GROUP_CHAT_ID = -1001
DEFAULT_BOT_TOKEN = "111111111:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def make_message(
    text: str,
    *,
    user_id: int = DEFAULT_OPERATOR_ID,
    chat_id: int = DEFAULT_CHAT_ID,
    chat_type: str = "private",
    message_id: int = 1,
    update_id: int = 1,
) -> dict:
    """Build a synthetic Telegram update payload.

    Returns the dict that `TelegramCommandChannel._process_update`
    expects to receive — an object with `update_id` and a `message`
    sub-object carrying `text`, `from.id`, `chat.id`, `chat.type`,
    and `message_id`.
    """
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "text": text,
            "from": {"id": user_id},
            "chat": {"id": chat_id, "type": chat_type},
        },
    }


class CapturingChannel:
    """Test wrapper that records what a TelegramCommandChannel would send.

    Replaces `_send` and `_delete_message` on the channel with
    list-collecting stubs. Tests assert on `replies` (list of
    `(chat_id, text)`) and `deletes` (list of `(chat_id, message_id)`).
    """

    def __init__(self, channel) -> None:
        self.channel = channel
        self.replies: list[tuple] = []
        self.deletes: list[tuple] = []
        channel._send = self._send
        channel._delete_message = self._delete

    def _send(self, chat_id, text):
        self.replies.append((chat_id, text))

    def _delete(self, chat_id, message_id):
        self.deletes.append((chat_id, message_id))

    @property
    def last_reply(self) -> str | None:
        return self.replies[-1][1] if self.replies else None

    def reset(self) -> None:
        self.replies.clear()
        self.deletes.clear()


class CapturingNotifier:
    """Stand-in for `TelegramNotifier` that records every `send()`.

    `BaseStation._dispatch` calls `self.notifier.send(...)` when a
    state change is worth pinging Telegram about. Tests that exercise
    the dispatch notify rules (alert always notifies; status notifies
    only on a real state transition) need to see whether send was
    called AND what message was passed. The real TelegramNotifier
    short-circuits to `ok=False` when bot_token is empty, so it does
    the right thing functionally but tells us nothing about what
    *would* have been sent — this stub fills that gap.
    """

    def __init__(self) -> None:
        self.sent: list[str] = []

    @property
    def configured(self) -> bool:
        return True

    def send(self, message: str):
        self.sent.append(message)
        # Match TelegramNotifier's return type loosely. Tests that
        # check sent-or-not don't look at the return value; tests
        # that need a TelegramSendResult-shaped object should pass
        # a real TelegramNotifier with empty creds instead.
        from types import SimpleNamespace
        return SimpleNamespace(ok=True, status_code=200, reason=None, transient=False)

    @property
    def last_sent(self) -> str | None:
        return self.sent[-1] if self.sent else None

    def reset(self) -> None:
        self.sent.clear()


def fresh_fernet_key() -> str:
    """Return a brand-new url-safe-base64 Fernet key as a str.

    Mirrors `provision_gate.generate_fernet_key`; reproduced here so
    the tests don't depend on the factory script.
    """
    import base64
    import secrets

    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
