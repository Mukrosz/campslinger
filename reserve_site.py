#!/usr/bin/env python3
"""
BC Parks reservation helper: API polling (normal mode) + Selenium for map / Reserve.
Warmode uses Selenium only — prefetch at T-1 minute, click Reserve at 7:00 (no API calls).
"""

import argparse
import os
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
    if error:
        sys.exit("{} - {}".format(current_time(), message))
    print("{} - {}".format(current_time(), message))


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


def setup_webdriver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--log-level=3")
    options.add_argument("--window-size=1920,1400")
    try:
        driver = webdriver.Chrome(
            service=ChromeService(ChromeDriverManager().install()), options=options
        )
        driver.set_page_load_timeout(60)
        return driver
    except WebDriverException as e:
        pp("❌ WebDriver failed to start: {}".format(e))
        return None


def get_available_sites(driver, url, max_attempts=5, retry_delay=1):
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

            WebDriverWait(driver, 45).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".map-container"))
            )

            WebDriverWait(driver, 45).until(
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
        except WebDriverException as e:
            pp("❌ WebDriver error: {}".format(e))
            break
        except Exception as e:
            pp("❌ Unexpected error: {}".format(e))

        time.sleep(retry_delay)

    pp("❌ Failed to read map after {} attempts".format(max_attempts))
    return {}


def collect_available_icons_from_map(driver, url):
    """Single navigation + parse; returns same dict shape as get_available_sites last attempt."""
    return get_available_sites(driver, url, max_attempts=3, retry_delay=1)


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


def reserve_normal_mode(driver, url, requested_sites, interval, debug=False):
    """
    One API poll per loop iteration (same pace as --interval). No extra requests.
    When API shows a target available, load the map once and click Reserve.
    """
    while True:
        try:
            sites = bcparks_fetch_sites_map(url)
        except Exception as e:
            pp("❌ API poll failed: {}".format(e))
            time.sleep(interval)
            continue

        target = pick_api_target(sites, requested_sites)
        if not target:
            pp("❌ No availability (API)")
            time.sleep(interval)
            continue

        label = sites[target].get("label", target)
        pp("✨ API reports available: {} — loading map…".format(label))

        on_map = collect_available_icons_from_map(driver, url)
        if target not in on_map:
            pp(
                "⚠️  API shows {} but map has no matching available icon yet; "
                "waiting {}s…".format(label, interval)
            )
            time.sleep(interval)
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

        time.sleep(interval)


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

    available_sites = get_available_sites(driver, url)
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


def build_arg_parser():
    description = (
        "Place a BC Parks campsite hold via the booking map (Selenium).\n"
        "Normal mode: poll availability via the public API every --interval (two GETs per poll), "
        "then use the browser only to click.\n"
        "Warmode (--w): Selenium only — prefetch at 1 minute before 7:00 in --timezone, "
        "click Reserve at 7:00 (no API).\n"
        "README: https://github.com/Mukrosz/campslinger\n\n"
        "Examples:\n"
        "  ./reserve_site.py --url 'https://camping.bcparks.ca/create-booking/...'\n"
        "  ./reserve_site.py --url '...' --f 'S51,S52'\n"
        "  ./reserve_site.py --url '...' --f 'S51' --warmode\n"
        "  google-chrome --user-data-dir=$HOME/.bcparks-profile "
        "--remote-debugging-port=9222 ...\n"
        "  ./reserve_site.py --url '...' --rip 127.0.0.1 --rp 9222 --f 'S51'\n"
    )
    parser = argparse.ArgumentParser(
        description=description, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--url", required=True, help="create-booking results URL")
    parser.add_argument(
        "--interval",
        "--i",
        type=int,
        default=60,
        help="Seconds between API polls in normal mode (default: 60)",
    )
    parser.add_argument(
        "--filter",
        "--f",
        type=comma_separated_list,
        help="Preferred sites, comma-separated (order matters)",
    )
    parser.add_argument("--sms", "--s", action="store_true", help="SMS on success")
    parser.add_argument("--twilio_sid", "--tsid", default="")
    parser.add_argument("--twilio_auth_token", "--tat", default="")
    parser.add_argument("--twilio_number", "--tn", default="")
    parser.add_argument("--my_phone_number", "--mpn", default="")
    parser.add_argument(
        "--warmode",
        "--w",
        action="store_true",
        help="7:00 AM reserve; prefetch at 6:59 (Selenium only, no API)",
    )
    parser.add_argument("--debug", "--d", action="store_true")
    parser.add_argument(
        "--timezone", default="US/Pacific", help="Warmode timezone (default US/Pacific)"
    )
    parser.add_argument("--remote_ip", "--rip", help="Chrome remote debugging host")
    parser.add_argument(
        "--remote_port", "--rp", type=int, help="Chrome remote debugging port"
    )
    return parser


def main():
    args = build_arg_parser().parse_args()

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
        driver = setup_webdriver()

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
