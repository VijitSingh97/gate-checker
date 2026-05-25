# Telegram operations manual

Everything you can do with the base station after it's online. Each
command lists its **syntax**, an **example**, the **bot's reply** in
the success and failure cases, and **when you'd use it**.

If you're setting up a fresh device for the first time, start with
[USER_GUIDE.md](USER_GUIDE.md) instead — it walks through the
out-of-box flow. This document is the reference manual you'll come
back to when you have a specific command in mind.

---

## Quick reference

| Command | What it does |
| --- | --- |
| `/help` | Print every command the bot knows. Aliased as `/start`. |
| `/status` | List paired gates, last-seen timestamps, sequence counters. |
| `/status GATE-XXXX` | Live query: ask the gate over LoRa what state it's in right now. |
| `/pair GATE-XXXX <key> ["Name"]` | Register a gate. **Must be sent in a private DM with the bot.** |
| `/unpair GATE-XXXX` | Remove a gate. Requires `/confirm`. |
| `/rename GATE-XXXX "New Name"` | Change a gate's display name. |
| `/open [GATE-XXXX]` | Open a gate via the LoRa challenge/command sequence. |
| `/close [GATE-XXXX]` | Close a gate. |
| `/factory_reset` | Wipe Wi-Fi, Telegram, and gate state; reboot into the captive portal. Requires `/confirm`. |
| `/confirm <token>` | Acknowledge the most recent destructive prompt. |
| `/cancel` | Abort any pending `/confirm`. |

---

## Setting up Telegram for your base station

You do this once, before plugging in the base station for the first
time.

### 1. Create a bot with @BotFather

Open Telegram on your phone, search for `@BotFather`, and start a
chat with it. Then:

1. Send `/newbot`.
2. Pick a display name (any human-readable string).
3. Pick a username — must end in `bot`, e.g. `MyRanchAlertBot`.
4. BotFather replies with a token that looks like
   `123456789:ABCdef...`. **This is your `TELEGRAM_TOKEN`.** Keep it
   private; anyone with this token can send messages as your bot.

The token never expires by default. If you ever leak it, send
`/revoke` to BotFather and start over.

### 2. Find your chat ID

The chat ID is the numeric identifier of the conversation the bot
should send alerts to. For a 1-on-1 chat with the bot, this is your
personal user ID; for a group chat, it's the group ID.

**Easiest path:**

1. Send any message to your bot ("hi" works).
2. In a browser, visit
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   (substitute your real token).
3. Look for `"chat":{"id":123456789, …}` in the JSON response. That
   number is your `TELEGRAM_CHAT_ID`.

For a **group chat**:

1. Create the group, invite the bot.
2. Send any message in the group.
3. Hit `getUpdates` the same way. The group's chat ID is **negative**
   (e.g. `-1001234567890`); preserve the leading minus.

### 3. Send the bot a "hi" before provisioning

Telegram bots can't initiate conversations — they can only reply.
Until you message your bot at least once from the chat you want to
use, the bot has no permission to send to it. The "hi" is the
trigger.

If you skip this step, the base station's first online ping will
fail with HTTP 403 ("bot can't initiate conversation with a user")
and the device will flip back to setup mode with an error banner.

### 4. Enter the credentials in the captive portal

When you first power on the base station, it broadcasts a Wi-Fi
network called `BaseStation_Setup` (no password). Connect to it,
open `http://10.42.0.1/`, log in with `admin` and the portal
password from the device's sticker, and fill in:

- Your home Wi-Fi SSID + password
- The bot token from step 1
- The chat ID from step 2

After you submit, the base station joins your Wi-Fi, the setup AP
disappears, and the first thing it does is send a "Gate Monitor Base
Station is Online." ping to the configured chat. **If you don't see
that ping within about a minute, something's wrong with the
credentials** — see [Troubleshooting](#troubleshooting).

For the full setup walkthrough including photos, see
[USER_GUIDE.md](USER_GUIDE.md).

---

## Notifications the bot sends you

These arrive unprompted, in response to physical events at the gates.

### The online ping

> 📡 Gate Monitor Base Station BASE-9A22 is Online.

Sent once on every boot of the base-station service, after the
device has confirmed its system clock is plausible (NTP) and the
Telegram TLS handshake works. This is the device's "I'm up" beacon.
The device ID (`BASE-XXXX`) makes it unambiguous which base just
came online if you've got more than one base reporting to the same
chat.

You'll see it:
- After the first captive-portal setup (means setup succeeded).
- After every reboot.
- After every successful Wi-Fi recovery (e.g. the operator
  re-entered Wi-Fi credentials following a router replacement).

### Gate state alerts

> 🔓 Front Pasture (GATE-A1B2C3): OPEN
> 🔒 Front Pasture (GATE-A1B2C3): CLOSED

Sent whenever a paired gate's state changes — both opens and closes.
The leading emoji (🔓 / 🔒) makes the state visible at a glance in
the notification tray. If the gate has a display name (set via
`/pair` or `/rename`), the message shows it with the gate ID in
parentheses; otherwise just the ID.

**Dedup rules:** the base only fires a Telegram message on a real
state transition. A `/status GATE-X` reply that comes back showing
the gate in the same state it was already known to be in does
**not** also fire an unsolicited notification — that would
double-spam every status check. The de-dup compares against the
most recently logged event in the SQLite gate-events table.

### What the bot will not send

- **No periodic heartbeat.** If you want one, point an external
  uptime monitor at the base station's Telegram online-ping
  cadence — at most an outage of `Restart=on-failure` + the watchdog
  threshold (30 min) will go undetected.
- **No batched / queued alerts.** A network outage means alerts
  fired during the outage are logged locally but not flushed when
  connectivity returns — by the time you'd be reading them they'd
  be misleading.
- **No diagnostic chatter.** The bot stays quiet between alerts and
  your commands. If you want to see what's happening on the device,
  SSH into a dev image and read `journalctl`.

---

## Commands you can send the bot

Every command below works in the configured chat — whoever's in
that chat (whether it's just you, or a group of trusted family
members) can issue any of these. The bot rejects messages from any
other chat silently.

### `/help` — list every command

**Syntax:** `/help` (alias: `/start`)

**Example:**
```
/help
```

**Reply:** A grouped list of every command the bot understands,
ending with the security note that anyone in the chat can drive the
device.

Use this if you forget a syntax in the field. The bot will echo it
back to you on demand.

---

### `/status` — list paired gates

**Syntax:** `/status`  (alias: `/gates`)

**Example:**
```
/status
```

**Reply (no gates):**
```
📋 Base: BASE-9A22  •  Wi-Fi: HomeNetwork

No gates registered. Pair one with /pair GATE-XXXX <key> [name].
```

**Reply (with gates):**
```
📋 Base: BASE-9A22  •  Wi-Fi: HomeNetwork

2 gate(s) registered:
  • Front Pasture (GATE-A1B2C3): 🔓 OPEN
      ⏱ open ~15s (n=14) · close ~16s (n=18)
  • Back Gate (GATE-B7Z3K4): 🔒 last seen CLOSED (no live reply)
      ⏱ open ~30s (warmup) · close ~30s (warmup)
```

The header line carries the base's device ID and the currently-
connected Wi-Fi SSID, so if you've got multiple base stations
reporting into one chat you can tell which one replied, and you can
spot the "base is on the wrong / a backup network" failure mode
without having to SSH in. SSID is `(unknown)` if NetworkManager
can't report an active wireless connection (e.g. the operator
landed on the captive portal AP somehow, or NM is wedged).

Per-gate state comes from a live LoRa `status_req` to each registered
gate. A gate that responds in time shows its current state
(`🔓 OPEN` / `🔒 CLOSED`). A gate that doesn't reply within the LoRa
timeout falls back to the most recent state from the SQLite event
log, marked `last seen X (no live reply)` so you can tell it might
be stale. A freshly-paired gate that has never reported and isn't
responding shows `❓ no data (no live reply)`.

The `⏱` line under each gate is the adaptive grace period the base
will wait for `/open` and `/close` against that gate, separately for
each direction. `(n=X)` means the threshold is computed from this
gate's last X successful actuation cycles; `(warmup)` means the
gate hasn't logged enough samples yet (fewer than 5) and the base
is falling back to the 30s default ceiling. As the gate gets more
real-world use the threshold tightens to match the gate's actual
cycle time. Newly-paired gates start in warmup until you've used
`/open` or `/close` against them enough to fill the buffer.

The list is sorted by pairing time, oldest first.

---

### `/status GATE-XXXX` — live state query

**Syntax:** `/status GATE-XXXX`

**Example:**
```
/status GATE-A1B2C3
```

**Reply (success):**
```
🔒 Front Pasture (GATE-A1B2C3): CLOSED (live).
⏱ open ~15s (n=14) · close ~16s (n=18)
```

**Reply (gate offline / out of range):**
```
❌ Front Pasture (GATE-A1B2C3) did not answer the challenge. Is the gate powered on and in LoRa range?
⏱ open ~15s (n=14) · close ~16s (n=18)
```

**Reply (base-side radio failure):**
```
❌ Could not transmit to GATE-A1B2C3 — the LoRa serial write failed. Check the device log; this is a base-side problem, not the gate.
⏱ open ~15s (n=14) · close ~16s (n=18)
```

This sends a real `status_req` packet over LoRa and waits up to 5
seconds for the gate to reply. Different from `/status` (no arg),
which only reads the local database. The second line is the
adaptive `/open` and `/close` grace period for this gate (see the
no-arg `/status` section above for the `(n=X)` vs `(warmup)`
explanation). The grace-period line is stable metadata about the
gate's actuation profile, so it appears on both success and failure
replies — even when the gate didn't answer the live query, you can
still see the wait you'd face on the next attempt.

Use this when:
- The gate hasn't sent an alert in a while and you want to confirm
  it's still alive.
- You're verifying a gate is wired correctly after physical work.

---

### `/pair` — register a gate

**Syntax:** `/pair GATE-XXXX <fernet-key> ["Optional Name"]`

**Example:**
```
/pair GATE-A1B2C3 gAAAAABl1234567890_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx= "Front Pasture"
```

The key comes from the gate's factory sticker. The name is optional;
quote it if it contains spaces. If you omit the name, alerts show
just the gate ID.

**Reply (success, new gate):**
```
✅ Paired Front Pasture (GATE-A1B2C3).
```

The bot also immediately deletes your `/pair` message from the chat,
because the message contained your Fernet key. Telegram's own
backups will still have it for up to 48 hours — see
[The chat-history caveat](#the-chat-history-caveat).

**Reply (gate already paired):**
```
GATE-A1B2C3 is already paired (last_seq=147).
Confirm with `/confirm 7f3a` within 60s to overwrite. The Fernet key has already been redacted from your /pair line; this prompt does not echo it. Send /cancel to abort.
```

Re-pairing an existing gate with a new key resets the gate's
sequence counter — required because the new key implies a new gate
device with seq starting at 0. That's destructive (the old sequence
data is gone), so the bot routes it through `/confirm`.

**Reply (invalid key):**
```
Invalid Fernet key — keys are 44 url-safe-base64 characters. Check the gate's factory sticker and try /pair again. The key was redacted from your message but is still visible to anyone with access to Telegram backups for up to 48 hours.
```

**Reply (sent from a group):**
```
/pair must be sent in a private DM with the bot, not a group chat — the Fernet key would leak to every group member. DM me directly and try again.
```

#### `/pair` rules to remember

- **DM-only.** Even if your bot is in a group chat for alerts,
  `/pair` only works in a private DM with the bot. Open a 1-on-1
  chat with the bot to pair, then add it back to the group if
  that's where you want alerts.
- **Rate limited.** Max 5 `/pair` attempts per hour per user. Helps
  against typo storms that would otherwise pollute Telegram's
  backups with leaked keys.
- **Name length cap.** 64 characters max. Long names truncate the
  alert text on small phone screens.

#### The chat-history caveat

The Fernet key is a bearer credential for the gate. Anyone with the
key can decrypt that gate's alerts, forge alerts as that gate, and
(if the command channel is enabled) drive the gate's relay.

The bot calls `deleteMessage` on your `/pair` line the instant it
parses the command, but Telegram retains the message in its own
backups for up to 48 hours per their published policy, and any
client that already cached it before the deletion still has a copy.

If you can't accept that exposure, pair via the captive portal
instead — the key never transits Telegram in that path. To re-arm
the captive portal on a deployed base station, either:
- Run `/factory_reset` (wipes everything, including paired gates),
  or
- Take the base station offline for ≥30 minutes until the watchdog
  re-enables setup mode (preserves paired gates).

---

### `/unpair` — remove a gate

**Syntax:** `/unpair GATE-XXXX`

**Example:**
```
/unpair GATE-A1B2C3
```

**Reply:**
```
Confirm with `/confirm 7f3a` within 60s.
This will remove Front Pasture (GATE-A1B2C3) (last seen 2026-05-22 14:18:55, last_seq=147). Gate hardware will be unaffected; you can re-pair anytime with /pair. Send /cancel to abort.
```

After `/confirm <token>`:
```
✅ Removed Front Pasture (GATE-A1B2C3). Event history kept; re-pair anytime with /pair.
```

**Reply (unknown gate):**
```
GATE-NOPEXX is not registered.
```

**What this affects:**
- The gate's row in `registered_gates` is deleted; the base station
  ignores any future alerts from that gate ID.
- The event history (`gate_events` rows) is **kept** — useful for
  investigating "did this gate ever open last week?" after a
  gate's been physically replaced.
- The gate's SD card is **not** touched. You can re-pair the same
  gate with the same key any time, and it'll pick up where it left
  off — except the `last_seq` resets to 0, so you'll see one
  "Replay or out-of-order packet" warning in the device log per
  message until the gate's persisted seq counter catches back up
  (or you re-flash the gate, which resets its seq too).

---

### `/rename` — change a gate's display name

**Syntax:** `/rename GATE-XXXX "New Name"`

**Example:**
```
/rename GATE-A1B2C3 "North Driveway"
```

**Reply:**
```
✅ Renamed GATE-A1B2C3 → North Driveway.
```

**Reply (unknown gate):**
```
GATE-NOPEXX is not registered. Pair it first with /pair.
```

`/rename` does **not** require `/confirm` — it's idempotent and easy
to undo. Just `/rename` it back to the previous name if you change
your mind. Alerts from that gate will use the new name immediately.

---

### `/open` — open a gate

**Syntax:** `/open [GATE-XXXX]`

The gate ID is optional when exactly one gate is paired — the base
will auto-select it. With zero or multiple gates paired the base
asks you to specify, rather than guessing (driving the wrong gate
physically moves something).

**Example:**
```
/open GATE-A1B2C3
```

**Example (single-gate install, no ID needed):**
```
/open
```

**Reply (no gates paired):**
```
❌ No gates registered. Pair one with /pair GATE-XXXX <key> [name] before /open.
```

**Reply (multiple gates, ambiguous):**
```
❓ Multiple gates paired (Front Pasture, Back Pasture). Specify which: /open GATE-XXXX
```

**Reply (success):**
```
🔓 Opened Front Pasture (GATE-A1B2C3).
```

**Reply (gate was already open):**
```
ℹ️ Front Pasture (GATE-A1B2C3) was already open; no relay pulse fired.
```

**Reply (gate didn't answer):**
```
❌ Front Pasture (GATE-A1B2C3) did not answer the challenge. Is the gate powered on and in LoRa range?
```

**Reply (gate answered, but state didn't confirm):**
```
⚠️ Front Pasture (GATE-A1B2C3) accepted the challenge but did not confirm. The action may still have fired — send `/status GATE-A1B2C3` to check.
```

#### What happens under the hood

The base station sends an authenticated `challenge_req` to the gate.
The gate replies with a single-use, 15-second-lifetime random nonce.
The base then sends a `command(open, nonce)` packet. The gate
verifies the nonce, pulses the relay for 1 second, and the new
state propagates back as a normal `alert` packet.

If you see the success ack but the gate didn't physically move, the
issue is between the relay and the gate itself (wiring, motor
power) — the base's view of "the relay pulsed" is whatever the gate
reported back. `/status GATE-XXXX` is the live truth check.

---

### `/close` — close a gate

**Syntax:** `/close [GATE-XXXX]`

Identical to `/open` in every way, with `closed` as the target
state. Gate ID is optional under the same single-gate auto-select
rule. The success reply leads with 🔒 (matching the close-state
emoji):
```
🔒 Closed Front Pasture (GATE-A1B2C3).
```
Same failure modes as `/open`, same need for `/status` to
double-check on timeout.

---

### `/factory_reset` — wipe the base station

**Syntax:** `/factory_reset` (no arguments)

**Example:**
```
/factory_reset
```

**Reply:**
```
Confirm with `/confirm 9c12` within 60s.

This will wipe:
  • Wi-Fi credentials (currently: "home-2.4G")
  • Telegram bot token and chat ID
  • 3 paired gate(s): Front Pasture, Driveway, Back Pasture
  • Event history

The device will then reboot the captive portal AP (BaseStation_Setup) and you'll need to re-enter all of the above. Send /cancel to abort.
```

After `/confirm <token>`:
```
🔄 Resetting now. You will lose this chat until the device joins a new Wi-Fi via the BaseStation_Setup captive portal.
```

The bot delivers that ack first, *then* the device:
1. Sleeps 2 seconds to ensure the ack flushed through Telegram.
2. Deletes `events.db` and `base_config.env` from `/var/lib/base_station/`.
3. Removes the NetworkManager connection profile for your home Wi-Fi.
4. Starts the captive-portal service.
5. Exits its own process; systemd lets it stay dead because the
   config file is gone.

After the reset, the base station broadcasts `BaseStation_Setup`
again and waits for you to come back through the captive portal —
exactly like the first time you set it up, except the portal
password on the sticker still works (it's stored in `/boot/`, which
isn't wiped).

#### When you'd use this

- You're changing home networks (new ISP, new router, new SSID).
- You're handing the device to someone else — though if you want to
  wipe the portal password too, you need to re-flash the SD card.
- Something's deeply wrong with the device state and you want a
  known-good starting point.

#### What survives

- The portal password (from `/boot/provision_creds.env`) — needed
  to log back into the captive portal after the reset.
- The device's hostname and Buildroot OS — this is a *config* reset,
  not an OS re-flash. To wipe even the portal password, re-flash
  with `flash_base_station.py`.

#### What about a backup?

There isn't one. The base station has no upload target it can
trust — Telegram chat would echo the gate Fernet keys, same problem
as `/pair`. The realistic recovery path is "the operator still has
the sticker on each gate; the sticker is the source of truth."
`/factory_reset` deliberately trusts that property.

---

### `/confirm` — acknowledge a destructive prompt

**Syntax:** `/confirm <token>`

`/unpair`, `/pair`-with-overwrite, and `/factory_reset` don't run
immediately. They issue a short 4-hex-char token and wait up to 60
seconds for you to send `/confirm <token>` back.

**Reply (success):** Depends on the command — see each section.

**Reply (wrong token):**
```
Token doesn't match the most recent prompt. Re-check the 4-char code, or /cancel to start over.
```

**Reply (expired):**
```
That token expired (60s limit on /unpair GATE-A1B2C3). Re-issue the command if you still want to run it.
```

**Reply (nothing pending):**
```
Nothing to confirm — no pending action for you (or it already expired).
```

#### Confirm rules

- **One pending action per user.** If you send a second destructive
  command before confirming the first, the first is dropped and you
  get a fresh token for the second.
- **Single use.** A confirmed token can't be replayed.
- **60-second TTL.** Measured against the device's monotonic clock,
  not wall time, so changing the system clock can't extend it.
- **Constant-time compare** on the token — no timing side channel.
- **Same operator only.** The user ID on the `/confirm` must match
  the user ID who issued the original command. Useful when more
  than one person is in the chat: Alice's `/unpair` can't be
  confirmed by Bob with the same token.

---

### `/cancel` — abort a pending confirmation

**Syntax:** `/cancel`

**Reply (something pending):**
```
🛑 Cancelled /unpair GATE-A1B2C3.
```

**Reply (nothing pending):**
```
Nothing pending to cancel.
```

Cheap fallback for "wait, I changed my mind." If you don't send
`/cancel`, the pending action expires on its own after 60 seconds.

---

## Authorization

The auth boundary is the configured `TELEGRAM_CHAT_ID`. Any message
in that chat is accepted; anything outside it is silently dropped.

There is **no** per-user allow-list. The operator owns the chat:

- If you DM the bot 1-on-1, the chat is just you. Only you can issue
  commands.
- If you invite the bot to a group, every member of that group can
  issue every command — including `/open`, `/close`, and
  `/factory_reset`. **Adding the bot to a group is granting that
  group full control over your gates.** Choose group membership
  with that in mind.
- If a third party knows the bot's `@username` and DMs it directly,
  the chat ID won't match the configured one, and their messages
  are dropped before any handler runs. There is no path for
  "outsider adds the bot to their own chat" to drive a real device.

`/pair` is **still DM-only** regardless of how the rest of the chat
is configured. That rule isn't about who's authorized — it's about
which channel the Fernet key transits. A group chat would broadcast
the key to every member at message-receive time, before any
`deleteMessage` fires.

---

## Security

### The token (`TELEGRAM_TOKEN`)

What an attacker with the token can do:

- **Send fake alerts to your chat** (impersonate the bot). Detectable
  if the operator pays attention — fake alerts won't correspond to
  real gate state — but disruptive.
- **Read the chat history visible to the bot.** For an alert-only
  chat, this is whatever you've already received. For a control
  chat where `/pair` happened, it briefly includes the Fernet keys
  (Telegram retains messages for ~48h even after `deleteMessage`).
- **Cannot drive the gates** unless they can also post into the
  configured `TELEGRAM_CHAT_ID`. The command channel rejects
  messages from any other chat. So the token alone is not enough.
- **Cannot read LoRa traffic.** The token gives no path to the gate
  Fernet keys.

Storage:

- **On device:** `/var/lib/base_station/base_config.env`, mode 0600,
  owned by `basesetup`. ext4, where Unix permissions are actually
  enforced.
- **In the build artifact:** none. The token is not baked into the
  golden image; the captive portal writes it on first boot.
- **In the manufacturing inventory:** none. The factory mints
  per-device portal passwords and gate Fernet keys, but the operator
  supplies the Telegram token at setup time.

Rotation: send `/revoke` to BotFather, get a new token, run
`/factory_reset` on the base station and re-enter the new token in
the captive portal. There is no in-place rotation that preserves
state today (it's on the roadmap).

### TLS to `api.telegram.org`

Standard HTTPS. The base station verifies the server certificate
against the system CA bundle that Buildroot installed
(`/etc/ssl/certs/ca-certificates.crt` via `ca-certificates`). This
requires the system clock to be roughly correct, which on a
freshly-booted Pi means waiting for NTP — see
`base_station.py:_wait_for_clock_sync` for how that's guaranteed
before the first send attempt.

The fallback `_force_ntp_sync` speaks UDP/123 directly to bypass a
Buildroot-specific systemd-timesyncd + PrivateTmp interaction —
the timesyncd unit has a drop-in disabling `PrivateTmp` so the
sandbox can read the operator's `/etc/resolv.conf`, and the manual
NTP path is a belt-and-braces fallback. See the file header on
`base_station.py:_force_ntp_sync` for the full explanation.

### Confidentiality on Telegram's side

Telegram has the messages in cleartext on their servers. There is
no end-to-end encryption between the base station and your phone
for bot traffic. (Telegram's "secret chats" are user-to-user only;
bots can't participate.) Anyone with read access to your bot's
chat — including you, anyone you've shared it with, and Telegram
itself — sees all alerts.

For most ranch deployments that's an acceptable trust assumption.
If it isn't for you, swap the Telegram notifier for an
end-to-end-encrypted alternative (Signal CLI, Matrix with E2EE, a
self-hosted ntfy instance). The notifier surface in
`base_station.py` is one class (`TelegramNotifier`); replacing it
is a contained change.

### Physical access to a base station's SD card

- Read `base_config.env` and harvest the Telegram token.
- Read `events.db` and harvest every gate's Fernet key (registered
  gates live in that database).
- This is the strongest argument for either physical security on
  the base station (the easy thing — it's indoors) or full-disk
  encryption with a TPM-backed unlock (the hard thing — Pi has no
  TPM by default).

### Physical access to a gate's SD card

- Read `/var/lib/gate-client/gate_config.env` and harvest that one
  gate's Fernet key.
- The factory provisioner writes the key to `/boot/gate_config.env`
  (FAT32 — the only partition the host laptop can write without
  ext4 tooling), then `gate-config-migrate.service` moves it to
  `/var/lib/gate-client/gate_config.env` (ext4, mode 0600) on first
  boot and deletes the FAT32 source. So an attacker who pulls the
  card off a deployed gate sees ext4, not FAT32.
- The narrow remaining window is a card pulled between flash and
  first boot, while the key is still on FAT32. After first boot,
  the attacker needs ext4 read tools — excludes the casual "plug
  it into Windows and look around" case.

---

## Troubleshooting

### No online ping after boot

Connect to the BaseStation_Setup AP again. If it doesn't appear:

- The device joined Wi-Fi and is online but the Telegram credentials
  are wrong. SSH in (dev image), check
  `journalctl -u base-station -b | grep -i telegram`.
- The device couldn't join Wi-Fi (typo, wrong password, signal too
  weak). It will retry the setup AP automatically after 30 minutes
  of no upstream connectivity via the watchdog. Or you can SSH in
  to a dev image and `nmcli device wifi rescan`.

If the AP did reappear with an error banner, the banner text tells
you what Telegram rejected: typically HTTP 401 (bad token) or HTTP
403 (you never messaged the bot first; see step 3 of bot setup).

### Alert from a known gate doesn't arrive

Run `/status GATE-XXXX` to query the gate directly:

- Gets a reply → gate is alive and the LoRa link works; the issue
  is either the gate sensor wiring or the gate's `last_seq` lagging.
  Pull the gate's SD card and check `journalctl -u gate-client`.
- "did not answer the challenge" → gate is offline, out of LoRa
  range, or the gate radio failed.
- "base-side problem" → the base's LoRa serial port is broken.
  Check wiring; check `journalctl -u base-station -b | grep -i lora`.

### Test the Telegram path manually

From any machine that can reach `api.telegram.org`:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/sendMessage" \
  --data-urlencode "chat_id=<CHAT_ID>" \
  --data-urlencode "text=test"
```

If that arrives in your chat, your `TOKEN` and `CHAT_ID` are
correct. If the base station is still silent, the problem is on the
device (clock, TLS, Wi-Fi, the long-poll thread).

### Inspect what the base station thinks it knows

On a dev image:

```bash
sqlite3 /var/lib/base_station/events.db <<'EOF'
.headers on
SELECT gate_id, name, last_seq, registered_at FROM registered_gates;
SELECT timestamp, gate_id, message FROM gate_events ORDER BY id DESC LIMIT 20;
EOF
```

### Force a state-of-the-world dump

If you can SSH to the device, the most useful single-command
diagnostic is:

```bash
systemctl status base-station base-provision ranch-wifi-watchdog \
  | head -80
journalctl -u base-station -b --no-pager | tail -60
nmcli device status
ip route
date
```

That tells you whether the right service is running, what it last
logged, whether Wi-Fi is up, whether the route to the internet
exists, and whether the clock is sane. 90% of "the bot is silent"
cases resolve at exactly one of those checks.

---

## Related docs

- [USER_GUIDE.md](USER_GUIDE.md) — end-to-end operator journey from
  unboxing to daily use.
- [BUILDING.md](BUILDING.md) — how to build the OS images, flash
  devices, and run the test suite.
- [../tests/README.md](../tests/README.md) — what the unit test
  suite covers and how to add a new test.
