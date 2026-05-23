# Ranch OS — test suite

Stdlib `unittest` suite covering the base-station application code,
the captive-portal helpers it shares, and the Wi-Fi watchdog. 111
tests; runs in ~7 seconds on a developer laptop.

## Running

```bash
# Default — looks for `python3` on PATH, gracefully skips if the dev
# environment doesn't have Python 3.10+ or the `cryptography` package.
scripts/run_tests.sh

# Verbose (per-test names + dots)
scripts/run_tests.sh -v

# Pick a specific module
scripts/run_tests.sh tests.test_telegram_commands

# Pick a specific test
scripts/run_tests.sh tests.test_telegram_commands.PairTests.test_pair_in_group_chat_is_rejected_dm_only

# Use a specific Python (e.g. a venv)
PYTHON=/path/to/venv/bin/python scripts/run_tests.sh
```

The runner wraps `python3 -m unittest discover -s tests` with two
short-circuits: if Python is older than 3.10 or `cryptography` is
absent, it prints a clear "install with `pip install cryptography`"
message and exits 0, so first-time contributors aren't blocked from
committing while they set up their venv.

## Pre-commit hook

`.githooks/pre-commit` runs the suite after the factory-deps check.
Enable once per clone:

```bash
git config core.hooksPath .githooks
```

A failed hook prints the unittest output and how to re-run with `-v`.
A skipped hook (no cryptography) lets the commit through with a
visible warning — explicit opt-in to slow you down isn't worth the
friction for new contributors.

## Layout

```
tests/
├── __init__.py
├── _helpers.py              # shared stubs, module loaders, fixtures
├── test_factory_sticker.py  # print_sticker layout + edge cases
├── test_redaction.py        # _redact_token regex
├── test_gate_registry.py    # SQLite schema, migration, register/unpair/seq
├── test_telegram_commands.py  # /pair /unpair /rename /confirm /cancel /status /help, auth
├── test_lora_commands.py    # /open /close /status GATE-XXXX dispatch
├── test_lora_transport.py   # BaseStation.lora_command + _route_to_waiter
├── test_factory_reset.py    # /factory_reset prompt + _perform_factory_reset ordering
└── test_watchdog.py         # _have_internet + TCP probe semantics
```

### Why a `_helpers.py` instead of a `conftest.py`

We use stdlib `unittest`, not pytest. `_helpers.py` is the module
each test file imports for shared fixtures (`make_message`,
`CapturingChannel`, `fresh_fernet_key`) and the loaders that pull
`base_station.py` / `ranch-wifi-watchdog.py` in via `importlib`.

The hyphen in `ranch-wifi-watchdog.py` is the real reason for the
explicit loader — that filename isn't a valid Python import target,
so the test suite gives it the local name `ranch_wifi_watchdog`.

### Stubbing strategy

`_helpers.py` injects minimal fakes for `serial` and `requests` into
`sys.modules` BEFORE the device-side code is imported, so the test
runner never touches a real radio or makes real HTTP. Individual
tests that need richer behaviour (recording serial frames, scripting
HTTP responses) swap their own fakes in via attribute assignment —
e.g. `_RecordingSerial` in `test_lora_transport.py` or
`CapturingChannel` for outbound Telegram messages.

`cryptography` is **not** stubbed. The suite uses real Fernet keys
because the `/pair` invalid-key path exercises `Fernet(key.encode())`
validation, and faking that out would invalidate the test.

## What's covered (by source area)

| Source | Tests |
| --- | --- |
| `factory_sticker.print_sticker` | layout invariants, column alignment, empty-input rejection |
| `base_station._redact_token` | token redaction inside URLs, multiple occurrences, false-positive avoidance |
| `base_station.GateRegistry` | schema + idempotent ALTER migration, register/unregister/rename, name semantics, accept_seq replay protection, last_seq reset on key change |
| `base_station.TelegramCommandChannel` (registry side) | every command handler, DM-only `/pair`, rate limit, pending action TTL, single-use tokens, alias commands, dispatcher edge cases |
| `base_station.TelegramCommandChannel` (LoRa side) | outcome → message mapping for `/open`, `/close`, `/status GATE-XXXX`, including the Session-11 send_failed-vs-no_challenge distinction |
| `base_station.BaseStation` LoRa transport | `lora_command` happy path, no_challenge, timeout, send_failed; `lora_status_request`; `_route_to_waiter` correctness; single-flight refusal |
| `base_station._perform_factory_reset` | wipe ordering (unlink → nmcli delete → systemctl → os._exit), tolerance for missing files, skip of `nmcli` on unknown SSID |
| `base_station._current_wifi_ssid` | nmcli output parsing, escaped-colon names, failure modes |
| `ranch-wifi-watchdog._upstream_reachable` | TCP probe against a local listener, blackhole timeout, refused-port |
| `ranch-wifi-watchdog._have_internet` | dual-signal AND semantics |
| `ranch-wifi-watchdog._nm_says_connected` | nmcli state parsing, qualified-connected forms |

## What's intentionally not covered

These need real hardware or a live build, and live in
`scripts/verify_image.sh` (a separate kind of test that runs against
a built `.img`):

- The Buildroot config and shipped systemd unit files.
- The captive portal's Flask app and `_complete_setup` thread —
  needs Flask in the test env; the relevant invariants are checked
  via `verify_image.sh:check_grep` on the deployed `provision.py`.
- The factory provisioner scripts — those are covered by
  `scripts/check_factory_deps.py` (the stdlib-only invariant) and
  by manual flash-and-verify on real SD cards.
- The actual end-to-end LoRa command path — `_lora_request` is unit
  tested with a mocked serial port, but the real Pi → LoRa module →
  air → gate path still needs hardware validation. Listed as the
  next milestone in [Todo.md](../Todo.md).

## Adding a new test

1. Pick the file that matches the source area, or add a new
   `tests/test_<area>.py`.
2. Import the fixture loader: `from tests import _helpers`.
3. Load the module under test inside `setUp` (or once at module
   load if the test class doesn't mutate the module): `self.bs =
   _helpers.import_base_station()`.
4. Drive `TelegramCommandChannel` via `_helpers.make_message` +
   `_helpers.CapturingChannel`; assert on `cap.replies` and
   `cap.deletes`.
5. Run `scripts/run_tests.sh -v` to confirm the new tests show up
   and pass.
