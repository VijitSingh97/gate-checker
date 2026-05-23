# Ranch OS — outstanding TODOs

## Security
(All three previous security TODOs landed — gate key migrated off FAT32
via gate-config-migrate.service, bot-token redaction wired into
base_station.py's error path, password-strength tracking comment added
to flash_base_station.py. Verify checks for each are in scripts/verify_image.sh.)

## User flow
(All initial user-flow items landed:)
- (Done) Form-format validation in provision.py catches typos at submit
  time; base_station.py flips back into the captive portal with a banner
  if the first Telegram ping returns a 4xx. Sentinel
  /var/lib/base_station/.setup_validated tracks once-only behavior.
- (Done) Captive portal slimmed to credentials-only. Section 2 "Register
  a Gate Monitor" card and /add_gate route removed — gate pairing moves
  to Telegram per the roadmap. provision.py no longer touches the gates
  table; base_station.py creates it via CREATE TABLE IF NOT EXISTS.
- (Done) "Settings saved" page rewritten with a 3-step timeline and an
  explicit "look for the online ping in Telegram" call-out.
- (Done) GATE_ID_ALPHABET stripped of I/O/0/1, length bumped to 6.
  32^6 ≈ 1.07B keyspace, birthday collision at ~32k gates.

## Consistency / cleanup
(All Consistency items landed. `factory_sticker.py` now owns the
sticker layout for both factory scripts; `run_build.sh` forwards
`RANCH_BUILD_TARGETS`; the `wait-sync` comment in
`base-station.service` matches today's in-process NTP behaviour; the
`remote_build.sh` rsync excludes were trimmed; `app.secret_key` was
already absent.)

## Reliability
(All Reliability items landed. Watchdog now requires both
`nmcli STATE` AND a TCP probe to 1.1.1.1:53 to call the device
connected, threshold raised to 30 min. verify_image.sh asserts both
`serial-getty@ttyAMA0` and `serial-getty@ttyS0` are masked.)

## Captive portal UX
**Decision (Session 9): not doing.** Native captive-portal detection
on iOS/Android needs either (a) replacing NM's shared-mode internal
dnsmasq with our own DHCP+DNS server gated on AP-up, or (b) iptables
REDIRECT rules covering 80/443/53 with correct teardown on AP→station
handoff. Modern OSes also probe over HTTPS so a DNS-only hijack often
fails to trigger the sheet anyway. Estimated 4–6 hours of careful work
+ real iOS/Android testing, against a payoff of "operator skips typing
one short URL that's already on the sticker." Sticker + setup
instructions stay as-is.

## Roadmap (not "TODO" exactly, but tracking)
- (Done — Session 10) Telegram command channel + /pair, /unpair,
  /rename, /confirm, /cancel, /status, /help. TelegramCommandChannel
  runs as a daemon thread off BaseStation.run(); DM-only for /pair;
  deleteMessage on the operator's /pair line; constant-time token
  compare; 60s pending-action TTL; 5-per-hour /pair rate limit. Gates
  grew a `name` column (optional, defaults to gate ID); alert text
  renders "Name (GATE-XXXX): OPEN" when a name is set.
- (Done — Session 11) /open, /close, /status GATE-XXXX driven by
  BaseStation.lora_command / lora_status_request — challenge_req →
  challenge_resp → command(action, nonce) → ack/alert sequence over
  the existing LoRa port, with _lora_tx_lock around writes and
  _LoRaRequestSlot for per-gate single-flight reply routing. Wire
  code is in place and smoke-tested against a mocked transport;
  real-hardware validation still pending.
- (Done — Session 11) /factory_reset with disconnect-after-ack
  ordering — confirmation prompt names the current Wi-Fi SSID +
  paired gate count + event count, /confirm fires the ack via the
  closure (so the dispatcher doesn't double-send), then spawns a
  daemon thread that sleeps to flush, unlinks events.db +
  base_config.env, deletes the nmcli station profile, and starts
  base-provision.service before os._exit(0). verify_image.sh asserts
  the os._exit call site.
- (Done — Session 11) Per-user allow-list removed.
  TELEGRAM_ALLOWED_USERS is gone; the configured TELEGRAM_CHAT_ID is
  the auth boundary. verify_image.sh check_no_grep asserts no stale
  references slip back in. /pair stays DM-only on the chat-history
  axis.
- (Done — Session 12) Persistent test suite. 111 stdlib-unittest
  tests under tests/ covering factory_sticker, _redact_token,
  GateRegistry (incl. ALTER migration + accept_seq replay), every
  TelegramCommandChannel handler, the LoRa transport, /factory_reset
  wipe ordering, and the watchdog dual-signal check. Runs in ~7s via
  scripts/run_tests.sh; .githooks/pre-commit calls it after the
  factory-deps check. Skips gracefully (exit 0 + visible warning)
  when cryptography isn't installed in the dev env. See
  tests/README.md.
- (Done — Session 12) Code-review fixes: _lora_failure_text cleaned
  up (no more .replace("{gate_id}", ...) hack); send_failed now
  surfaces distinctly from no_challenge/timeout in lora_command /
  lora_status_request; /unpair and /rename validate GATE_ID_RE;
  _handle_command uses defensive chat-id access throughout;
  ranch-wifi-watchdog systemctl calls now have timeouts so a
  misbehaving systemd can't wedge the watchdog.
- Real-hardware validation of the LoRa command path. Need to flash a
  gate + a base, /pair the gate over Telegram, send /status GATE-X
  and /open GATE-X, and confirm relay fires + state-change alert
  comes back. The wire-format choices in gate_client.py
  :_handle_message could still shift if anything breaks; both files
  need to move in lockstep.
- Validate production-profile builds end-to-end. Only dev profile has
  been booted on real hardware.
- Hardware platform decision for gates (Pi Zero W vs Pico W). Defer
  until end-to-end validation on Pi Zero W succeeds.
