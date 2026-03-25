# campslinger

Small Python helpers for **BC Parks** camping at [camping.bcparks.ca](https://camping.bcparks.ca/create-booking/): watch availability via the site’s JSON API, and optionally drive the booking map in Chrome to place a cart **hold** (then finish checkout in the browser yourself).

## Platform

**Supported and tested: Debian Linux** (e.g. Debian 12), on a laptop or server with Google Chrome where you intend to run `reserve_site.py`.

**Windows and macOS are not tested** and are **not a priority** right now; things might work, but instructions here target Linux. If you need first-class support elsewhere later, open an issue or PR.

## Scripts

| Script | What it does |
|--------|----------------|
| `monitor_site_api.py` | Polls availability only (HTTP). Prints when sites are free; optional SMS (Twilio). |
| `reserve_site.py` | **Normal mode:** polls the same API on `--interval`, then uses **Selenium** to open the results URL, click the site, and click **Reserve**. **Warmode (`--warmode`):** no API; at one minute before 7:00 in `--timezone` it loads the map and prefetches the sidebar, then clicks **Reserve** at 7:00 (for same-day reservation opens). |

Reservations in BC Parks typically open **three months ahead** at **7:00 Pacific**. Warmode targets that window; adjust `--timezone` if you ever need a different zone.

## Requirements

- **Python 3.10+** (3.10 / 3.11 tested on Debian)
- **`monitor_site_api.py`:** no browser — only `requests`.
- **`reserve_site.py`:** **Google Chrome** (stable) installed on the machine. ChromeDriver setup depends on how you run it (see below).
- **Packages:** see `requirements.txt` (`requests`, `selenium`, `webdriver-manager`, optional `twilio`, `pyshorteners`, `pytz` for warmode)

SMS is optional for both scripts.

## Chrome and ChromeDriver (`reserve_site.py` only)

### Headless (default): no manual ChromeDriver

If you do **not** pass `--rip` / `--rp`, the script starts its own Chrome via Selenium and uses **[webdriver-manager](https://github.com/SergeyPirogov/webdriver_manager)** to download a **ChromeDriver** that matches your installed Chrome (cached under `~/.wdm`). You only need:

1. **Google Chrome** stable for Linux (`google-chrome` on `PATH`; install from [Google’s .deb repo](https://www.google.com/chrome/linux/) on Debian/Ubuntu if needed).
2. Dependencies from `requirements.txt` (includes `webdriver-manager`).

On the first run you may see a short delay while the driver is downloaded.

### Remote Chrome (`--rip` / `--rp`): you must provide ChromeDriver

When you attach to an **existing** Chrome with remote debugging, the script calls `webdriver.Chrome(options=…)` **without** webdriver-manager. Selenium then expects a **`chromedriver` executable on your `PATH`** (or in the usual install location), and that driver’s **major version must match** the Chrome you started manually.

Check versions:

```bash
google-chrome --version          # or chromium --version
chromedriver --version
```

If `chromedriver` is missing or mismatched:

**Linux (Chrome for Testing — matches stable Chrome version):**

```bash
CHROME_VERSION=$(google-chrome --version | awk '{print $3}')
cd /tmp
wget -q "https://storage.googleapis.com/chrome-for-testing-public/${CHROME_VERSION}/linux64/chromedriver-linux64.zip"
unzip -o chromedriver-linux64.zip
sudo install -m 755 chromedriver-linux64/chromedriver /usr/local/bin/chromedriver
```

Adjust the URL if Google changes hosting; the version string must match your Chrome.

### If headless still fails

- Confirm Chrome launches manually.
- Upgrade packages: `pip install -U selenium webdriver-manager`.
- Run with `--debug` and check whether the map loads; some environments need a display or different Chrome flags (outside the scope of this README).

## Setup

```bash
git clone https://github.com/Mukrosz/campslinger.git
cd campslinger
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

The `venv/` directory is gitignored; keep it local.

## Getting the booking URL

1. Open [Create booking](https://camping.bcparks.ca/create-booking/), choose park, dates, equipment, and search.
2. On the **map/results** page, copy the full URL from the address bar. It must include query parameters such as `resourceLocationId`, `mapId`, `startDate`, and `endDate` (the scripts parse these for the API).

Example shape (your values will differ):

```text
https://camping.bcparks.ca/create-booking/results?resourceLocationId=...&mapId=...&startDate=2025-08-18&endDate=2025-08-25&...
```

## Usage: monitor only (API)

Poll every 60 seconds (default); only sites matching `--filter` if set:

```bash
./monitor_site_api.py --url 'https://camping.bcparks.ca/create-booking/...'
./monitor_site_api.py --url '...' --f 'S51,S52' --i 30
```

SMS:

```bash
./monitor_site_api.py --url '...' --f 'S51' --sms --i 60 \
  --twilio_sid … --twilio_auth_token … --twilio_number … --my_phone_number …
```

## Usage: reserve (hold in cart)

**Normal mode** — API tells you when a site is available; the browser performs the click path. One API poll per loop (two HTTP GETs per poll), then sleep for `--interval`. No tight polling loops.

```bash
./reserve_site.py --url 'https://camping.bcparks.ca/create-booking/...'
./reserve_site.py --url '...' --f 'S51,S52' --i 60
```

**Warmode** — Selenium only at prefetch time (no API spam). Prefetch at **6:59**, click **Reserve** at **7:00** `US/Pacific` by default:

```bash
./reserve_site.py --url '...' --f 'S51' --warmode
```

**Signed-in Chrome (remote debugging)** — start Chrome, log in, then point the script at the debug port so the hold uses your session:

```bash
google-chrome --user-data-dir="$HOME/.bcparks-profile" \
  --remote-debugging-port=9222 --no-first-run --no-default-browser-check
./reserve_site.py --url '...' --rip 127.0.0.1 --rp 9222 --f 'S51'
```

Debug screenshots (saved in the current working directory):

```bash
./reserve_site.py --url '...' --f 'S51' --debug
```

**Map timeout (“Timeout waiting for map or map icons”)** — the API can see availability before the map finishes in headless Chrome (slow network, bot checks, or DOM changes). Try in order:

1. **`--debug`** — prints counts for `.map-container` / `.map-icon` and writes `reserve_map_failure.html` and `reserve_map_failure.png` on failure. Open the HTML in a browser or search it for `map-icon` / `map-container` in DevTools on the live site to see if class names changed.
2. **`--headed`** — runs a visible Chrome window so you can watch the map load (useful on a machine with a display; for SSH use X forwarding or VNC).
3. **`--rip` / `--rp`** — attach to your normal Chrome session (see above) so the map behaves like manual browsing.
4. Confirm **`google-chrome`** matches the **ChromeDriver** version when using remote mode.

## Important notes

- A successful **Reserve** click adds a timed **hold** in the cart; complete payment and details in the browser before the hold expires (on the order of ~15 minutes).
- **Terms of use:** use these tools responsibly. Aggressive or abusive request rates can get your IP blocked; keep `--interval` reasonable for API use.
- This is a hobby project; the BC Parks site can change and break selectors or APIs without notice.

## License / disclaimer

Use at your own risk. Not affiliated with BC Parks.
