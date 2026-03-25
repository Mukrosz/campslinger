# campslinger

Tools for **BC Parks** camping at [camping.bcparks.ca](https://camping.bcparks.ca/create-booking/): poll availability over the public JSON API, and optionally use Chrome (Selenium) to place a cart **hold** on a site—then finish checkout in the browser.

**Platform:** developed and tested on **Debian Linux** (e.g. Debian 12). Windows and macOS are not covered here.

---

## Contents

- [Repository layout](#repository-layout)
- [How the two scripts differ](#how-the-two-scripts-differ)
- [Requirements and setup](#requirements-and-setup)
- [Booking URL](#booking-url)
- [Usage](#usage)
- [Chrome: run modes and ChromeDriver](#chrome-run-modes-and-chromedriver)
- [Policies and disclaimer](#policies-and-disclaimer)

Subsections under **Usage** and **Chrome** cover each script, flags, remote attach, and troubleshooting.

---

## Repository layout

| Path | Purpose |
|------|---------|
| `monitor_site_api.py` | Availability only (HTTP). Optional SMS. **No browser.** |
| `reserve_site.py` | API + Selenium (normal) or Selenium-only (warmode). See below. |
| `reserve_site_v2.py` | `reserve_site.py` behavior plus Telegram bot control mode (long polling, multi-job). |
| `requirements.txt` | Python dependencies. |

Full flag lists, defaults, and examples:
- `python3 reserve_site.py --help`
- `python3 reserve_site_v2.py --help`

---

## How the two scripts differ

### `monitor_site_api.py`

Repeated GETs to BC Parks JSON endpoints derived from your results URL. Prints when sites are available. No map, no clicks.

### `reserve_site.py`

| Mode | API | Browser |
|------|-----|--------|
| **Normal (default)** | Yes: polls every `--interval` (two GETs per poll). Chooses a target site from API availability and your `--f` order. | Loads the results URL, finds **green** pins (`icon-available`), clicks the matching site, then **Reserve**. |
| **`--warmode`** | No | At ~1 minute before 07:00 in `--timezone`, prefetches the map and sidebar; clicks **Reserve** at 07:00. |

**Normal mode in one sentence:** the API says *which label is free*; Selenium finds the pin for that label on the map. The API does not supply DOM nodes—only labels and status.

**Official booking rules** (rolling window, 07:00 Pacific, etc.): [BC Parks — Frontcountry camping](https://bcparks.ca/reservations/frontcountry-camping/).

---

## Requirements and setup

- **Python 3.10+** (3.10 / 3.11 tested on Debian).
- **`monitor_site_api.py`:** `requests` (and optional Twilio / `pyshorteners` per `requirements.txt`).
- **`reserve_site.py`:** Google Chrome stable, Selenium, `webdriver-manager`; optional Twilio, `pyshorteners`, `pytz` (warmode).

```bash
git clone https://github.com/Mukrosz/campslinger.git
cd campslinger
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

`venv/` is gitignored.

---

## Booking URL

1. Open [Create booking](https://camping.bcparks.ca/create-booking/), set park, dates, equipment, search.
2. On the **map / results** page, copy the address bar URL. It must include parameters such as `resourceLocationId`, `mapId`, `startDate`, `endDate` (required for the API).

Example shape:

```text
https://camping.bcparks.ca/create-booking/results?resourceLocationId=...&mapId=...&startDate=2025-08-18&endDate=2025-08-25&...
```

Pass it as `--url` or `--u`.

---

## Usage

### monitor_site_api.py

```bash
./monitor_site_api.py --url 'https://camping.bcparks.ca/create-booking/...'
./monitor_site_api.py --url '...' --f 'S51,S52' --i 30
```

SMS (Twilio):

```bash
./monitor_site_api.py --url '...' --f 'S51' --sms --i 60 \
  --twilio_sid … --twilio_auth_token … --twilio_number … --my_phone_number …
```

### reserve_site.py

**Normal mode** — API polling + one browser pass when a target is free:

```bash
./reserve_site.py --url 'https://camping.bcparks.ca/create-booking/...'
./reserve_site.py --url '...' --f 'S51,S52' --i 60
```

- `--f`: comma-separated site labels. **Order is priority:** first site that is free in the API **and** shows as green on the map is used.

**Warmode:**

```bash
./reserve_site.py --url '...' --f 'S51' --warmode
```

Uses `--timezone` (default `US/Pacific`) for 07:00 / prefetch. No API calls.

**Debugging:**

| Flag | Effect |
|------|--------|
| `--headed` | Visible Chrome window (needs a display). Still a separate automated Chrome, not your daily profile—unless you use remote attach. |
| `--debug` | Extra logging; on map failure writes `reserve_map_failure.html` and `reserve_map_failure.png` in the working directory. |

### reserve_site_v2.py (Telegram-enabled)

`reserve_site_v2.py` keeps CLI usage compatible with `reserve_site.py` and adds:

- `--telegram-bot` to run a Telegram long-polling control loop.
- `--max-concurrent` (default `3`) to cap concurrent reservation jobs started from chat.
- Job commands: `/reserve`, `/jobs`, `/status`, `/cancel`, `/help`.
- Optional convenience: sending a plain booking URL in chat starts a default normal-mode job.

Telegram bot environment variables (required):

```bash
export TELEGRAM_BOT_TOKEN='123456:ABC...'
export TELEGRAM_ALLOWED_USER_IDS='11111111,22222222'
```

Run bot mode:

```bash
./reserve_site_v2.py --telegram-bot --max-concurrent 3
```

Command examples in Telegram:

```text
/reserve https://camping.bcparks.ca/create-booking/results?... --f 29,11 --i 60 --debug
/reserve https://... --warmode --f S51 --timezone US/Pacific
/jobs
/status ab12cd34
/cancel ab12cd34
```

---

## Chrome: run modes and ChromeDriver

`reserve_site.py` can drive Chrome in three ways:

| Situation | What to use |
|-----------|-------------|
| Default | Headless Chrome; **webdriver-manager** downloads a matching ChromeDriver to `~/.wdm`. |
| See the browser locally | `--headed` |
| Use your own logged-in Chrome (often script on server, browser on desktop) | `--rip` + `--rp` |

You need **Google Chrome** stable on the machine where Chrome runs (`google-chrome` on `PATH`).

### Default (headless)

No `--rip` / `--rp`: Selenium starts Chrome with **`--headless=new`** and uses **webdriver-manager** so you usually do **not** install `chromedriver` by hand.

### Visible window (`--headed`)

Pass **`--headed`** to disable headless mode. Use on a machine with a graphical session when debugging map load or timeouts.

### Remote Chrome (`--rip` / `--rp`)

Attach Selenium to **Chrome you start manually**—useful for logging into BC Parks in a real profile, or when the script runs on a **server** while Chrome runs on your **desktop**.

#### 1. Start Chrome (on the machine with the display)

Choose a persistent profile directory and a debug port (often **9222**):

```bash
google-chrome \
  --user-data-dir="$HOME/.bcparks-profile" \
  --remote-debugging-port=9222 \
  --no-first-run \
  --no-default-browser-check
```

Use the same port you pass as `--rp`. You may change the profile path.

#### 2. Point `reserve_site.py` at that instance

| Scenario | `--rip` | `--rp` |
|----------|---------|--------|
| Script and Chrome on **same** host | `127.0.0.1` | your port |
| Script on **server**, Chrome on **another PC** on the LAN | PC’s LAN IP (e.g. `192.168.1.50`) | your port |

Example (Chrome on `192.168.1.50:9222`):

```bash
./reserve_site.py --url '...' --f 'S51' --rip 192.168.1.50 --rp 9222
```

Open the firewall on the PC running Chrome for **inbound TCP** to that port from the server only. **Do not** expose the debug port to the public internet.

**Note on LAN connections:** on many modern Chrome builds, the remote-debugging port binds to `127.0.0.1` by default. In that case, connecting directly to `desktop_ip:9222` from your server will fail even on a trusted LAN. The most reliable approach is an **SSH tunnel**.

**SSH tunnel (common for “server runs script, desktop runs Chrome”):** on the machine where the script runs, forward the desktop’s debug port:

```bash
ssh -N -L 9222:127.0.0.1:9222 you@your-desktop
```

Then use `--rip 127.0.0.1 --rp 9222`. Chrome must still be running on the desktop with `--remote-debugging-port=9222`.

If you don’t have SSH access to your desktop yet (Debian/Ubuntu), install and enable an SSH server there:

```bash
sudo apt update
sudo apt install -y openssh-server
sudo systemctl enable --now ssh
```

#### 3. ChromeDriver on the host that runs the script

Remote attach does **not** use webdriver-manager for that code path. The machine where you run **`reserve_site.py`** must have **`chromedriver` on `PATH`**, and its **major version** must match the Chrome you opened with remote debugging.

```bash
google-chrome --version   # on the Chrome host
chromedriver --version    # on the script host
```

#### 4. Why remote mode

- Reuse cookies / login from a normal profile.
- Sometimes avoids headless-only site behavior.

The script **attaches** to the existing window; it does not open a second profiled Chrome for you.

### Install `chromedriver` manually (Linux)

Needed especially for `--rip` / `--rp`:

```bash
CHROME_VERSION=$(google-chrome --version | awk '{print $3}')
cd /tmp
wget -q "https://storage.googleapis.com/chrome-for-testing-public/${CHROME_VERSION}/linux64/chromedriver-linux64.zip"
unzip -o chromedriver-linux64.zip
sudo install -m 755 chromedriver-linux64/chromedriver /usr/local/bin/chromedriver
```

### Map load troubleshooting

If headless times out waiting for the map:

1. **`--debug`** — saves HTML/PNG on failure.
2. **`--headed`** — confirm the map loads visually.
3. **`--rip` / `--rp`** — use a real logged-in session.
4. Verify Chrome and ChromeDriver versions match when using remote attach.

---

## Policies and disclaimer

- A successful **Reserve** click creates a timed **hold**; complete checkout before it expires (often on the order of ~15 minutes).
- Use responsibly: aggressive API polling can get an IP blocked—keep `--interval` reasonable.
- Hobby project; BC Parks may change the site at any time.

**Not affiliated with BC Parks.** Use at your own risk.
