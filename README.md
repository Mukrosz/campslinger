# campslinger

Small helpers for **[BC Parks frontcountry booking](https://camping.bcparks.ca/create-booking/)** on **Linux** (Debian-style): watch availability, optionally drive Chrome to click **Reserve**, or run a **Telegram bot** so allowed users can start jobs from a phone (browser still on the server).

**Platform:** Tested on Debian Linux (e.g. Debian 12). This README does not cover Windows or macOS.

---

## Main contents

| Section | What you get |
|---------|----------------|
| [Shared (all scripts)](#shared-all-scripts) | Clone, virtualenv, packages, booking URL |
| [monitor.py](#monitorpy) | Poll the public API for availability only (no browser) |
| [reserve.py](#reservepy) | API + Chrome to reserve when sites match |
| [reserve_tg.py](#reserve_tgpy) | Telegram bot around the same automation as `reserve.py` |
| [Policies and disclaimer](#policies-and-disclaimer) | Holds, rate limits, not affiliated |

Jump to a script, follow its **Section contents** links, then setup and usage there.

---

## Shared (all scripts)

### Shared: section contents

- [What ships in the repo](#what-ships-in-the-repo)
- [One-time setup: clone, venv, dependencies](#one-time-setup-clone-venv-dependencies)

### What ships in the repo

| File | Role |
|------|------|
| `monitor.py` | Availability checks via the site’s JSON API; optional Twilio SMS. |
| `reserve.py` | Watches availability and uses **Chrome** to open the map and click **Reserve** when filters match. |
| `reserve_tg.py` | **Telegram-only** process: same reservation engine as `reserve.py`, controlled from chat. |
| `requirements.txt` | `pip install -r requirements.txt` |

Inline help:

```bash
python3 monitor.py --help
python3 reserve.py --help
python3 reserve_tg.py --help
```

### One-time setup: clone, venv, dependencies

You need **Python 3.10+** and a terminal.

```bash
git clone https://github.com/Mukrosz/campslinger.git
cd campslinger
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

The `venv/` directory stays local and is not committed.

### Booking URL (used by every script)

1. Open **[Create booking](https://camping.bcparks.ca/create-booking/)**, choose park, dates, equipment, search.
2. On the **map / results** page, copy the **full** address bar URL.

Example shape (yours will differ):

```text
https://camping.bcparks.ca/create-booking/results?resourceLocationId=...&mapId=...&startDate=...&endDate=...&...
```

Pass it as `--url` / `--u` (or paste/send it in Telegram for the bot).

---

## monitor.py

### monitor.py: section contents

- [What it does](#monitorpy-what-it-does)
- [Prerequisites](#monitorpy-prerequisites)
- [Installation / setup](#monitorpy-installation--setup)
- [Usage](#monitorpy-usage)
- [Optional SMS (Twilio)](#monitorpy-optional-sms-twilio)

### monitor.py: what it does

You give it the same long results URL as the other tools. It repeatedly queries the booking system’s **public API** and prints which sites are available. It **does not** open a browser or click the map.

### monitor.py: prerequisites

- Shared [venv and dependencies](#one-time-setup-clone-venv-dependencies).
- No Chrome required.

### monitor.py: installation / setup

Activate the venv (see shared setup). No extra services.

### monitor.py: usage

```bash
./monitor.py --url 'https://camping.bcparks.ca/create-booking/...'
./monitor.py --url '...' --f 'S51,S52' --i 30
```

`--f` limits which site labels you care about; `--i` is the poll interval in seconds (default 60).

### monitor.py: optional SMS (Twilio)

Requires a Twilio account and the `twilio` package (included in `requirements.txt` if you install the full file).

```bash
./monitor.py --url '...' --f 'S51' --sms --i 60 \
  --twilio_sid … --twilio_auth_token … --twilio_number … --my_phone_number …
```

---

## reserve.py

### reserve.py: section contents

- [What it does](#reservepy-what-it-does)
- [Prerequisites](#reservepy-prerequisites)
- [Installation / setup](#reservepy-installation--setup)
- [Usage](#reservepy-usage)
- [Chrome: headless, visible window, or remote attach](#reservepy-chrome-headless-visible-window-or-remote-attach)
- [Map troubleshooting](#reservepy-map-troubleshooting)

### reserve.py: what it does

| Mode | Behaviour |
|------|-----------|
| **Normal (default)** | On an interval, checks which sites are free via the API. When one you want is free, launches Chrome, finds it on the map, clicks **Reserve** so it lands in the cart/hold. You still complete checkout on the website. |
| **Warmode (`--warmode`)** | For “opens at 7 a.m. Pacific” style windows: about a minute before 7 it loads the map and prepares **Reserve**, then clicks at 7. See **[BC Parks — frontcountry camping](https://bcparks.ca/reservations/frontcountry-camping/)** for official rules. |

### reserve.py: prerequisites

- Shared [venv and dependencies](#one-time-setup-clone-venv-dependencies).
- **Google Chrome** installed on the machine where the browser runs (see Chrome section below).

### reserve.py: installation / setup

1. Complete [shared setup](#one-time-setup-clone-venv-dependencies).
2. Install Chrome on that host (Debian/Ubuntu example under [Chrome](#reservepy-chrome-headless-visible-window-or-remote-attach)).
3. In default (non-remote) mode, **`webdriver-manager`** usually downloads a matching **ChromeDriver** on first run; you still must install **Chrome** yourself.

### reserve.py: usage

**Normal mode:**

```bash
./reserve.py --url 'https://camping.bcparks.ca/create-booking/...'
./reserve.py --url '...' --f 'S51,S52' --i 60
```

`--f` is comma-separated; **order matters** (first matching free site is tried first).

**Warmode:**

```bash
./reserve.py --url '...' --f 'S51' --warmode
```

**Useful flags:**

| Flag | Meaning |
|------|--------|
| `--headed` | Show a Chrome window (machine with a display; debugging). |
| `--debug` | Extra logging; on map failure may write `reserve_map_failure.html` / `.png` in the cwd. |
| `--rip` / `--rp` | Attach to your own Chrome with remote debugging (see below). |

Optional Twilio SMS uses the same pattern as `monitor.py` (`--sms` and Twilio arguments).

### reserve.py: Chrome: headless, visible window, or remote attach

| Situation | What to use |
|-----------|-------------|
| Default on a server | **Headless** Chrome; **`webdriver-manager`** usually downloads ChromeDriver on first run. **Chrome** must still be installed. |
| You want to see the browser | `--headed` on a desktop session. |
| Chrome on another machine (e.g. logged-in profile) | `--rip` and `--rp` to that Chrome’s remote debugging port (often with an SSH tunnel). |

Headless still runs the **`google-chrome`** binary with headless flags.

#### Install Google Chrome (Debian / Ubuntu)

```bash
sudo apt update
sudo apt install -y wget gnupg
wget -q -O - https://dl.google.com/linux/linux_signing_key.pub \
  | sudo gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
  | sudo tee /etc/apt/sources.list.d/google-chrome.list
sudo apt update
sudo apt install -y google-chrome-stable
google-chrome --version
```

#### ChromeDriver

**Default local mode:** `reserve.py` uses **`ChromeDriverManager().install()`** — downloads a driver into a cache (often `~/.wdm`). It does **not** install Chrome.

**`--rip` / `--rp`:** the machine that **runs** `reserve.py` needs **`chromedriver` on `PATH`**, version matching the Chrome you attached to. Example install matching Chrome’s version:

```bash
CHROME_VERSION=$(google-chrome --version | awk '{print $3}')
cd /tmp
wget -q "https://storage.googleapis.com/chrome-for-testing-public/${CHROME_VERSION}/linux64/chromedriver-linux64.zip"
unzip -o chromedriver-linux64.zip
sudo install -m 755 chromedriver-linux64/chromedriver /usr/local/bin/chromedriver
chromedriver --version
```

#### Remote debugging (your own Chrome)

On the machine with the screen:

```bash
google-chrome \
  --user-data-dir="$HOME/.bcparks-profile" \
  --remote-debugging-port=9222 \
  --no-first-run \
  --no-default-browser-check
```

From the script host (same PC: `127.0.0.1`; over network or via tunnel: see your setup):

```bash
./reserve.py --url '...' --f 'S51' --rip 192.168.1.50 --rp 9222
```

**SSH tunnel** when the debug port is localhost-only on the desktop:

```bash
ssh -N -L 9222:127.0.0.1:9222 you@your-desktop
# then: --rip 127.0.0.1 --rp 9222
```

Do not expose the debug port to the public internet.

### reserve.py: map troubleshooting

1. Try **`--debug`** (artifacts on failure).
2. Try **`--headed`** on a machine with a display.
3. Try **remote Chrome** with `--rip` / `--rp`.
4. Match Chrome and ChromeDriver versions when using remote attach.

---

## reserve_tg.py

### reserve_tg.py: section contents

- [What it does](#reserve_tgpy-what-it-does)
- [Server operator: full setup](#reserve_tgpy-server-operator-full-setup)
- [Process flags](#reserve_tgpy-process-flags)
- [Telegram user guide](#reserve_tgpy-telegram-user-guide)
- [Park name in messages](#reserve_tgpy-park-name-in-messages)
- [Security notes](#reserve_tgpy-security-notes)

### reserve_tg.py: what it does

`reserve_tg.py` is **only** the long-polling **Telegram bot**. It does **not** offer a standalone “run once from CLI with `--url`” mode; use **`reserve.py`** for that.

The **browser runs on the Linux host** where you start `reserve_tg.py` (typically a server), not on the phone. Allowed users send commands and URLs in chat; the bot starts **jobs** (with a concurrency limit), sends deduplicated status lines to Telegram, and can attach **Cancel / Status / Jobs** buttons to the job-start message.

### reserve_tg.py: server operator: full setup

1. **Python environment**  
   Use the same [clone + venv + `pip install -r requirements.txt`](#one-time-setup-clone-venv-dependencies) as the other scripts.

2. **Google Chrome on the server**  
   Install Chrome the same way as for `reserve.py` ([Chrome install](#reservepy-chrome-headless-visible-window-or-remote-attach)). The bot uses **headless** automation only (no `--headed` / `--rip` / `--rp` in this script).

3. **Create the bot in Telegram**  
   - Open Telegram, talk to **[@BotFather](https://t.me/BotFather)**.  
   - Create a new bot, choose a name and username.  
   - Copy the **HTTP API token** BotFather gives you. **Never** commit it or post it publicly.

4. **Allowlisted user IDs**  
   The bot only accepts commands from numeric IDs listed in `TELEGRAM_ALLOWED_USER_IDS`. These are **not** `@usernames`. Users can discover their ID with bots like **@userinfobot**.

5. **Environment variables** (required):

```bash
export TELEGRAM_BOT_TOKEN='...'                    # from BotFather
export TELEGRAM_ALLOWED_USER_IDS='11111111,22222222'   # comma-separated integers
```

Optional:

```bash
export CAMPSLINGER_AUDIT_LOG='/var/log/campslinger_audit.log'
# JSON lines: user_id, action, job_id, booking URL, etc.
# Default if unset: ./campslinger_telegram_audit.log
```

The script reads **only the process environment**; it does not load `.env` files unless your shell or systemd does (e.g. `set -a; source .env.local`).

6. **Run the bot**

```bash
cd campslinger && source venv/bin/activate
python3 reserve_tg.py
```

Keep it running with **systemd**, **tmux**, or similar. If the process exits, the bot stops.

7. **Optional: systemd sketch**

```ini
[Service]
WorkingDirectory=/path/to/campslinger
EnvironmentFile=/path/to/campslinger/telegram.env
ExecStart=/path/to/campslinger/venv/bin/python3 /path/to/campslinger/reserve_tg.py
Restart=on-failure
```

Put `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_USER_IDS` in `telegram.env` with `0600` permissions.

### reserve_tg.py: process flags

| Flag | Meaning |
|------|--------|
| `--max-concurrent N` | Max parallel reservation jobs (default **3**). |
| `--no-terminal-log` | Stop printing per-job lines on the **server** terminal; Telegram logging unchanged. |

### reserve_tg.py: Telegram user guide

**Who can use it:** only user IDs on the operator’s allowlist. Others see **Unauthorized**.

**Help:** send **`/help`**. You should see **Campslinger Telegram commands** and a **Quick actions** keyboard (`/jobs`, `/status`, `/cancel`, `/reserve`, `/help`).

**Quick action buttons**

- **`/jobs`**, **`/help`**: run immediately.  
- **`/status`** / **`/cancel`**: the bot asks for the **job id**; reply with that id (e.g. from `/jobs` or from a previous message).  
- **`/reserve`**: guided flow — enter the **booking URL** first, then **Go** (URL + sensible defaults) or **More** (step through options with a live preview of the `/reserve …` command). You can still type a full **`/reserve https://… --f S51 --i 60`** manually if you prefer.

**Plain URL message:** a message that is **only** your `https://camping.bcparks.ca/create-booking/...` URL starts a default normal-mode job (same idea as a quick **Go**).

**After a job starts:** the bot sends **Cancel**, **Status**, and **Jobs** buttons for that job so you can run `/cancel <id>` and `/status <id>` without typing.

**While a job runs:** status updates are **deduplicated** where possible; important events and changes still go to chat. Routine “still waiting” style messages can include the poll interval and job id for reference.

**URL rules:** must be `https://` on **`camping.bcparks.ca`** under **`/create-booking/`** (enforced to limit abuse).

Use **`/reserve@YourBot`** if Telegram inserts the bot name; that form is supported.

### reserve_tg.py: park name in messages

When the API returns a resolvable park name, log lines mirrored to Telegram are prefixed with **`[park]`** so you can confirm the correct park.

### reserve_tg.py: security notes

- Safety is the **numeric allowlist**, not hiding the bot username.  
- Never share the **bot token**; revoke in BotFather if leaked.  
- Audit log path should be restricted on disk in production.

---

## Policies and disclaimer

- A successful **Reserve** usually creates a **time-limited hold**; finish checkout on the real site before it expires.  
- Poll at reasonable intervals; aggressive polling can get an IP blocked.  
- BC Parks may change the site at any time. **Not affiliated with BC Parks.** Use at your own risk.
