# Ranch Sentinel — operator's guide

Welcome. This is what to do when a base station and one or more gate
monitors arrive at your door. You'll be done in about 30 minutes and
the rest of your interaction with the system happens through a
Telegram chat on your phone.

This guide assumes:

- You can plug things in.
- You have a Telegram account on your phone.
- You can connect a phone or laptop to a temporary Wi-Fi network and
  type a URL into a browser.

You **don't** need to know what Linux is, what LoRa is, or what
Fernet is. If you're curious about any of that, [README.md](../Readme.md)
in the root of this project explains the architecture.

---

## What's in the box

You should have received:

- **One Base Station.** A small box with a Raspberry Pi 3, a LoRa
  radio, and a power supply. Has a sticker on it.
- **One or more Gate Monitors.** Smaller boxes (Raspberry Pi Zero W
  inside) with their own LoRa radios, magnetic contact sensors, and
  power solutions. Each has its own sticker.

The stickers are important. Hang on to them — you'll type information
off them during setup.

### What's on the stickers

**Base station sticker:**

```
PRINT THIS ON THE PRODUCT STICKER:
  Device ID:    BASE-AB12
  Setup Wi-Fi:  BaseStation_Setup
  Portal URL:   http://10.42.0.1/
  Portal Login: admin / kJ9wF2pQrXmL5zVn
```

**Gate sticker:**

```
PRINT THIS ON THE PRODUCT STICKER:
  Device ID:  GATE-A1B2C3
  Secret Key: gAAAAABl1234567890_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx=
```

You'll use the base station's Portal Login during initial setup, and
each gate's Device ID + Secret Key when you pair that gate over
Telegram. **The secret key is sensitive** — anyone who has it can
both monitor that gate and (eventually) drive its relay. Don't post
the keys publicly; the stickers are meant to stay on the devices.

---

## Step 1 — Create your Telegram bot

You're going to give Telegram a small dedicated "bot" account that
your base station will use to send you alerts and accept your
commands. Bots are free.

1. Open Telegram on your phone.
2. Search for `@BotFather` (the official bot-creation bot) and start
   a chat with it.
3. Send `/newbot`.
4. BotFather will ask for a display name — pick anything human, like
   "Ranch Alerts".
5. BotFather will ask for a username — must end in `bot`, e.g.
   `MyRanchAlertsBot`. If the name's taken, try a variation.
6. BotFather replies with a **token** that looks like
   `123456789:ABCdefghij…` — a long string of letters, digits,
   colons, dashes, and underscores.

**Save the token somewhere private** (your phone's notes app is
fine). Anyone with this token can post messages as your bot, so
treat it like a password. Don't share it; don't put it in a public
chat.

If you ever leak it, send `/revoke` to BotFather and start over.

---

## Step 2 — Get your chat ID

Your base station needs to know **which Telegram chat** to send
alerts to. Every chat has a numeric ID, including your private chat
with your new bot.

1. Open the chat you just created with your bot (search for the
   bot's display name in Telegram).
2. Send it any message — "hi" works. (This step is required for
   reasons explained in the next note.)
3. Open a web browser and visit:

   ```
   https://api.telegram.org/bot<YOUR-TOKEN>/getUpdates
   ```

   substituting the token from step 1 for `<YOUR-TOKEN>`.
4. You'll see a JSON response. Look for `"chat":{"id":12345…}`. That
   number is your **chat ID**. Save it next to the token.

> **Why did I have to say "hi" to my bot first?**
> Bots can only reply to chats that have already messaged them.
> Until you send the bot any message, it has zero permission to
> talk back. The first "hi" unlocks the channel.

### Group chats (optional)

If you want multiple people to receive alerts, you can do that with a
group chat:

1. Create a Telegram group, name it whatever you like.
2. Add your bot as a member of the group.
3. Send any message in the group.
4. Repeat the `getUpdates` URL step above.
5. The group's chat ID is a **negative number** (e.g.
   `-1001234567890`). Keep the leading minus sign.

⚠️ **Anyone in the group can issue every command the bot accepts**,
including opening and closing gates and factory-resetting the base
station. The base station does not distinguish between users in the
chat — the chat itself is the trust boundary. Invite carefully.

---

## Step 3 — Plug in the base station

1. Find a spot inside your house with Wi-Fi coverage and a power
   outlet.
2. Plug the base station's power supply in.
3. Wait about 60 seconds for it to boot.

The base station's first boot does one thing: broadcasts a temporary
Wi-Fi network called **`BaseStation_Setup`** so you can configure it.

> The setup Wi-Fi is **intentionally open** (no password). Don't
> worry — it only exists for the few seconds it takes you to fill
> out the setup form, and the form itself is password-protected.
> Once you submit the form, the setup Wi-Fi disappears and the base
> station joins your real Wi-Fi.

---

## Step 4 — Connect and configure

1. On your phone or laptop, open the Wi-Fi settings and connect to
   **`BaseStation_Setup`**.

   On iOS you may see a "join this network anyway / not secure" prompt
   — that's expected for the open setup AP. Tap "Join Anyway".

2. Open any web browser and go to **http://10.42.0.1/**

   (Some phones will open a captive-portal sheet automatically; if
   yours doesn't, just type the URL.)

3. Your browser will ask for a username and password. Use:

   - **Username:** `admin`
   - **Password:** the long alphanumeric password from the **base
     station's sticker** (the line that says `Portal Login: admin /
     ...`).

4. The setup form appears. Fill it in:

   | Field | What to enter |
   | --- | --- |
   | **Wi-Fi network (SSID)** | The name of your home Wi-Fi. The form pre-populates a dropdown of networks the base station can see. |
   | **Wi-Fi password** | Your home Wi-Fi password. |
   | **Telegram bot token** | The token you got from BotFather in Step 1. |
   | **Telegram chat ID** | The chat ID you found in Step 2. |

5. Submit.

The page will switch to a "Setting up your base station" timeline
that walks you through three checkpoints:

1. The device tears down the setup Wi-Fi.
2. It joins your home Wi-Fi.
3. It sends a test message to your Telegram chat.

When you see the test message in Telegram —

> 📡 Gate Monitor Base Station is Online.

— you're done with the base station. The setup Wi-Fi is gone for
good (until and unless you factory-reset).

### What if the test message never arrives?

If the captive portal returns to a setup-form view with a red banner
at the top, the most common causes are:

- **Bot token mistyped.** Copy/paste from your notes, don't retype.
- **Chat ID mistyped.** Negative numbers must keep the minus sign.
- **You didn't message the bot first.** Re-do step 2's "hi" and
  retry.
- **Wi-Fi password mistyped.** This one is harder to recover from
  via the portal — see [Recovery flows](#recovery-flows) below.

The banner text tells you what Telegram rejected. Common ones:

- `HTTP 401` → bad token.
- `HTTP 403` → bot can't initiate conversation (you never said "hi").
- `HTTP 400` with chat-not-found → bad chat ID.

---

## Step 5 — Pair your first gate

Now that the base station is online and reachable on Telegram,
pairing gates happens through the bot.

1. **From a 1-on-1 DM with your bot** (not a group chat — the gate's
   secret key would leak to every group member), send:

   ```
   /pair GATE-A1B2C3 gAAAAABl12345…== "Front Pasture"
   ```

   Substituting:
   - `GATE-A1B2C3` with the **Device ID** from the gate's sticker.
   - `gAAAAABl12345…==` with the **Secret Key** from the gate's
     sticker (the whole long string, paste it).
   - `"Front Pasture"` with whatever name you want. The name is
     optional but recommended — alerts will read "Gate Front
     Pasture: OPEN" instead of "Gate GATE-A1B2C3: OPEN".

2. The bot will:
   - Delete your `/pair` message from the chat (to scrub the secret
     key from the visible chat history).
   - Reply: `✓ Paired Front Pasture (GATE-A1B2C3).`

3. Plug the gate monitor in at the gate. Once it boots and senses
   its first state change (e.g. the gate opens or closes), you'll
   see the alert in Telegram.

### Repeat for each gate

Send a separate `/pair` command for each gate, with that gate's
sticker info. There's no soft limit on how many gates you can pair.

### A note on the secret keys in chat

The bot deletes your `/pair` line as soon as it sees it. **However,**
Telegram itself keeps a copy of every message in its backups for up
to 48 hours, and other Telegram clients (laptop, web) that already
saw the message may have cached it. If you can't accept that
exposure window:

- Don't pair via Telegram. Re-arm the base station's captive portal
  (run `/factory_reset` and start over) and pair gates through the
  portal instead. That route never sends keys over Telegram.

For most operators the 48-hour window is fine.

---

## Daily use

Once everything's paired, you mostly don't have to touch the bot.
The base station sends alerts when gates open, and you can send a
few commands when you want to inspect or drive things.

### When a gate opens

You'll get a Telegram message like:

> Gate Front Pasture (GATE-A1B2C3): OPEN

That's it. Gate closes are recorded by the device but not sent as
alerts (the design assumption is "you want to know when the gate
opens; closing is the normal state").

### Checking on your gates

Ask the bot to list everything it knows:

```
/status
```

Sample reply:

```
3 gate(s) registered:
  • Front Pasture (GATE-A1B2C3)  last_seq=147  paired 2026-04-12 09:14:33
  • Driveway (GATE-7F4E22)  last_seq=22  paired 2026-05-01 17:02:11
  • Back Pasture (GATE-C8D9E0)  last_seq=88  paired 2026-05-15 12:30:01
```

`last_seq` is the number of messages the base has received from that
gate. If it hasn't changed in days when you'd expect activity, that
gate may be offline.

### Asking a specific gate "are you still there?"

```
/status GATE-A1B2C3
```

This sends a real packet over LoRa and asks the gate for its
current state. Reply:

```
Front Pasture (GATE-A1B2C3): CLOSED (live).
```

If the gate doesn't answer:

```
✗ Front Pasture (GATE-A1B2C3) did not answer the challenge. Is the gate powered on and in LoRa range?
```

Useful for routine "is the gate still healthy?" checks.

### Opening / closing a gate

If your gate hardware has a motorized actuator wired through the
gate monitor's relay:

```
/open GATE-A1B2C3
/close GATE-A1B2C3
```

The bot will reply once the gate confirms the new state, or with a
diagnostic message if something went wrong. See
[TELEGRAM.md](TELEGRAM.md#open--open-a-gate) for the full set of
replies.

> ⚠️ The `/open` and `/close` commands are implemented but have not
> yet been validated against real LoRa hardware. If you're an early
> tester, please report what you see.

### Renaming a gate

If you initially paired a gate as "Front Pasture" and want to
rename it to "North Driveway":

```
/rename GATE-A1B2C3 "North Driveway"
```

This doesn't require a confirmation — it's easy to undo. The new
name will appear on the next alert.

### Removing a gate

If you're decommissioning a gate or starting fresh:

```
/unpair GATE-A1B2C3
```

The bot will ask you to confirm with a 4-character token. You have
60 seconds:

```
/confirm 7f3a
```

The gate's row is deleted from the base station's database; its
event history is kept (in case you ever need to look up "did this
gate open last Tuesday?"). The gate's SD card and physical hardware
are untouched — you can re-pair the same gate any time.

---

## Recovery flows

### "I changed my Wi-Fi password and the base station can't get online."

Two options:

**Option A (passive):** wait 30 minutes. The base station has a
watchdog that detects sustained loss of internet connectivity and
automatically re-arms the `BaseStation_Setup` Wi-Fi. Once it does,
follow Step 4 again to enter your new Wi-Fi credentials. Everything
else (paired gates, Telegram credentials) is preserved.

**Option B (active):** if you have your bot still working (token +
chat ID didn't change), and your bot is in the configured chat,
send `/factory_reset` — but that wipes paired gates too, which you
probably don't want for this case. Option A is the right path.

### "I switched ISPs / my SSID is different now and I want to re-set up the base station."

Send `/factory_reset` to the bot. It'll list what's about to be
wiped (Wi-Fi credentials, Telegram credentials, paired gates, event
history) and ask you to confirm:

```
/confirm 9c12
```

The base station will then reboot the setup Wi-Fi and you can run
Steps 4 and 5 again — re-entering your Wi-Fi, re-creating or
re-using a bot, re-pairing each gate.

### "I'm setting up a brand-new bot."

Same path as the previous: `/factory_reset` and reconfigure. The
portal password on the base station's sticker is preserved across
factory resets, so the captive portal authentication still works
without re-flashing the SD card.

### "I lost the sticker."

If you've lost the base station's sticker, you'll need to re-flash
its SD card with `flash_base_station.py` to mint a fresh portal
password. The factory provisioner will print a new sticker block
for you.

If you've lost a gate's sticker, that gate's Fernet key is gone
forever (factory provisioning is deliberately not backed up). You'll
need to re-flash that gate's SD card too. The hardware itself is
fine; just the credentials need to be re-minted.

### "My base station seems totally stuck."

Power-cycle it. If that doesn't help:

- The watchdog will re-arm the setup Wi-Fi after 30 minutes of no
  internet. Connect to it and look at the captive portal — if
  there's a red banner, the banner text says what failed.
- If that fails too, your last resort is to re-flash the SD card
  with `flash_base_station.py` and start fresh.

---

## What to do if something feels wrong

| Symptom | First check |
| --- | --- |
| No online ping after plugging the base station in for the first time. | Step 4 — was the setup form accepted? Watch for the red banner. |
| Online ping arrives, but a paired gate never sends alerts. | `/status GATE-XXXX` — is the gate responding to live queries? |
| Bot replies "command not understood" or doesn't reply at all. | Are you sending from the same chat the base station was configured against? Try `/help` to verify the bot is processing commands. |
| Bot is missing or seems to have forgotten everything. | The base station may have factory-reset itself or its SD card. Check the device LED; if it's flashing as if for first boot, reconnect to `BaseStation_Setup`. |
| Alert says a gate is open when you know it's closed (or vice versa). | The magnetic contact sensor wiring at the gate is likely loose or reversed. Physical fix; not something the bot can repair. |

For deeper troubleshooting, see
[TELEGRAM.md § Troubleshooting](TELEGRAM.md#troubleshooting).

---

## Security & privacy basics

A few things worth knowing so you can make informed decisions about
how you use the system.

- **Telegram messages are not end-to-end encrypted between you and
  the bot.** Telegram stores them in cleartext on their servers.
  Anyone with read access to your chat — including Telegram itself
  and anyone you've added — sees every alert.
- **The gate Fernet keys briefly transit Telegram during `/pair`.**
  The bot deletes the message immediately, but Telegram's own
  backups keep it for ~48 hours. If you can't accept that exposure,
  pair via the captive portal instead.
- **Anyone in your configured Telegram chat can drive every command,
  including `/factory_reset` and (when implemented) `/open` and
  `/close`.** Choose chat membership carefully. The base station
  does not distinguish between users.
- **The base station's SD card holds every gate's Fernet key.** If
  someone walks off with the base station, they can decrypt every
  gate's traffic and forge alerts. Keep the base station physically
  secure.
- **A gate's SD card holds only that one gate's key.** Stolen from
  the gate, it can be used to forge alerts from that gate or
  (eventually) drive its relay. Standard physical-security tradeoffs
  for an outdoor enclosure.

If your threat model needs stronger guarantees, see
[TELEGRAM.md § Security](TELEGRAM.md#security) and consider
swapping Telegram for an end-to-end-encrypted notifier — the
notifier surface is a single Python class.

---

## When you want to know more

- **[TELEGRAM.md](TELEGRAM.md)** — every command, with all the
  expected replies. The reference manual for what to type when.
- **[../Readme.md](../Readme.md)** — the project overview,
  architecture diagram, repo layout. Read this if you're curious
  about how the system works under the hood.
- **[BUILDING.md](BUILDING.md)** — for building, flashing, and
  testing the OS yourself. Not needed if you received flashed
  devices.

Welcome to the system.
