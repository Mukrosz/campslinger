# campslinger

Small helpers for **[BC Parks frontcountry booking](https://camping.bcparks.ca/create-booking/)** on **Linux** (Debian-style): watch availability, optionally drive Chrome to click **Reserve**, or run a **Telegram bot** so allowed users can start jobs from a phone (browser still on the server).

**Platform:** Tested on Debian Linux (e.g. Debian 12). This README does not cover Windows or macOS.

---

## Main contents

| Section | What you get |
|---------|----------------|
| [Shared (all scripts)](#shared-all-scripts) | Getting started: [clone and Python environment](#one-time-setup-clone-venv-dependencies), [your booking link](#booking-url-used-by-every-script), and an [important note on cart holds](#hold-vs-finishing-your-booking) for the reserve tools. |
| [monitor.py](#monitorpy) | **Watch availability only** - uses the park site’s public data in the background; **no browser window** from the script. Optional text alerts (Twilio). |
| [reserve.py](#reservepy) | The main **“try to Reserve”** script: watches availability, then drives **Chrome** to open the map and click **Reserve** when a site you want is free. You still finish checkout on the real website (see [holds](#hold-vs-finishing-your-booking)). |
| [reserve_tg.py](#reserve_tgpy) | Same reservation logic as `reserve.py`, plus a **Telegram bot** so allowlisted users can start jobs from a phone. Chrome always runs on the **Linux server** where the bot runs - not on your handset. |
| [Policies and disclaimer](#policies-and-disclaimer) | Fair use, checkout deadlines, and “not affiliated with BC Parks.” |

Jump to a script, follow its **Section contents** links, then setup and usage there.

---

## Shared (all scripts)

### Shared: section contents

- [What ships in the repo](#what-ships-in-the-repo)
- [One-time setup: clone, venv, dependencies](#one-time-setup-clone-venv-dependencies)
- [Booking URL (used by every script)](#booking-url-used-by-every-script)
- [Hold vs finishing your booking](#hold-vs-finishing-your-booking)

### What ships in the repo

| File | What it’s for |
|------|----------------|
| `monitor.py` | Checks **availability only**. Uses the park website’s public data in the background - **no separate browser window** opened by the script. Optional text-message alerts (Twilio). |
| `reserve.py` | The main **“try to Reserve”** script: it can watch availability and drive an automated **Chrome** session to click the map and **Reserve** when conditions match. This is the **CLI** tool most people use on a server or desktop. |
| `reserve_tg.py` | **Telegram-only** entrypoint: same reservation logic as `reserve.py`, plus a **Telegram bot** so allowlisted users can start and monitor jobs from a phone. Chrome still runs on the **Linux machine** where the bot process runs. |
| `requirements.txt` | Python dependencies: `pip install -r requirements.txt`. |

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

### Hold vs finishing your booking

This matters for **`reserve.py`** and **`reserve_tg.py`** (not for `monitor.py`, which never clicks **Reserve**).

**Default automation (headless Chrome on the server, or `reserve_tg.py` - remote attach is not available there)**  
When the script clicks **Reserve**, it is usually **putting the site in a cart / on hold** for a limited time (often on the order of **10 - 15 minutes**), not completing your whole booking for you. During that hold, the site typically **shows as unavailable to everyone**, **including you**, until the hold expires or someone completes checkout in **that** browser session.

**Why “including you”?** Selenium is driving **its own** Chrome instance - typically a **clean or server-side profile**, not the browser window where **you** are signed in on your laptop or phone. The hold lives in **that automated session**; you have **no way to open that same cart or continue checkout** in your normal browser, so for you the site is blocked just like for any other visitor. That is the practical difference from **`--rip` / `--rp`**, where the script attaches to **your** Chrome (your profile, your logins), so the hold is in a session **you** can actually use.

So in practice the script is often **snagging or tagging** a site so it briefly leaves the pool; the **predictable** hold window is something you can plan around: be ready on the **real BC Parks site** in **your** browser (signed in, payment flow in mind) so that when the hold ends and the site becomes bookable again, **you** can complete a normal reservation - while other campers who were not watching may still think the site is simply “taken.”

**When you attach your own signed-in Chrome (`--rip` / `--rp` on `reserve.py` only)**  
If you use **your** browser profile (already logged in), you may be able to continue into checkout in that same session - because the automation and the hold are no longer trapped in an anonymous server-side browser you cannot see. This path does **not** apply to `reserve_tg.py`, which always uses server-side headless Chrome.

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

- Complete [One-time setup: clone, venv, dependencies](#one-time-setup-clone-venv-dependencies) (virtualenv and `pip install -r requirements.txt`).
- No Chrome required.

### monitor.py: installation / setup

[Activate the virtualenv](#one-time-setup-clone-venv-dependencies) (`source venv/bin/activate` from the repo root). No extra services.

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

Read **[Hold vs finishing your booking](#hold-vs-finishing-your-booking)** for what a successful **Reserve** click usually means (cart hold vs finishing checkout), especially if you are **not** using `--rip` / `--rp`.

| Mode | Behaviour |
|------|-----------|
| **Normal (default)** | Every so often, the script checks **which site numbers are free** via the API. When one you care about is free, it opens Chrome, finds that site on the **map**, and clicks **Reserve** so it lands in the cart/hold - **you still complete checkout on the website** (and may time your visit around the hold window; see [Shared](#hold-vs-finishing-your-booking)). |
| **Warmode (`--warmode`)** | For the “opens at 7 a.m. Pacific” style window: about a minute before 7 it loads the map and prepares **Reserve**, then clicks at 7. See **[BC Parks - frontcountry camping](https://bcparks.ca/reservations/frontcountry-camping/)** for official rules. |

**Why two steps?** In normal mode, the quick API check avoids loading the full map on every poll; the script only uses the heavy map flow when it is time to click.

### reserve.py: prerequisites

- Complete [One-time setup: clone, venv, dependencies](#one-time-setup-clone-venv-dependencies).
- **Google Chrome** on the machine where the browser runs (install steps under [Chrome: headless, visible window, or remote attach](#reservepy-chrome-headless-visible-window-or-remote-attach)).

### reserve.py: installation / setup

1. Complete [One-time setup: clone, venv, dependencies](#one-time-setup-clone-venv-dependencies).
2. Install Chrome on that host (Debian/Ubuntu example under [Chrome: headless, visible window, or remote attach](#reservepy-chrome-headless-visible-window-or-remote-attach)).
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

**Default local mode:** `reserve.py` uses **`ChromeDriverManager().install()`** - downloads a driver into a cache (often `~/.wdm`). It does **not** install Chrome.

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

`reserve_tg.py` is **only** the long-polling **Telegram bot**. It does **not** offer a standalone “run once from CLI with `--url`” mode; use **[`reserve.py`](#reservepy)** for that.

The **browser runs on the Linux host** where you start `reserve_tg.py` (typically a server), not on the phone. There is **no** `--rip` / `--rp` here - automation always uses **headless Chrome on the server**, so the **[hold / snag / cart behaviour](#hold-vs-finishing-your-booking)** described in Shared applies the same way as for default `reserve.py`.

Allowed users send commands and URLs in chat; the bot starts **jobs** (with a concurrency limit), sends deduplicated status lines to Telegram, and can attach **Cancel / Status / Jobs** buttons to the job-start message.

### reserve_tg.py: server operator: full setup

1. **Python environment**  
   Use the same [One-time setup: clone, venv, dependencies](#one-time-setup-clone-venv-dependencies) as the other scripts (`git clone`, `venv`, `pip install -r requirements.txt`).

2. **Google Chrome on the server**  
   Install Chrome the same way as for `reserve.py` ([Chrome: headless, visible window, or remote attach](#reservepy-chrome-headless-visible-window-or-remote-attach) - use the **Debian/Ubuntu** install block; the bot itself runs **headless** only and does not support `--headed` / `--rip` / `--rp`).

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

6. **Run the bot** (from the repo directory, with the [virtualenv activated](#one-time-setup-clone-venv-dependencies)):

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

**Your jobs only:** `/jobs`, `/status`, `/cancel`, and the **Cancel** / **Status** buttons only list or act on jobs **you** started (private chats; each user’s job ids are isolated from other allowlisted users). The server still enforces a single global `--max-concurrent` pool across everyone.

**Help:** send **`/help`**. You should see **Campslinger Telegram commands** and a **Quick actions** keyboard (`/jobs`, `/status`, `/cancel`, `/reserve`, `/help`).

**Quick action buttons**

- **`/jobs`**, **`/help`**: run immediately.  
- **`/status`** / **`/cancel`**: the bot asks for the **job id**; reply with that id (e.g. from `/jobs` or from a previous message).  
- **`/reserve`**: guided flow - enter the **booking URL** first, then **Go** (URL + sensible defaults) or **More** (step through options with a live preview of the `/reserve …` command). You can still type a full **`/reserve https://… --f S51 --i 60`** manually if you prefer.

**Plain URL message:** a message that is **only** your `https://camping.bcparks.ca/create-booking/...` URL starts a default normal-mode job (same idea as a quick **Go**).

**After a job starts:** the bot sends **Cancel**, **Status**, and **Jobs** buttons for that job so you can run `/cancel <id>` and `/status <id>` without typing.

**While a job runs:** status updates are **deduplicated** where possible; important events and changes still go to chat. Routine “still waiting” style messages can include the poll interval and job id for reference.

**URL rules:** must be `https://` on **`camping.bcparks.ca`** under **`/create-booking/`** (enforced to limit abuse).

Use **`/reserve@YourBot`** if Telegram inserts the bot name; that form is supported.

### reserve_tg.py: park name in messages

When the API returns a resolvable park name, log lines mirrored to Telegram are prefixed with **`[park]`** so you can confirm the correct park.

### reserve_tg.py: security notes

- Safety is the **numeric allowlist**, not hiding the bot username.  
- Job control is **per Telegram user id**: you cannot see or cancel another person’s jobs (private chat use is assumed).  
- Never share the **bot token**; revoke in BotFather if leaked.  
- Audit log path should be restricted on disk in production.

---

## Policies and disclaimer

- A successful **Reserve** click usually creates a **time-limited hold** in the cart; finish checkout on the real site before it expires. See **[Hold vs finishing your booking](#hold-vs-finishing-your-booking)** for how that behaves when you are **not** using your own signed-in Chrome (`--rip` / `--rp` on `reserve.py` only).  
- Poll at reasonable intervals; aggressive polling can get an IP blocked.  
- BC Parks may change the site at any time. **Not affiliated with BC Parks.** Use at your own risk.
