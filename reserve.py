#!/usr/bin/env python3
"""
BC Parks reservation helper: API polling (normal mode) + Selenium for map / Reserve.
Warmode uses Selenium only - prefetch at T-1 minute, click Reserve at 7:00 (no API calls).
"""

import argparse
import os
import random
import re
import sys
import time
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

BCPARKS_API_BASE = "https://camping.bcparks.ca/api/"
BCPARKS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}
_PARK_NAME = None


def sort_key(s):
    match = re.match(r"([A-Za-z]*)(\d+)([A-Za-z]*)", s.strip())
    if match:
        prefix, number, suffix = match.groups()
        return (prefix, int(number), suffix)
    return (s, 0, "")


def comma_separated_list(value):
    return [item.strip().lower() for item in value.split(",")]


def current_time():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def pp(message, error=False):
    park = _PARK_NAME.strip() if isinstance(_PARK_NAME, str) else ""
    prefix = "[{}] ".format(park) if park else ""
    if error:
        sys.exit("{} - {}{}".format(current_time(), prefix, message))
    print("{} - {}{}".format(current_time(), prefix, message))


def randomized_probe_wait_seconds(interval_seconds, jitter_seconds):
    base = max(1, int(interval_seconds))
    spread = max(0, int(jitter_seconds or 0))
    if spread == 0:
        return base
    low = max(1, base - spread)
    high = base + spread
    return random.randint(low, high)


def shorten_url(url):
    try:
        import pyshorteners

        return pyshorteners.Shortener().tinyurl.short(url)
    except Exception as e:
        sys.exit("Error: pyshorteners function failed: {}".format(e))


def send_sms(message, client, to_number, from_number):
    msg = client.messages.create(to=to_number, from_=from_number, body=message)
    pp("SMS sent: {}".format(msg.sid))


def debug_screenshot(driver, path, message="Debug screenshot saved"):
    try:
        driver.save_screenshot(path)
        pp("📸 {}: {}".format(message, path))
    except Exception as e:
        pp("⚠️  Screenshot failed ({}): {}".format(path, e))


def bcparks_parse_url(url, params):
    try:
        url_params = parse_qs(urlparse(url).query)
        url_params = {key: url_params[key][0] for key in params if key in url_params}
        if len(url_params) != len(params):
            missing = set(params) - set(url_params.keys())
            raise ValueError("Missing params: {}".format(missing))
    except Exception as e:
        raise ValueError("Invalid URL: {}".format(e)) from e
    return url_params


def bcparks_normalize_sites(n_dict, a_dict):
    merged = {}
    for key in a_dict.get("resourceAvailabilities", {}):
        name = n_dict[key].get("localizedValues", {})[0].get("name", "")
        status = (
            a_dict.get("resourceAvailabilities", {})
            .get(key, {})[0]
            .get("availability", "")
        )
        label = name.strip()
        merged[label.lower()] = {"status": status, "id": key, "label": label}
    return {k: merged[k] for k in sorted(merged, key=sort_key)}


def bcparks_fetch_sites_map(booking_url):
    """
    Two GETs (resources + availability). Returns dict site_key_lower -> {status, id, label}
    or raises on HTTP/JSON/parse errors.
    """
    site_name_params = bcparks_parse_url(
        booking_url, ["resourceLocationId", "mapId"]
    )
    site_status_params = bcparks_parse_url(
        booking_url, ["mapId", "startDate", "endDate"]
    )
    names_url = "{}resourcelocation/resources?{}".format(
        BCPARKS_API_BASE, urlencode(site_name_params)
    )
    status_url = "{}availability/map?{}".format(
        BCPARKS_API_BASE, urlencode(site_status_params)
    )
    r1 = requests.get(names_url, headers=BCPARKS_HEADERS, timeout=30)
    r1.raise_for_status()
    r2 = requests.get(status_url, headers=BCPARKS_HEADERS, timeout=30)
    r2.raise_for_status()
    return bcparks_normalize_sites(r1.json(), r2.json())


def bcparks_fetch_park_name(booking_url):
    """
    Best-effort park name lookup by resourceLocationId.
    Returns None when not found or on any request/shape failure.
    """
    try:
        p = bcparks_parse_url(booking_url, ["resourceLocationId"])
        rid_str = p["resourceLocationId"]
        rid_int = int(rid_str)
        loc_url = "{}resourcelocation?resourceLocationId={}".format(
            BCPARKS_API_BASE, rid_str
        )
        r = requests.get(loc_url, headers=BCPARKS_HEADERS, timeout=30)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            return None
        for loc in data:
            if not isinstance(loc, dict) or loc.get("resourceLocationId") != rid_int:
                continue
            locs = loc.get("localizedValues")
            if not isinstance(locs, list) or not locs or not isinstance(locs[0], dict):
                return None
            first = locs[0]
            for key in ("fullName", "shortName", "name", "value"):
                v = first.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            return None
    except Exception:
        return None
    return None


def api_available_labels(sites):
    """Display labels (sorted) for sites with API status == available (0)."""
    labels = []
    for key in sorted(sites.keys(), key=sort_key):
        if sites[key].get("status") == 0:
            labels.append(sites[key].get("label", key))
    return labels


def pick_api_target(sites, requested_sites):
    """
    First available (status == 0) site in preference order.
    requested_sites: lowercased labels, or None/empty for any site.
    """
    if not sites:
        return None
    if requested_sites:
        pool = requested_sites
    else:
        pool = list(sites.keys())
    for key in pool:
        if key in sites and sites[key].get("status") == 0:
            return key
    return None


def setup_webdriver_remote(ip, port):
    options = Options()
    options.add_experimental_option("debuggerAddress", "{}:{}".format(ip, port))
    try:
        return webdriver.Chrome(options=options)
    except WebDriverException as e:
        pp("❌ Failed to connect to existing Chrome instance: {}".format(e))
        return None


def setup_webdriver(headed=False):
    options = Options()
    if not headed:
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--log-level=3")
    options.add_argument("--window-size=1920,1400")
    options.add_argument(
        "--user-agent={}".format(BCPARKS_HEADERS["User-Agent"])
    )
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    try:
        driver = webdriver.Chrome(
            service=ChromeService(ChromeDriverManager().install()), options=options
        )
        driver.set_page_load_timeout(120)
        try:
            driver.execute_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
        except Exception:
            pass
        return driver
    except WebDriverException as e:
        pp("❌ WebDriver failed to start: {}".format(e))
        return None


def _dump_map_load_failure(driver, debug):
    """Log page state; with --debug write HTML + PNG for DevTools inspection."""
    try:
        pp("   Diagnostic: title={!r}".format(driver.title))
        pp("   Current URL: {}".format(driver.current_url))
        n_mc = len(driver.find_elements(By.CSS_SELECTOR, ".map-container"))
        n_mi = len(driver.find_elements(By.CLASS_NAME, "map-icon"))
        pp(
            "   Elements: .map-container={}  .map-icon={}".format(n_mc, n_mi)
        )
    except Exception as e:
        pp("   Could not inspect page: {}".format(e))
    if not debug:
        pp(
            "   Re-run with --debug to save reserve_map_failure.html and "
            "reserve_map_failure.png here."
        )
        return
    html_path = os.path.join(os.getcwd(), "reserve_map_failure.html")
    png_path = os.path.join(os.getcwd(), "reserve_map_failure.png")
    try:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        pp("   Wrote {}".format(html_path))
    except Exception as e:
        pp("   Could not write HTML: {}".format(e))
    debug_screenshot(driver, png_path, message="Map load failure screenshot")


def get_available_sites(driver, url, max_attempts=5, retry_delay=1, debug=False):
    """
    Selenium: map icons with class icon-available -> {label_lower: icon_element}.
    Normal mode uses this after API says a site is free; warmode uses it at prefetch.
    """
    for attempt in range(max_attempts):
        available = {}
        try:
            pp(
                "⏳ Scanning map for available sites (attempt {}/{})...".format(
                    attempt + 1, max_attempts
                )
            )
            driver.get(url)

            # Map root class may change; accept .map-container OR any .map-icon.
            WebDriverWait(driver, 90).until(
                lambda d: (
                    len(d.find_elements(By.CSS_SELECTOR, ".map-container")) > 0
                    or len(d.find_elements(By.CLASS_NAME, "map-icon")) > 0
                )
            )

            WebDriverWait(driver, 90).until(
                lambda d: len(d.find_elements(By.CLASS_NAME, "map-icon")) > 0
            )

            stable_count = 0
            last_count = 0
            for _ in range(12):
                icons = driver.find_elements(By.CLASS_NAME, "map-icon")
                count = len(icons)
                if count == last_count:
                    stable_count += 1
                    if stable_count >= 2:
                        break
                else:
                    stable_count = 0
                    last_count = count
                time.sleep(0.5)

            icons = driver.find_elements(By.CLASS_NAME, "map-icon")
            for i, icon in enumerate(icons):
                try:
                    if "icon-available" not in (icon.get_attribute("class") or ""):
                        continue
                    label_el = icon.find_element(
                        By.XPATH,
                        './following-sibling::*[contains(@class, "map-site-label")]',
                    )
                    label_text = (
                        label_el.find_element(By.CLASS_NAME, "resource-label")
                        .text.strip()
                        .lower()
                    )
                    if label_text:
                        available[label_text] = icon
                except (StaleElementReferenceException, NoSuchElementException):
                    continue

            if available:
                pp(
                    "✨ Map reports {} available site(s): {}".format(
                        len(available),
                        ",".join(sorted(available.keys(), key=sort_key)),
                    )
                )
            return available

        except TimeoutException:
            pp("❌ Timeout waiting for map or map icons")
            _dump_map_load_failure(driver, debug)
        except WebDriverException as e:
            pp("❌ WebDriver error: {}".format(e))
            break
        except Exception as e:
            pp("❌ Unexpected error: {}".format(e))

        time.sleep(retry_delay)

    pp("❌ Failed to read map after {} attempts".format(max_attempts))
    return {}


def collect_available_icons_from_map(driver, url, debug=False):
    """Single navigation + parse; returns same dict shape as get_available_sites last attempt."""
    return get_available_sites(
        driver, url, max_attempts=3, retry_delay=1, debug=debug
    )


def prepare_reservation(driver, available_sites, requested_sites, debug=False):
    available_site_names = list(available_sites.keys())
    requested_sites = requested_sites if requested_sites else available_site_names

    for site in requested_sites:
        if site in available_sites:
            try:
                pp("✅ Clicking site icon: {}".format(site))
                driver.execute_script("arguments[0].click();", available_sites[site])

                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.CLASS_NAME, "side-bar-container"))
                )
                reserve_buttons = WebDriverWait(driver, 15).until(
                    EC.presence_of_all_elements_located((By.ID, "addToStay"))
                )
                if reserve_buttons:
                    reserve_button = reserve_buttons[-1]
                    if debug:
                        debug_screenshot(
                            driver,
                            os.path.join(os.getcwd(), "ss-after_clicking_site.png"),
                        )
                    return site, reserve_button
            except Exception as e:
                pp("⚠️  Skipped site {} due to: {}".format(site, e))

    pp("❌ None of the preferred sites are available on the map")
    return "", None


def reserve_normal_mode(driver, url, requested_sites, interval, interval_jitter=10, debug=False):
    """
    One API poll per loop iteration. Wait between polls is randomized around --interval
    by jitter seconds (default 10) to reduce strict periodic cadence.
    When API shows a target available, load the map once and click Reserve.
    """
    while True:
        wait_s = randomized_probe_wait_seconds(interval, interval_jitter)
        try:
            sites = bcparks_fetch_sites_map(url)
        except Exception as e:
            pp("❌ API poll failed: {}".format(e))
            time.sleep(wait_s)
            continue

        avail = api_available_labels(sites)
        if avail:
            pp(
                "✨ Available sites: {}".format(",".join(avail))
            )
        else:
            pp("❌ No availability")
            time.sleep(wait_s)
            continue

        target = pick_api_target(sites, requested_sites)
        if not target:
            if requested_sites:
                pp(
                    "❌ None of your preferred sites ({}) are free.".format(",".join(requested_sites))
                )
            else:
                pp("❌ Could not pick a target site; retrying…")
            time.sleep(wait_s)
            continue

        label = sites[target].get("label", target)
        pp("🎯 Trying map + Reserve for: {} …".format(label))

        on_map = collect_available_icons_from_map(driver, url, debug=debug)
        if target not in on_map:
            pp(
                "⚠️  API shows {} but map has no matching available icon yet; "
                "waiting {}s (base {}s, jitter ±{}s)…".format(
                    wait_s, interval, max(0, int(interval_jitter or 0))
                )
            )
            time.sleep(wait_s)
            continue

        site, reserve_button = prepare_reservation(
            driver, on_map, [target], debug=debug
        )
        if site and reserve_button:
            try:
                driver.execute_script(
                    "arguments[0].scrollIntoView(true);", reserve_button
                )
                if debug:
                    debug_screenshot(
                        driver,
                        os.path.join(os.getcwd(), "ss-before_clicking_reserve.png"),
                    )
                driver.execute_script("arguments[0].click();", reserve_button)
                pp("✅ Clicked the Reserve button")
                time.sleep(5)
                if debug:
                    debug_screenshot(
                        driver,
                        os.path.join(os.getcwd(), "ss-after_clicking_reserve.png"),
                    )
                return site
            except Exception as e:
                pp("❌ Failed to click reserve button: {}".format(e))
        else:
            pp("❌ Could not prepare reservation")

        time.sleep(wait_s)


def reserve_war_mode(driver, url, requested_sites, timezone="US/Pacific", debug=False):
    """
    No API calls. At T-1 minute: load map, find available icons, open sidebar, grab
    #addToStay. At 7:00: click Reserve (Selenium element handle).
    """
    try:
        import pytz
        from datetime import timedelta
    except ImportError:
        sys.exit("Error: pytz module not found. Install with `pip install pytz`")

    def wait_until(target_time):
        while True:
            now = datetime.now(tz=target_time.tzinfo)
            if now >= target_time:
                break
            time.sleep(min(0.05, (target_time - now).total_seconds()))

    tz = pytz.timezone(timezone)
    now = datetime.now(tz)
    target_time = now.replace(hour=7, minute=0, second=0, microsecond=0)
    if now >= target_time:
        target_time += timedelta(days=1)

    def fmt_ampm(dt):
        try:
            return dt.strftime("%-I:%M%p")
        except ValueError:
            return dt.strftime("%I:%M%p").lstrip("0")

    pp(
        "⚔️  Warmode: prefetch at {} {}…".format(
            fmt_ampm(target_time - timedelta(minutes=1)), timezone
        )
    )
    wait_until(target_time - timedelta(minutes=1))

    available_sites = get_available_sites(driver, url, debug=debug)
    if not available_sites:
        pp("❌ No available sites on map at prefetch time")
        return ""

    site, reserve_button = prepare_reservation(
        driver, available_sites, requested_sites, debug=debug
    )
    if not site or not reserve_button:
        pp("❌ Could not prepare reservation (prefetch)")
        return ""

    pp(
        "✅ Prefetch done. Waiting to click Reserve for {} at {}…".format(
            site, fmt_ampm(target_time)
        )
    )
    wait_until(target_time)

    try:
        driver.execute_script("arguments[0].scrollIntoView(true);", reserve_button)
        if debug:
            debug_screenshot(
                driver, os.path.join(os.getcwd(), "ss-before_clicking_reserve.png")
            )
        driver.execute_script("arguments[0].click();", reserve_button)
        pp("✅ Clicked the Reserve button")
        time.sleep(5)
        if debug:
            debug_screenshot(
                driver, os.path.join(os.getcwd(), "ss-after_clicking_reserve.png")
            )
        return site
    except Exception as e:
        pp(
            "❌ Failed to click reserve button at {}: {}".format(fmt_ampm(target_time), e)
        )
        return ""


class _HelpFormatter(
    argparse.ArgumentDefaultsHelpFormatter, argparse.RawTextHelpFormatter
):
    """Show defaults in option help and preserve newlines in description/epilog."""

    pass


def build_arg_parser():
    description = (
        "Place a BC Parks campsite hold via the booking map (Selenium).\n\n"
        "Normal mode:\n"
        "  Poll the public API every --interval (two GETs per poll: resource names + map\n"
        "  availability). When your preferred site is free in the API, load the results URL\n"
        "  in Chrome, click the matching green map pin (icon-available), then Reserve.\n\n"
        "Warmode (--w / --warmode):\n"
        "  No API calls. About one minute before 7:00 in --timezone, load the map, open the\n"
        "  sidebar for the first available preferred site, then click Reserve at 7:00.\n"
        "  Intended for first-day-of-window bookings (see BC Parks frontcountry rules).\n\n"
        "More context: https://github.com/Mukrosz/campslinger\n\n"
        "Examples:\n"
        "  Reserve any available site (normal mode; API picks first free key when --f omitted).\n"
        "    ./reserve.py --url 'https://camping.bcparks.ca/create-booking/...'\n"
        "    ./reserve.py --u   'https://camping.bcparks.ca/create-booking/...'\n\n"
        "  Prefer specific sites (left-to-right order = try order; first match wins).\n"
        "    ./reserve.py --url '...' --f 'S51,S52,S53'\n\n"
        "  Poll every 30 seconds instead of 60.\n"
        "    ./reserve.py --url '...' --f 'S51' --interval 30\n\n"
        "  Add jitter of 10s around interval (e.g. 50-70s when --i 60).\n"
        "    ./reserve.py --url '...' --f 'S51' --interval 60 --jitter 10\n\n"
        "  Warmode at 7:00 Pacific (prefetch ~6:59).\n"
        "    ./reserve.py --url '...' --f 'S51' --warmode\n\n"
        "  Remote Chrome (log in first), then attach (ChromeDriver on PATH must match Chrome).\n"
        "    google-chrome --user-data-dir=$HOME/.bcparks-profile \\\n"
        "      --remote-debugging-port=9222 --no-first-run --no-default-browser-check\n"
        "    ./reserve.py --url '...' --rip 127.0.0.1 --rp 9222 --f 'S51'\n\n"
        "  Headed browser (debug map/timeouts on a machine with a display).\n"
        "    ./reserve.py --url '...' --f 'S51' --headed\n\n"
        "  SMS when a reservation succeeds (requires Twilio credentials).\n"
        "    ./reserve.py --url '...' --f 'S51' --sms \\\n"
        "      --twilio_sid … --twilio_auth_token … --twilio_number … --my_phone_number …\n\n"
        "  Save map step screenshots / failure HTML when the map fails to load.\n"
        "    ./reserve.py --url '...' --f 'S51' --debug"
    )
    epilog = (
        "Notes:\n"
        "  --url and --u are equivalent. Normal mode uses the API only to choose *when* and\n"
        "  *which label* to reserve; Selenium must still click the map. Warmode uses the map\n"
        "  only (green pins / icon-available). Debian/Linux is the tested platform; see README."
    )
    parser = argparse.ArgumentParser(
        description=description,
        epilog=epilog,
        formatter_class=_HelpFormatter,
    )
    parser.add_argument(
        "--url",
        "--u",
        dest="url",
        required=True,
        metavar="URL",
        help="Full create-booking *results* URL (must include resourceLocationId, mapId, "
        "startDate, endDate in the query string).",
    )
    parser.add_argument(
        "--interval",
        "--i",
        type=int,
        default=60,
        metavar="SECONDS",
        help="Seconds between API polls in normal mode (ignored in warmode).",
    )
    parser.add_argument(
        "--jitter",
        "--ij",
        type=int,
        default=10,
        metavar="SECONDS",
        help="Random variance in seconds around --interval in normal mode. "
        "Example: --i 60 --jitter 10 => each wait in 50-70s.",
    )
    parser.add_argument(
        "--filter",
        "--f",
        type=comma_separated_list,
        metavar="SITES",
        help="Comma-separated preferred campsite labels, lowercased internally. "
        "Order matters: first API-available / first green pin match is used.",
    )
    parser.add_argument(
        "--sms",
        "--s",
        action="store_true",
        help="Send a Twilio SMS when a reservation completes successfully.",
    )
    parser.add_argument(
        "--twilio_sid",
        "--tsid",
        default="",
        metavar="SID",
        help="Twilio Account SID (required with --sms).",
    )
    parser.add_argument(
        "--twilio_auth_token",
        "--tat",
        default="",
        metavar="TOKEN",
        help="Twilio auth token (required with --sms).",
    )
    parser.add_argument(
        "--twilio_number",
        "--tn",
        default="",
        metavar="FROM",
        help="Twilio sending phone number (required with --sms).",
    )
    parser.add_argument(
        "--my_phone_number",
        "--mpn",
        default="",
        metavar="TO",
        help="Your mobile number to receive SMS (required with --sms).",
    )
    parser.add_argument(
        "--warmode",
        "--w",
        action="store_true",
        help="Warmode: prefetch map ~1 minute before 07:00 in --timezone, click Reserve "
        "at 07:00. Selenium only (no API).",
    )
    parser.add_argument(
        "--debug",
        "--d",
        action="store_true",
        help="Verbose diagnostics: screenshots on success; on map failure writes "
        "reserve_map_failure.html and reserve_map_failure.png in the cwd.",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run Chrome with a visible window (disable headless). Useful for debugging.",
    )
    parser.add_argument(
        "--timezone",
        default="US/Pacific",
        metavar="TZ",
        help="IANA timezone name for warmode 7:00 / prefetch timing.",
    )
    parser.add_argument(
        "--remote_ip",
        "--rip",
        metavar="HOST",
        help="Host running Chrome with --remote-debugging-port (use with --rp).",
    )
    parser.add_argument(
        "--remote_port",
        "--rp",
        type=int,
        metavar="PORT",
        help="Remote debugging port (e.g. 9222). Both --rip and --rp required together.",
    )
    return parser


def main():
    global _PARK_NAME
    args = build_arg_parser().parse_args()
    _PARK_NAME = bcparks_fetch_park_name(args.url)

    client = None
    if args.sms:
        try:
            from twilio.rest import Client

            client = Client(args.twilio_sid, args.twilio_auth_token)
        except ImportError:
            sys.exit("Error: pip install twilio")

    if args.remote_ip and args.remote_port:
        driver = setup_webdriver_remote(args.remote_ip, args.remote_port)
    else:
        driver = setup_webdriver(headed=args.headed)

    if not driver:
        sys.exit("❌ WebDriver initialization failed.")

    use_remote = bool(args.remote_ip and args.remote_port)

    try:
        if args.warmode:
            reserved = reserve_war_mode(
                driver,
                args.url,
                args.filter,
                timezone=args.timezone,
                debug=args.debug,
            )
        else:
            reserved = reserve_normal_mode(
                driver,
                args.url,
                args.filter,
                interval=args.interval,
                interval_jitter=args.jitter,
                debug=args.debug,
            )

        if reserved:
            pp("🎯 Reserved: {}".format(reserved))
            if args.sms:
                send_sms(
                    "{} - 🎯 Reserved: {}\n{}".format(
                        current_time(), reserved, shorten_url(args.url)
                    ),
                    client,
                    args.my_phone_number,
                    args.twilio_number,
                )
        else:
            pp("❌ No reservation was successful")
    except KeyboardInterrupt:
        pp("🛑 Interrupted")
    except Exception as e:
        pp("❌ Unexpected error: {}".format(e))
    finally:
        if not use_remote:
            driver.quit()


if __name__ == "__main__":
    main()
