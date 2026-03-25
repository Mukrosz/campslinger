# campslinger

**What this is**

This repository is a small helper project for people who book frontcountry camping on **[BC Parks’ online reservation site](https://camping.bcparks.ca/create-booking/)**. It was built for personal use on **Linux** (Debian-style systems):

- **Watch** whether campsites are free (without clicking around the map every minute).
- **Optionally try to put a site in your cart** (“Reserve”) when something matches what you want - then you finish payment and details yourself in the normal website checkout.

You do **not** need to have written any of this code to use it. Basic comfort with a terminal (running commands), copying a URL from your browser, and installing Python helps.

**Platform:** Tested on **Debian Linux** (e.g. Debian 12). This README does not cover Windows or macOS.

---

## Contents

- [Repository layout](#repository-layout)
- [What each script does (plain English)](#what-each-script-does-plain-english)
- [Setup](#setup)
- [Getting your booking link from the website](#getting-your-booking-link-from-the-website)
- [Usage](#usage)
- [Chrome: invisible browser, visible window, or your own Chrome](#chrome-invisible-browser-visible-window-or-your-own-chrome)
- [Policies and disclaimer](#policies-and-disclaimer)

---

## Repository layout

| File | What it’s for |
|------|----------------|
| `monitor_site_api.py` | Checks availability only. Uses the park website’s public data in the background - **no separate browser window** opened by the script. Optional text-message alerts (Twilio). |
| `reserve_site.py` | The main “try to Reserve” script: it can watch availability and drive an automated **Chrome** session to click the map and **Reserve** when conditions match. This is the script that has been **used and tested** here. |
| [`reserve_site_v2.py`](#reserve_site_v2py-experimental--telegram) | **Work in progress.** Same idea as `reserve_site.py`, plus an optional **Telegram bot** so you could start jobs from your phone. **Not fully tested yet** - expect rough edges; prefer `reserve_site.py` for anything important. |
| `requirements.txt` | List of Python packages to install (`pip install -r requirements.txt`). |

For every command-line option and built-in examples:

- `python3 reserve_site.py --help`
- `python3 reserve_site_v2.py --help`

---

## What each script does (plain English)

### `monitor_site_api.py`

You give it the **same long URL** you see on the BC Parks results/map page after you search. The script asks the booking system, in the background, which sites are available and prints a simple answer (and can text you if you set up Twilio). It **does not** open a map or click anything.

### `reserve_site.py`

Two behaviors:

| Mode | In simple terms |
|------|------------------|
| **Normal (default)** | Every so often (e.g. every 60 seconds), the script quietly checks **which site numbers are free**. When one you care about is free, it opens an automated Chrome, finds that site on the **map** (green “available” marker), and clicks **Reserve** so the site lands in the cart/hold - **you still complete checkout on the website.** |
| **Warmode (`--warmode`)** | For the “opens at 7 a.m. Pacific” style window: about a minute before 7, it loads the map and prepares the **Reserve** step, then clicks at 7. No background “availability check” loop for that mode - see BC Parks’ own rules for how far ahead you can book. |

**Why two steps?** In normal mode, the quick background check saves constantly loading the full map; the script only uses the full map when it’s time to click. Official booking rules (how far ahead, 7 a.m. Pacific, etc.) are on **[BC Parks  -  Frontcountry camping](https://bcparks.ca/reservations/frontcountry-camping/)**.

---

## Setup

You need **Python 3.10 or newer** and a terminal on your Linux machine.

```bash
git clone https://github.com/Mukrosz/campslinger.git
cd campslinger
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

The `venv/` folder is only on your computer and is **not** part of the GitHub repo.

---

## Getting your booking link from the website

1. Go to **[Create booking](https://camping.bcparks.ca/create-booking/)**, pick park, dates, equipment, and run a search.
2. When you see the **map / results** page, copy the **entire** address bar URL. The scripts need the long URL that includes things like dates and map identifiers.

Example shape (your link will be different):

```text
https://camping.bcparks.ca/create-booking/results?resourceLocationId=...&mapId=...&startDate=2025-08-18&endDate=2025-08-25&...
```

Pass that string to the scripts as `--url` or `--u`.

---

## Usage

### `monitor_site_api.py`

```bash
./monitor_site_api.py --url 'https://camping.bcparks.ca/create-booking/...'
./monitor_site_api.py --url '...' --f 'S51,S52' --i 30
```

Optional SMS (Twilio account required):

```bash
./monitor_site_api.py --url '...' --f 'S51' --sms --i 60 \
  --twilio_sid … --twilio_auth_token … --twilio_number … --my_phone_number …
```

### `reserve_site.py`

**Normal mode** (check in the background, then try Reserve when something matches):

```bash
./reserve_site.py --url 'https://camping.bcparks.ca/create-booking/...'
./reserve_site.py --url '...' --f 'S51,S52' --i 60
```

- `--f` is a comma-separated list of site labels you prefer. **Order matters:** the script tries your first choice that is actually free, then the next, etc.

**Warmode (7 a.m. Pacific–style timing):**

```bash
./reserve_site.py --url '...' --f 'S51' --warmode
```

**Useful options:**

| Flag | Meaning |
|------|--------|
| `--headed` | Show a real Chrome window (only useful on a machine with a screen; good for troubleshooting). |
| `--debug` | Extra logging; if the map fails to load, the script can save `reserve_map_failure.html` and `reserve_map_failure.png` in the folder where you ran the command. |

---

### `reserve_site_v2.py` (experimental + Telegram)

**Status: work in progress.** This file adds an optional **Telegram bot** so you could start runs from chat. It is **not** fully tested or “production ready” yet. For real trips, use **`reserve_site.py`** until you’ve verified v2 yourself.

Planned idea (when stable):

- Run the bot on your server with `--telegram-bot`.
- Allowed Telegram users send `/reserve` with the same URL and options you’d use on the command line, plus `/jobs`, `/status`, `/cancel`.

Environment variables (for when you experiment):

```bash
export TELEGRAM_BOT_TOKEN='123456:ABC...'
export TELEGRAM_ALLOWED_USER_IDS='11111111,22222222'
```

Proposed bot startup (experimental):

```bash
./reserve_site_v2.py --telegram-bot --max-concurrent 3
```

Example chat commands (for testing only):

```text
/reserve https://camping.bcparks.ca/create-booking/results?... --f 29,11 --i 60 --debug
/reserve https://... --warmode --f S51 --timezone US/Pacific
/jobs
/status ab12cd34
/cancel ab12cd34
```

---

## Chrome: invisible browser, visible window, or your own Chrome

`reserve_site.py` (and v2 in CLI mode) can use Chrome in three ways:

| Situation | What to use |
|-----------|-------------|
| Default on a server | **Headless** Chrome - the script runs a browser you don’t see; **`webdriver-manager`** usually **downloads** a matching **ChromeDriver** on first run (see below). **Google Chrome itself** is not installed by the script or `pip`. |
| You want to *see* what’s happening | `--headed` (only on a computer with a normal desktop session). |
| The script runs on a **server** but you want to use **your own Chrome** on another computer (e.g. already logged in) | `--rip` and `--rp` point at that Chrome’s “remote debugging” port. |

You need **Google Chrome** installed where the browser actually runs. Headless mode still launches that same **`google-chrome`** binary with a headless flag - it is not a separate “headless-only” package.

### Chrome and ChromeDriver on the script machine (typical headless server)

These steps assume a **64-bit x86 (amd64)** Linux host. Bare servers often ship without Chrome - install the browser first, then wire the driver.

#### 1. Install Google Chrome (Debian / Ubuntu)

Using Google’s APT repository:

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

#### 2. Is ChromeDriver auto-installed?

**Partially, and only in default mode.** When you **do not** pass `--rip` / `--rp`, `reserve_site.py` calls **`ChromeDriverManager().install()`** from the **`webdriver-manager`** package (see `requirements.txt`). That **downloads** a ChromeDriver build into a cache (commonly under **`~/.wdm`**) and passes it to Selenium - it does **not** run `apt install chromedriver`, and it does **not** install Chrome.

You can skip **manual** ChromeDriver setup on the script host **if** default mode works and the download succeeds. You may still need a **manual** driver if you use **remote attach** (`--rip` / `--rp`), an older copy of the script **without** `webdriver-manager`, or **`webdriver-manager` fails** (no HTTPS to Google/storage, disk permissions, proxy, or unusual version detection).

**Remote mode always needs `chromedriver` on `PATH`** on the machine that runs the script, matching the Chrome you attached to - see below.

#### 3. Install ChromeDriver manually (Linux)

For **`--rip` / `--rp`**, or when **`webdriver-manager`** will not run. Match **`google-chrome --version`** on the Chrome host:

```bash
CHROME_VERSION=$(google-chrome --version | awk '{print $3}')
cd /tmp
wget -q "https://storage.googleapis.com/chrome-for-testing-public/${CHROME_VERSION}/linux64/chromedriver-linux64.zip"
unzip -o chromedriver-linux64.zip
sudo install -m 755 chromedriver-linux64/chromedriver /usr/local/bin/chromedriver
chromedriver --version
```

### Visible window (`--headed`)

Turns off headless mode so a window appears - helpful when the map seems stuck or you want to watch clicks.

### Your own Chrome (`--rip` / `--rp`)

**Plain idea:** you start Chrome yourself with a special “remote debugging” port; the script connects to **that** Chrome instead of starting a fresh one. That helps when you need to be logged in on your home PC while the script runs on a server.

#### 1. Start Chrome on the machine with the screen

Pick a folder for Chrome to remember logins (example below) and a port (often **9222**):

```bash
google-chrome \
  --user-data-dir="$HOME/.bcparks-profile" \
  --remote-debugging-port=9222 \
  --no-first-run \
  --no-default-browser-check
```

Use the same port number later as `--rp`.

#### 2. Tell the script where to connect

| Where the script runs | Typical `--rip` | `--rp` |
|------------------------|-----------------|--------|
| Same computer as Chrome | `127.0.0.1` | e.g. `9222` |
| Server → another PC on your home network | That PC’s IP (if Chrome accepts outside connections) | same port |

Example:

```bash
./reserve_site.py --url '...' --f 'S51' --rip 192.168.1.50 --rp 9222
```

Only open that port on your firewall for trusted machines. **Never** expose it to the whole internet.

**Common snag:** On recent Chrome, the debug port often listens on **`127.0.0.1` only**. Then your server cannot connect to `desktop_ip:9222` directly. The fix that usually works is an **SSH tunnel**: from the server, forward your desktop’s port so the script still uses `127.0.0.1` locally.

```bash
ssh -N -L 9222:127.0.0.1:9222 you@your-desktop
```

Then run the script with `--rip 127.0.0.1 --rp 9222`. Chrome must still be running on the desktop with `--remote-debugging-port=9222`.

**First-time SSH on the desktop:** you may need an SSH server there (Debian/Ubuntu example):

```bash
sudo apt update
sudo apt install -y openssh-server
sudo systemctl enable --now ssh
```

#### 3. ChromeDriver on the machine that runs the script

When you use `--rip` / `--rp`, the script does **not** use the automatic driver download for that path. The computer where you **run** `reserve_site.py` must have a **`chromedriver` program** installed and on your `PATH`, and its version should match the Chrome version you connected to.

```bash
google-chrome --version   # on the computer running Chrome
chromedriver --version    # on the computer running the script
```

#### 4. Why people use “your own” Chrome

- Stay logged in with your normal profile.
- Sometimes the map behaves more reliably than in invisible mode.

The script attaches to the Chrome you started; it does not open a second profile for you.

### Map load troubleshooting

If invisible Chrome keeps timing out on the map:

1. Try **`--debug`** (saved page and screenshot on failure).
2. Try **`--headed`** on a machine with a display.
3. Try **your own Chrome** with `--rip` / `--rp` (often with an SSH tunnel).
4. Make sure Chrome and ChromeDriver versions match when using remote attach.

---

## Policies and disclaimer

- A successful **Reserve** click usually puts a **time-limited hold** in the cart; finish checkout before it expires (often on the order of ~15 minutes).
- Use tools like this responsibly. Polling too aggressively can get an IP blocked - keep reasonable intervals.
- This is a hobby project; BC Parks may change the website at any time.

**Not affiliated with BC Parks.** Use at your own risk.
