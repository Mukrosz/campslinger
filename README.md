# 🏕️ campslinger

**Campsite monitoring and reservation helpers for Linux.**

Works with any park on the **Aspira / GoingToCamp** booking platform -- BC Parks, Ontario Parks, Parks Canada, and many more across Canada and the USA.

Watch availability via the public API, get notified when sites open up, and optionally automate the Reserve click with Selenium -- from the terminal or a Telegram bot.

> **Platform:** Tested on Debian / Ubuntu Linux. This README does not cover Windows or macOS.

---

## 📑 Contents

| Section | What you get |
|---------|--------------|
| [Supported parks](#-supported-parks) | Platforms and hostnames that work out of the box. |
| [Quick start](#-quick-start) | Clone, install, and run your first monitor in 60 seconds. |
| [campslinger.py](#-campslingerpy--cli) | Unified CLI: monitor-first with optional `--reserve`. |
| [campslinger_tg.py](#-campslinger_tgpy--telegram-bot) | Telegram bot with the same feature set, controlled from your phone. |
| [Chrome setup](#-chrome-setup) | Install Chrome, ChromeDriver, remote debugging -- needed only when `--reserve` is used. |
| [Hold vs finishing your booking](#-hold-vs-finishing-your-booking) | What a successful Reserve click actually does (cart hold, not full checkout). |
| [Archive](#-archive) | Legacy scripts kept for reference. |
| [Policies & disclaimer](#-policies--disclaimer) | Fair use, rate limits, and disclaimer. |

---

## 🌍 Supported parks

All parks using the **Aspira / GoingToCamp** reservation platform share the same API and booking UI. Only the hostname differs.

| Park system | Hostname |
|---|---|
| **BC Parks** (Canada) | `camping.bcparks.ca` |
| **Ontario Parks** (Canada) | `reservations.ontarioparks.ca` |
| **Parks Canada** | `reservation.pc.gc.ca` |
| **Manitoba Parks** (Canada) | `camping.manitobaparks.com` |
| **Nova Scotia Parks** (Canada) | `camping.novascotia.ca` |
| **New Brunswick Parks** (Canada) | `camping.nbbparks.ca` |
| **Newfoundland & Labrador** (Canada) | `camping.nlcamping.ca` |
| **Yukon Parks** (Canada) | `yukon.goingtocamp.com` |
| **Michigan** (USA) | `midnrreservations.com` |
| **Maryland** (USA) | `parkreservations.maryland.gov` |
| **Mississippi** (USA) | `mississippi.goingtocamp.com` |
| **Nebraska** (USA) | `nebraska.goingtocamp.com` |

> Missing a park? If it uses the same `/create-booking/` URL pattern and API, adding the hostname to the allowlist is a one-line change in `campslinger/util.py`.

---

## 🚀 Quick start

```bash
git clone https://github.com/Mukrosz/campslinger.git
cd campslinger
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Monitor any availability (no Chrome needed):

```bash
# BC Parks
python3 campslinger.py --url 'https://camping.bcparks.ca/create-booking/results?...'

# Ontario Parks
python3 campslinger.py --url 'https://reservations.ontarioparks.ca/create-booking/results?...'
```

Sites matching your URL will be printed as they appear. Add `--f S51,S52` to filter, `--i 30` to poll every 30s.

---

## 🖥️ campslinger.py — CLI

### What it does

| Mode | Behaviour |
|------|-----------|
| **Monitor** (default) | Polls the park API for availability. Prints matching sites to the terminal. No browser. |
| **Reserve** (`--reserve`) | Same API polling, but when a target site is free, opens Chrome, finds the site on the map, and clicks **Reserve**. |
| **Warmode** (`--reserve --warmode`) | No API polling. Prefetches the map at 06:59 US/Pacific, clicks Reserve at 07:00. |

### Usage examples

```bash
# Monitor only (default) — no Chrome
python3 campslinger.py --url '...'

# Filter to specific sites, poll every 30s
python3 campslinger.py --url '...' --f S51,S52 --i 30

# Stop after first availability hit
python3 campslinger.py --url '...' --f S51 --loop once

# Reserve on hit (needs Chrome)
python3 campslinger.py --url '...' --f S51 --reserve

# Warmode reserve (07:00 US/Pacific)
python3 campslinger.py --url '...' --f S51 --reserve --warmode

# Visible Chrome window (debugging)
python3 campslinger.py --url '...' --reserve --headed

# Attach to remote Chrome on LAN
python3 campslinger.py --url '...' --reserve --rip 192.168.1.50 --rp 9222

# SMS notification on availability
python3 campslinger.py --url '...' --sms --tsid X --tat X --tn X --mpn X
```

### Flags reference

| Flag | Default | Description |
|------|---------|-------------|
| `--url` / `--u` | *(required)* | Full park create-booking results URL (any supported platform). |
| `--interval` / `--i` | `60` | Seconds between API polls (ignored in warmode). |
| `--jitter` / `--ij` | `10` | Random variance around `--interval` (e.g. 50–70s). |
| `--filter` / `--f` | *(all sites)* | Comma-separated preferred site labels. Order = priority. |
| `--reserve` / `--r` | off | Enable Selenium reservation on hit. |
| `--loop` | `continuous` | `continuous` or `once` (stop after first hit). |
| `--warmode` / `--w` | off | 07:00 US/Pacific timed reserve. Requires `--reserve`. |
| `--debug` / `--d` | off | Extra diagnostics, screenshots on map failures. |
| `--headed` | off | Show Chrome window. Requires `--reserve`. |
| `--timezone` | `US/Pacific` | IANA timezone for warmode. |
| `--rip` / `--remote_ip` | — | Chrome remote debugging host. Requires `--reserve` + `--rp`. |
| `--rp` / `--remote_port` | — | Chrome remote debugging port (e.g. 9222). |
| `--sms` / `--s` | off | SMS on availability (requires Twilio flags below). |
| `--tsid`, `--tat`, `--tn`, `--mpn` | — | Twilio SID, auth token, from-number, your-number. |

### Booking URL

1. Go to your park's reservation site (e.g. [BC Parks](https://camping.bcparks.ca/create-booking/), [Ontario Parks](https://reservations.ontarioparks.ca/create-booking/)).
2. Choose park, dates, equipment, and search.
3. On the **map / results** page, copy the **full** URL from the address bar.

The URL must be `https://` on a [supported park host](#-supported-parks) under `/create-booking/`.

---

## 📱 campslinger_tg.py — Telegram bot

Same features as the CLI, but controlled from Telegram. Monitor is the primary action; Reserve is an optional toggle in the wizard. Works with all supported park platforms.

### Feature matrix

| Feature | Default | How to enable |
|---------|---------|---------------|
| 📡 **Monitor** | Always on | Tap **📡 Monitor** or `/monitor <url>` |
| ⛺ **Reserve** | Off | Toggle in wizard More menu, or `--reserve` |
| 🔄 **Loop** | Continuous | Set in More menu, or `--loop once` |
| 📱 **SMS / Twilio** | Off | Configure in SMS submenu under More |
| 🌅 **Warmode** | Off | Shown when Reserve is on |
| 🐛 **Debug** | Off | Shown when Reserve is on |

### Server operator setup

1. **Python environment** — same `git clone` + `venv` + `pip install` as above.
2. **Chrome** — only needed if users toggle Reserve on (see [Chrome setup](#-chrome-setup)).
3. **Create the bot** — talk to **[@BotFather](https://t.me/BotFather)** in Telegram, create a bot, copy the token.
4. **Allowlist user IDs** — numeric IDs (not @usernames). Use **@userinfobot** to find yours.
5. **Environment variables:**

```bash
export TELEGRAM_BOT_TOKEN='...'
export TELEGRAM_ALLOWED_USER_IDS='11111111,22222222'
# Optional:
export CAMPSLINGER_AUDIT_LOG='/var/log/campslinger_audit.log'
```

6. **Start the bot:**

```bash
cd campslinger && source venv/bin/activate
python3 campslinger_tg.py
```

7. **Optional — remote Chrome** (operator only):

```bash
python3 campslinger_tg.py --rip 192.168.1.50 --rp 9222
```

8. **Optional — systemd:**

```ini
[Service]
WorkingDirectory=/path/to/campslinger
EnvironmentFile=/path/to/campslinger/telegram.env
ExecStart=/path/to/campslinger/venv/bin/python3 /path/to/campslinger/campslinger_tg.py
Restart=on-failure
```

### Process flags

| Flag | Description |
|------|-------------|
| `--max-concurrent N` | Max parallel jobs (default 3). Use 1 with `--rip`/`--rp`. |
| `--no-terminal-log` | Suppress server terminal output. |
| `--rip HOST` | Chrome remote debugging host (same LAN, operator only). |
| `--rp PORT` | Chrome remote debugging port. |

### Telegram user guide

- **`/help`** — commands + quick-action buttons.
- **📡 Monitor** — wizard: paste URL → **Go** (defaults) or **More** (sites, interval, jitter, reserve, loop, SMS, warmode, debug).
- **`/jobs`**, **`/status <id>`**, **`/cancel <id>`** — job management. Each user only sees their own jobs.
- **Plain URL message** — starts a default monitor job (no Reserve).
- Type `/monitor <url> --f S51 --reserve --loop once` for a one-liner.

### Security notes

- Access is controlled by numeric user ID allowlist, not bot username.
- Each user can only see/cancel their own jobs.
- Never share the bot token; revoke in BotFather if leaked.
- Restrict audit log file permissions in production.

---

## 🌐 Chrome setup

> Only needed when `--reserve` is used (CLI or Telegram). Monitor-only mode requires no browser.

### Install Google Chrome (Debian / Ubuntu)

```bash
sudo apt update && sudo apt install -y wget gnupg
wget -q -O - https://dl.google.com/linux/linux_signing_key.pub \
  | sudo gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] \
  http://dl.google.com/linux/chrome/deb/ stable main" \
  | sudo tee /etc/apt/sources.list.d/google-chrome.list
sudo apt update && sudo apt install -y google-chrome-stable
```

### ChromeDriver

**Default (headless):** `webdriver-manager` auto-downloads a matching driver on first run.

**Remote attach (`--rip`/`--rp`):** install `chromedriver` on PATH matching the remote Chrome version:

```bash
CHROME_VERSION=$(google-chrome --version | awk '{print $3}')
cd /tmp
wget -q "https://storage.googleapis.com/chrome-for-testing-public/${CHROME_VERSION}/linux64/chromedriver-linux64.zip"
unzip -o chromedriver-linux64.zip
sudo install -m 755 chromedriver-linux64/chromedriver /usr/local/bin/chromedriver
```

### Remote Chrome (your own logged-in browser)

On the machine with the screen:

```bash
google-chrome \
  --user-data-dir="$HOME/.campslinger-profile" \
  --remote-debugging-port=9222 \
  --no-first-run --no-default-browser-check
```

From the script host:

```bash
python3 campslinger.py --url '...' --reserve --rip 192.168.1.50 --rp 9222
```

**SSH tunnel** if the debug port is localhost-only:

```bash
ssh -N -L 9222:127.0.0.1:9222 you@your-desktop
# then: --rip 127.0.0.1 --rp 9222
```

Do **not** expose the debug port to the public internet.

### Map troubleshooting

1. Add `--debug` (saves HTML + PNG on failure).
2. Try `--headed` on a machine with a display.
3. Try remote Chrome with `--rip`/`--rp`.
4. Ensure Chrome and ChromeDriver major versions match.

---

## 🔒 Hold vs finishing your booking

This applies when **Reserve** is toggled on (not for monitor-only jobs).

**Headless Chrome on the server** — clicking Reserve typically places the site in a **cart / hold** for ~10–15 minutes, not a complete booking. That hold is in an anonymous server-side session you cannot access from your own browser. The site appears unavailable to everyone during the hold.

**Strategy:** be ready on the park's reservation site, signed in, so when the hold expires and the site re-appears, you complete a normal booking before other visitors notice.

**Remote Chrome (`--rip`/`--rp`)** — the script drives *your* browser (your profile, your login), so the hold is in a session you can continue into checkout.

---

## 📦 Archive

The `_archive/` directory contains earlier standalone scripts kept for reference:

| File | Predecessor of |
|------|---------------|
| `_archive/monitor.py` | `campslinger.py` (monitor mode) |
| `_archive/reserve.py` | `campslinger.py --reserve` |
| `_archive/reserve_tg.py` | `campslinger_tg.py` |

These will be removed once the new scripts are fully validated.

---

## ⚖️ Policies & disclaimer

- A successful Reserve click creates a **time-limited hold**, not a completed booking. Finish checkout before it expires.
- Poll at reasonable intervals; aggressive polling can get an IP blocked.
- Park platforms may change at any time.
- **Not affiliated with any park authority or Aspira.** Use at your own risk.
