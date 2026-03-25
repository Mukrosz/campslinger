# campslinger

Small Python helpers for **BC Parks** camping at [camping.bcparks.ca](https://camping.bcparks.ca/create-booking/): watch availability via the site’s JSON API, and optionally drive the booking map in Chrome to place a cart **hold** (then finish checkout in the browser yourself).

## Platform

**Supported and tested: Debian Linux** (e.g. Debian 12), on a laptop or server with Google Chrome where you intend to run `reserve_site.py`.

**Windows and macOS are not tested** and are **not a priority** right now; instructions here target Linux.

## What’s in this repo

| File | Role |
|------|------|
| `monitor_site_api.py` | Polls availability only (HTTP). Optional SMS (Twilio). **No browser.** |
| `reserve_site.py` | **Normal mode:** API polling on `--interval`, then Selenium loads the results URL, finds **green** map pins (`icon-available`), clicks your preferred site, then **Reserve**. **Warmode (`--warmode`):** no API; at ~1 minute before 07:00 in `--timezone` the map is prefetched, **Reserve** is clicked at 07:00. |
| `requirements.txt` | Python dependencies. |

Run `python3 reserve_site.py --help` for the full CLI (examples, flags, defaults).

### How normal mode ties API and Selenium together

- The API returns **which site labels are free** (`status == 0`) for your search URL — it does **not** give DOM elements.
- Selenium builds `{site_label: icon_element}` from the map (only pins with class **`icon-available`**).
- The script picks the **first** site that is free in the API **and** matches your `--f` order (or the first free site if you omit `--f`), then clicks that label on the map. If the API says free but the map has not painted the pin yet, it waits for the next `--interval` and retries.

Warmode uses **only** the map (green pins + `--f` order); no API calls.

Official BC Parks frontcountry rules (rolling window, 07:00 Pacific, etc.) are summarized on [bcparks.ca — Frontcountry camping](https://bcparks.ca/reservations/frontcountry-camping/).

## Requirements

- **Python 3.10+** (3.10 / 3.11 tested on Debian)
- **`monitor_site_api.py`:** `requests` only (see `requirements.txt`).
- **`reserve_site.py`:** Google Chrome stable, Selenium stack, optional Twilio / `pyshorteners` / `pytz` (warmode). See `requirements.txt`.

## Setup

```bash
git clone https://github.com/Mukrosz/campslinger.git
cd campslinger
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

The `venv/` directory is gitignored.

## Getting the booking URL

1. Open [Create booking](https://camping.bcparks.ca/create-booking/), choose park, dates, equipment, and search.
2. On the **map/results** page, copy the full URL. It must include parameters such as `resourceLocationId`, `mapId`, `startDate`, and `endDate` (used by the API and the page).

Example shape (your values will differ):

```text
https://camping.bcparks.ca/create-booking/results?resourceLocationId=...&mapId=...&startDate=2025-08-18&endDate=2025-08-25&...
```

Use `--url` or `--u` with that string.

## Usage: monitor only (API)

```bash
./monitor_site_api.py --url 'https://camping.bcparks.ca/create-booking/...'
./monitor_site_api.py --url '...' --f 'S51,S52' --i 30
```

SMS (Twilio):

```bash
./monitor_site_api.py --url '...' --f 'S51' --sms --i 60 \
  --twilio_sid … --twilio_auth_token … --twilio_number … --my_phone_number …
```

## Usage: `reserve_site.py`

### Normal mode (default)

Polls the API every `--interval` (two GETs per poll). When a target site is available, opens Chrome once and tries map + **Reserve**.

```bash
./reserve_site.py --url 'https://camping.bcparks.ca/create-booking/...'
./reserve_site.py --url '...' --f 'S51,S52' --i 60
```

- **`--f`:** comma-separated preferred labels; **order is priority** (first match that is API-free and green on the map wins).

### Warmode

```bash
./reserve_site.py --url '...' --f 'S51' --warmode
```

Uses `--timezone` (default `US/Pacific`) for 07:00 / prefetch timing. No API calls in this mode.

### `--headed` (visible Chrome)

Runs a normal browser window instead of headless — useful on a **machine with a display** to debug map loading. Selenium still starts its **own** Chrome process (not your everyday user Chrome), unless you use remote attach below.

### `--debug`

Screenshots on success; on map failure writes `reserve_map_failure.html` and `reserve_map_failure.png` in the current directory.

---

## Chrome, ChromeDriver, and run modes (`reserve_site.py`)

### Default: headless + webdriver-manager

Without `--rip` / `--rp`, the script starts Chrome via Selenium and **[webdriver-manager](https://github.com/SergeyPirogov/webdriver_manager)** downloads a matching **ChromeDriver** (cached under `~/.wdm`). Install **Google Chrome** stable (`google-chrome` on `PATH`).

### Remote attach: `--rip` / `--rp` (existing Chrome with remote debugging)

Use this when you want the automation to drive **Chrome you started yourself** — typically so you can **log in** to BC Parks (or use a specific profile) in a **real window**, while the **Python script runs elsewhere** (e.g. on a headless server).

**1. On the machine that will show Chrome** (your desktop/laptop, or any host where you can run Chrome with a display):

- Pick a **persistent profile directory** (cookies and logins live here). Example: `$HOME/.bcparks-profile`.
- Pick a **port** for the DevTools protocol (default **9222** is common).

Start Chrome **before** running the script:

```bash
google-chrome \
  --user-data-dir="$HOME/.bcparks-profile" \
  --remote-debugging-port=9222 \
  --no-first-run \
  --no-default-browser-check
```

You can change the profile path and port; use the **same port** you pass as `--rp`.

**2. Where `reserve_site.py` runs**

- **Same machine as Chrome:** use `--rip 127.0.0.1 --rp 9222` (or the port you chose).
- **Script on a server, Chrome on your PC (same LAN):** the server must reach the PC’s IP on the debug port. Example: PC address `192.168.1.50`, Chrome listening on `9222`:

  ```bash
  ./reserve_site.py --url '...' --f 'S51' --rip 192.168.1.50 --rp 9222
  ```

  Ensure the **firewall on the PC** allows inbound **TCP** to that port from the server (or only from the server’s IP). **Do not** expose the debug port to the public internet — it is a powerful control surface.

- **Script on a server, Chrome only on localhost (recommended pattern):** use **SSH local port forwarding** from the server to your desktop session:

  ```bash
  # Run from the machine where the script will execute (or use -R reverse as appropriate)
  ssh -N -L 9222:127.0.0.1:9222 you@your-desktop
  ```

  Then on the server:

  ```bash
  ./reserve_site.py --url '...' --f 'S51' --rip 127.0.0.1 --rp 9222
  ```

  Chrome must still be running on the desktop with `--remote-debugging-port=9222` as above.

**3. ChromeDriver on the machine running the script**

Remote mode does **not** use webdriver-manager in the script path that attaches to the debugger. Selenium still needs a **`chromedriver`** binary on **`PATH`** on the **host where you run `reserve_site.py`**, and its **major version must match** the Chrome you started with remote debugging.

```bash
google-chrome --version
chromedriver --version
```

Install or update `chromedriver` if needed (see below).

**4. Why use remote mode**

- Log in once in a real profile; the script reuses that browser context.
- Sometimes avoids headless-specific quirks on complex SPAs.

The script does **not** open a second visible window for you in remote mode; it attaches to the existing Chrome instance you started.

### Manual ChromeDriver install (Linux example)

When you need a system `chromedriver` (especially for `--rip`/`--rp`):

```bash
CHROME_VERSION=$(google-chrome --version | awk '{print $3}')
cd /tmp
wget -q "https://storage.googleapis.com/chrome-for-testing-public/${CHROME_VERSION}/linux64/chromedriver-linux64.zip"
unzip -o chromedriver-linux64.zip
sudo install -m 755 chromedriver-linux64/chromedriver /usr/local/bin/chromedriver
```

### If the map still times out in headless

1. **`--debug`** — failure artifacts and element counts.
2. **`--headed`** — visible window on a machine with a display.
3. **`--rip` / `--rp`** — attach to your own Chrome session (see above).
4. Confirm Chrome / ChromeDriver versions match when using remote attach.

---

## Important notes

- A successful **Reserve** click adds a timed **hold** in the cart; complete payment and details in the browser before the hold expires (on the order of ~15 minutes).
- **Terms of use:** use these tools responsibly. Aggressive or abusive request rates can get your IP blocked; keep `--interval` reasonable for API use.
- This is a hobby project; the BC Parks site can change and break selectors or APIs without notice.

## License / disclaimer

Use at your own risk. Not affiliated with BC Parks.
