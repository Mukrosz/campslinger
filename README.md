# campslinger

Small Python helpers for **BC Parks** camping at [camping.bcparks.ca](https://camping.bcparks.ca/create-booking/): watch availability via the site’s JSON API, and optionally drive the booking map in Chrome to place a cart **hold** (then finish checkout in the browser yourself).

## Scripts

| Script | What it does |
|--------|----------------|
| `monitor_site_api.py` | Polls availability only (HTTP). Prints when sites are free; optional SMS (Twilio). |
| `reserve_site.py` | **Normal mode:** polls the same API on `--interval`, then uses **Selenium** to open the results URL, click the site, and click **Reserve**. **Warmode (`--warmode`):** no API; at one minute before 7:00 in `--timezone` it loads the map and prefetches the sidebar, then clicks **Reserve** at 7:00 (for same-day reservation opens). |

Reservations in BC Parks typically open **three months ahead** at **7:00 Pacific**. Warmode targets that window; adjust `--timezone` if you ever need a different zone.

## Requirements

- **Python 3.10+** (3.11 tested)
- **Google Chrome** + matching ChromeDriver path (or let `webdriver-manager` fetch a driver for `reserve_site.py`)
- **Packages:** see `requirements.txt` (`requests`, `selenium`, `webdriver-manager`, optional `twilio`, `pyshorteners`, `pytz` for warmode)

SMS is optional for both scripts.

## Setup

```bash
git clone https://github.com/Mukrosz/campslinger.git
cd campslinger
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
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

## Important notes

- A successful **Reserve** click adds a timed **hold** in the cart; complete payment and details in the browser before the hold expires (on the order of ~15 minutes).
- **Terms of use:** use these tools responsibly. Aggressive or abusive request rates can get your IP blocked; keep `--interval` reasonable for API use.
- This is a hobby project; the BC Parks site can change and break selectors or APIs without notice.

## License / disclaimer

Use at your own risk. Not affiliated with BC Parks.
