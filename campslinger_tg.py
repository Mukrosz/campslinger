#!/usr/bin/env python3
"""
Campslinger Telegram bot (campslinger_tg.py): BC Parks monitoring and optional reservation via Telegram.

- ``/watch`` / ``/monitor``: API-only monitoring (optional SMS). Post-hit poll limit: per-job
  ``--after-hit N``, or bot default from ``/set_after_hit`` (stored in prefs file).
- ``/reserve``: same Selenium automation as reserve_tg.py when reservation is enabled
  (``/set_reserve on``); optional host ``--rip``/``--rp`` for LAN Chrome attach.

Prefs path: env ``CAMPSLINGER_TG_PREFS`` or ``./campslinger_tg_prefs.json`` (allowlisted users
can change via ``/settings``, ``/set_reserve``, ``/set_after_hit``).

Warmode uses 07:00 US/Pacific; there is no Telegram timezone option.
"""

import argparse
import asyncio
import json
import os
import random
import re
import shlex
import sys
import threading
import time
import traceback
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
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
# Allowed booking page host/path for user-supplied URLs (Telegram + CLI). Do not log secrets.
_BCPARKS_BOOKING_HOST = "camping.bcparks.ca"
_BCPARKS_BOOKING_PATH_PREFIX = "/create-booking/"
BCPARKS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

_LOGGER_LOCAL = threading.local()
# When False, pp() skips print() (Telegram bot mode with --no-terminal-log).
_TERMINAL_LOG_ENABLED = True
# (host, port) when the bot process was started with --rip and --rp; else None.
_REMOTE_CHROME = None
_prefs_lock = threading.Lock()
_audit_lock = threading.Lock()
_audit_write_warned = False


def _bot_prefs_path():
    p = (os.getenv("CAMPSLINGER_TG_PREFS") or "campslinger_tg_prefs.json").strip()
    return p if p else "campslinger_tg_prefs.json"


def load_bot_prefs():
    """
    Bot-wide settings (Telegram-controlled). Keys:
      reserve_enabled (bool): allow /reserve, reserve wizard, Chrome jobs.
      default_after_hit_polls (None | int): for /watch jobs without --after-hit; None = continuous.
    """
    defaults = {"reserve_enabled": True, "default_after_hit_polls": None}
    path = _bot_prefs_path()
    with _prefs_lock:
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return dict(defaults)
        except (OSError, json.JSONDecodeError, TypeError):
            return dict(defaults)
        if not isinstance(raw, dict):
            return dict(defaults)
        out = dict(defaults)
        if "reserve_enabled" in raw:
            out["reserve_enabled"] = bool(raw["reserve_enabled"])
        v = raw.get("default_after_hit_polls")
        if v is None:
            out["default_after_hit_polls"] = None
        elif isinstance(v, int) and v >= 1:
            out["default_after_hit_polls"] = v
        return out


def save_bot_prefs(prefs):
    path = _bot_prefs_path()
    tmp = path + ".tmp"
    with _prefs_lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(prefs, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)


def get_reserve_enabled():
    return load_bot_prefs()["reserve_enabled"]


def get_default_after_hit_polls():
    return load_bot_prefs()["default_after_hit_polls"]


def validate_bcparks_booking_url(url):
    """
    Reject non-BC-Parks URLs before HTTP or browser navigation (SSRF hardening).
    """
    if not url or not isinstance(url, str):
        raise ValueError("Missing booking URL")
    u = url.strip()
    p = urlparse(u)
    if (p.scheme or "").lower() != "https":
        raise ValueError("Booking URL must use https")
    host = (p.hostname or "").lower()
    if host != _BCPARKS_BOOKING_HOST:
        raise ValueError("Booking URL must be on camping.bcparks.ca")
    path = p.path or ""
    if not path.startswith(_BCPARKS_BOOKING_PATH_PREFIX):
        raise ValueError("Booking URL path must start with {}".format(_BCPARKS_BOOKING_PATH_PREFIX))
    if p.username or p.password:
        raise ValueError("Booking URL must not contain embedded credentials")
    if p.port not in (None, 443):
        raise ValueError("Booking URL must use default HTTPS port")
    return u


def audit_log(action, user_id=None, chat_id=None, **fields):
    """
    Append one JSON line per event (user_id, action, ...). Never pass tokens or API keys.
    Path: CAMPSLINGER_AUDIT_LOG env (default campslinger_telegram_audit.log in cwd).
    """
    path = (os.getenv("CAMPSLINGER_AUDIT_LOG") or "campslinger_telegram_audit.log").strip()
    if not path:
        return
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), "action": action}
    if user_id is not None:
        rec["user_id"] = user_id
    if chat_id is not None:
        rec["chat_id"] = chat_id
    for k, v in fields.items():
        if v is not None:
            rec[k] = v
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    global _audit_write_warned
    try:
        with _audit_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
    except OSError as e:
        if not _audit_write_warned:
            _audit_write_warned = True
            print("Warning: audit log write failed: {}".format(e), file=sys.stderr, flush=True)


def set_log_callback(callback):
    _LOGGER_LOCAL.callback = callback


def set_telegram_job_meta(job_id, interval_seconds, park_name=None, interval_jitter_seconds=0):
    """Per-job thread: job id, interval, optional park label; /cancel footer on poll messages."""
    _LOGGER_LOCAL.telegram_job_id = job_id
    _LOGGER_LOCAL.telegram_interval = int(interval_seconds)
    _LOGGER_LOCAL.telegram_interval_jitter = max(0, int(interval_jitter_seconds or 0))
    _LOGGER_LOCAL.park_name = (park_name or "").strip() or None
    _LOGGER_LOCAL.poll_digest = None


def set_terminal_log_enabled(enabled):
    global _TERMINAL_LOG_ENABLED
    _TERMINAL_LOG_ENABLED = bool(enabled)


def _bot_console_line(message):
    """Log to stderr for bot lifecycle (respects set_terminal_log_enabled)."""
    if _TERMINAL_LOG_ENABLED:
        print("{} - {}".format(current_time(), message), flush=True)


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


def _telegram_poll_footer():
    iv = getattr(_LOGGER_LOCAL, "telegram_interval", 60)
    ij = getattr(_LOGGER_LOCAL, "telegram_interval_jitter", 0)
    jid = getattr(_LOGGER_LOCAL, "telegram_job_id", "") or ""
    cancel_line = "/cancel {}".format(jid) if jid else "/cancel"
    cadence = "Polling about every {}s".format(iv)
    if ij > 0:
        cadence += " ({} - {}s)".format(max(1, iv - ij), iv + ij)
    return (
        "\n\n{}. To stop this job use respective Cancel button above or send:\n{}".format(
            cadence, cancel_line
        )
    )


def pp(message, error=False, telegram_digest=None, skip_telegram=False):
    """
    Log a line. If telegram_digest is a tuple, Telegram mirror is sent only when the
    digest changes (dedupe routine poll status). Some digest kinds append a /cancel
    footer (see set below). telegram_digest None = always mirror to Telegram (if callback).
    skip_telegram=True: print to terminal only; do not notify Telegram or change poll_digest.
    """
    park = getattr(_LOGGER_LOCAL, "park_name", None)
    if park:
        line = "{} - [{}] {}".format(current_time(), park, message)
    else:
        line = "{} - {}".format(current_time(), message)
    callback = getattr(_LOGGER_LOCAL, "callback", None)
    if callback and not skip_telegram:
        send_telegram = True
        if telegram_digest is not None:
            last = getattr(_LOGGER_LOCAL, "poll_digest", object())
            if last == telegram_digest:
                send_telegram = False
            else:
                _LOGGER_LOCAL.poll_digest = telegram_digest
        if send_telegram:
            text = line
            if telegram_digest is not None and telegram_digest[0] in (
                "zero",
                "filter_miss",
                "filter_wait",
                "no_pick",
                "no_pick_wait",
                "map_wait",
            ):
                text += _telegram_poll_footer()
            try:
                callback(text)
            except Exception:
                pass
    elif not callback and telegram_digest is not None and not skip_telegram:
        _LOGGER_LOCAL.poll_digest = telegram_digest
    if error:
        sys.exit(line)
    if _TERMINAL_LOG_ENABLED:
        print(line, flush=True)


def shorten_url(url):
    try:
        import pyshorteners

        return pyshorteners.Shortener().tinyurl.short(url)
    except Exception:
        return url


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
    validate_bcparks_booking_url(booking_url)
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
    Best-effort park / location label using /api/resourcelocation?resourceLocationId=...
    Returns None if the response shape differs or the request fails.
    """
    try:
        validate_bcparks_booking_url(booking_url)
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


def randomized_probe_wait_seconds(interval_seconds, jitter_seconds):
    base = max(1, int(interval_seconds))
    spread = max(0, int(jitter_seconds or 0))
    if spread == 0:
        return base
    low = max(1, base - spread)
    high = base + spread
    return random.randint(low, high)


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


def labels_available_matching_filter(sites, requested_sites):
    """
    Sorted display labels for sites with API status == 0, optionally restricted to
    requested_sites (lowercased keys).
    """
    out = []
    for key in sorted(sites.keys(), key=sort_key):
        if sites[key].get("status") != 0:
            continue
        if requested_sites and key not in requested_sites:
            continue
        out.append(sites[key].get("label", key))
    return out


def setup_webdriver():
    options = Options()
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


def setup_webdriver_remote(ip, port):
    """
    Attach to Chrome started with --remote-debugging-port (operator-only; same LAN as server).
    ChromeDriver on PATH must match the Chrome major version.
    """
    options = Options()
    options.add_experimental_option("debuggerAddress", "{}:{}".format(ip, port))
    try:
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(120)
        try:
            driver.execute_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
        except Exception:
            pass
        return driver
    except WebDriverException as e:
        pp("❌ Failed to connect to existing Chrome instance: {}".format(e))
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


def get_available_sites(
    driver, url, max_attempts=5, retry_delay=1, debug=False, stop_event=None
):
    """
    Selenium: map icons with class icon-available -> {label_lower: icon_element}.
    Normal mode uses this after API says a site is free; warmode uses it at prefetch.
    """
    for attempt in range(max_attempts):
        if stop_event and stop_event.is_set():
            pp("🛑 Cancellation requested")
            return {}
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
                if stop_event and stop_event.is_set():
                    pp("🛑 Cancellation requested")
                    return {}
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


def collect_available_icons_from_map(driver, url, debug=False, stop_event=None):
    """Single navigation + parse; returns same dict shape as get_available_sites last attempt."""
    return get_available_sites(
        driver, url, max_attempts=3, retry_delay=1, debug=debug, stop_event=stop_event
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


def reserve_normal_mode(
    driver, url, requested_sites, interval, interval_jitter=10, debug=False, stop_event=None
):
    """
    One API poll per loop iteration. Sleep between iterations is randomized around --interval
    using jitter seconds (default 10), so cadence is less predictable.
    When API shows a target available, load the map once and click Reserve.
    """
    while True:
        wait_s = randomized_probe_wait_seconds(interval, interval_jitter)
        if stop_event and stop_event.is_set():
            pp("🛑 Cancellation requested")
            return ""
        try:
            sites = bcparks_fetch_sites_map(url)
        except Exception as e:
            pp(
                "❌ API poll failed: {}".format(e),
                telegram_digest=("api_err", str(e)[:220]),
            )
            if stop_event and stop_event.wait(wait_s):
                pp("🛑 Cancellation requested")
                return ""
            time.sleep(0 if stop_event else wait_s)
            continue

        avail = api_available_labels(sites)
        if not avail:
            pp("❌ No availability", telegram_digest=("zero",))
            if stop_event and stop_event.wait(wait_s):
                pp("🛑 Cancellation requested")
                return ""
            time.sleep(0 if stop_event else wait_s)
            continue

        target = pick_api_target(sites, requested_sites)
        if not target:
            labels_csv = ",".join(sorted(avail, key=sort_key))
            if requested_sites:
                # Single digest for "have availability, not our sites" so we do not alternate
                # digests with a separate avail line every poll (which re-sent Telegram every interval).
                pp(
                    "✨ Available sites: {}\n"
                    "❌ None of your preferred sites ({}) are free.".format(
                        labels_csv, ",".join(requested_sites)
                    ),
                    telegram_digest=("filter_wait", frozenset(avail), tuple(requested_sites)),
                )
            else:
                pp(
                    "✨ Available sites: {}\n"
                    "❌ Could not pick a target site; retrying…".format(labels_csv),
                    telegram_digest=("no_pick_wait", frozenset(avail)),
                )
            if stop_event and stop_event.wait(wait_s):
                pp("🛑 Cancellation requested")
                return ""
            time.sleep(0 if stop_event else wait_s)
            continue

        # Proceeding to map: console-only avail line so poll_digest is not clobbered before a hit.
        pp(
            "✨ Available sites: {}".format(",".join(sorted(avail, key=sort_key))),
            skip_telegram=True,
        )
        label = sites[target].get("label", target)
        pp("🎯 Trying map + Reserve for: {} …".format(label))

        on_map = collect_available_icons_from_map(
            driver, url, debug=debug, stop_event=stop_event
        )
        if target not in on_map:
            pp(
                "⚠️  API shows {} but map has no matching available icon yet; "
                "waiting {}s (base {}s, jitter ±{}s)…".format(
                    wait_s, interval, max(0, int(interval_jitter or 0))
                ),
                telegram_digest=("map_wait", label),
            )
            if stop_event and stop_event.wait(wait_s):
                pp("🛑 Cancellation requested")
                return ""
            time.sleep(0 if stop_event else wait_s)
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
            pp(
                "❌ Could not prepare reservation",
                telegram_digest=("prep_fail", label),
            )

        if stop_event and stop_event.wait(wait_s):
            pp("🛑 Cancellation requested")
            return ""
        time.sleep(0 if stop_event else wait_s)


def reserve_war_mode(
    driver, url, requested_sites, timezone="US/Pacific", debug=False, stop_event=None
):
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
            if stop_event and stop_event.is_set():
                return False
            now = datetime.now(tz=target_time.tzinfo)
            if now >= target_time:
                return True
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
        "⚔️  Warmode: prefetch at {} US/Pacific (07:00 window)…".format(
            fmt_ampm(target_time - timedelta(minutes=1))
        )
    )
    if not wait_until(target_time - timedelta(minutes=1)):
        pp("🛑 Cancellation requested")
        return ""

    available_sites = get_available_sites(driver, url, debug=debug, stop_event=stop_event)
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
    if not wait_until(target_time):
        pp("🛑 Cancellation requested")
        return ""

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


@dataclass
class JobState:
    job_id: str
    chat_id: int
    user_id: int
    args: argparse.Namespace
    started_at: str = field(default_factory=current_time)
    ended_at: Optional[str] = None
    status: str = "running"
    result_site: Optional[str] = None
    error: Optional[str] = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: Optional[threading.Thread] = None


class JobManager:
    def __init__(self, max_concurrent=3, recent_max=40):
        self.max_concurrent = max_concurrent
        self.recent_max = recent_max
        self._lock = threading.Lock()
        self.active = {}
        self.recent = deque(maxlen=recent_max)

    def create(self, chat_id, user_id, args):
        with self._lock:
            if len(self.active) >= self.max_concurrent:
                return None
            job_id = uuid.uuid4().hex[:8]
            job = JobState(job_id=job_id, chat_id=chat_id, user_id=user_id, args=args)
            self.active[job_id] = job
            return job

    def mark_done(self, job_id, status, site=None, error=None):
        with self._lock:
            job = self.active.pop(job_id, None)
            if not job:
                return
            job.status = status
            job.result_site = site
            job.error = error
            job.ended_at = current_time()
            self.recent.appendleft(job)

    def cancel(self, job_id):
        with self._lock:
            job = self.active.get(job_id)
            if not job:
                return False
            job.stop_event.set()
            return True

    def get(self, job_id):
        with self._lock:
            if job_id in self.active:
                return self.active[job_id]
            for job in self.recent:
                if job.job_id == job_id:
                    return job
            return None

    def list_active(self):
        with self._lock:
            return list(self.active.values())

    def list_recent(self, count=10):
        with self._lock:
            return list(self.recent)[:count]

    def get_for_user(self, job_id, user_id):
        """Return job if it exists and belongs to user_id; else None (no leak of other users' jobs)."""
        job = self.get(job_id)
        if not job or job.user_id != user_id:
            return None
        return job

    def list_active_for_user(self, user_id):
        with self._lock:
            return [j for j in self.active.values() if j.user_id == user_id]

    def list_recent_for_user(self, user_id, count=10):
        with self._lock:
            out = []
            for job in self.recent:
                if job.user_id == user_id:
                    out.append(job)
                    if len(out) >= count:
                        break
            return out

    def cancel_for_user(self, job_id, user_id):
        with self._lock:
            job = self.active.get(job_id)
            if not job or job.user_id != user_id:
                return False
            job.stop_event.set()
            return True


def build_telegram_arg_parser():
    p = argparse.ArgumentParser(
        description=(
            "Campslinger Telegram bot: BC Parks monitoring and reservation (see Telegram /settings)."
        )
    )
    p.add_argument(
        "--max-concurrent",
        type=int,
        default=3,
        help="Max concurrent jobs (monitor + reserve combined).",
    )
    p.add_argument(
        "--no-terminal-log",
        action="store_false",
        dest="terminal_log",
        default=True,
        help="Do not print job lines to the server terminal.",
    )
    p.add_argument(
        "--rip",
        "--remote_ip",
        dest="remote_ip",
        default=None,
        metavar="HOST",
        help=(
            "Operator only: attach to Chrome remote debugging on this host. "
            "Must be reachable on the LAN where this bot runs (not a Telegram option). "
            "Use with --rp."
        ),
    )
    p.add_argument(
        "--rp",
        "--remote_port",
        dest="remote_port",
        type=int,
        default=None,
        metavar="PORT",
        help="Operator only: remote debugging port (e.g. 9222). Use with --rip.",
    )
    return p


class _BotCommandParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def build_bot_reserve_parser():
    parser = _BotCommandParser(add_help=False)
    parser.add_argument("--url", "--u", dest="url", required=False)
    parser.add_argument("--interval", "--i", type=int, default=60)
    parser.add_argument("--jitter", "--interval-jitter", "--ij", type=int, default=10)
    parser.add_argument("--filter", "--f", type=comma_separated_list, required=False)
    parser.add_argument("--sms", "--s", action="store_true", default=False)
    parser.add_argument("--twilio_sid", "--tsid", default="")
    parser.add_argument("--twilio_auth_token", "--tat", default="")
    parser.add_argument("--twilio_number", "--tn", default="")
    parser.add_argument("--my_phone_number", "--mpn", default="")
    parser.add_argument("--warmode", "--w", action="store_true", default=False)
    parser.add_argument("--debug", "--d", action="store_true", default=False)
    return parser


def parse_bot_reserve_args(raw_text):
    tokens = shlex.split(raw_text)
    if tokens and not tokens[0].startswith("-") and tokens[0].startswith("http"):
        tokens = ["--url", tokens[0]] + tokens[1:]
    parser = build_bot_reserve_parser()
    args = parser.parse_args(tokens)
    if not args.url:
        raise ValueError("Missing URL. Usage: /reserve <url> [--f ... --i ...]")
    validate_bcparks_booking_url(args.url)
    args.job_kind = "reserve"
    return args


def build_bot_watch_parser():
    parser = _BotCommandParser(add_help=False)
    parser.add_argument("--url", "--u", dest="url", required=False)
    parser.add_argument("--interval", "--i", type=int, default=60)
    parser.add_argument("--jitter", "--interval-jitter", "--ij", type=int, default=10)
    parser.add_argument("--filter", "--f", type=comma_separated_list, required=False)
    parser.add_argument(
        "--after-hit",
        "--after_hit",
        dest="after_hit",
        type=int,
        default=None,
    )
    parser.add_argument("--sms", "--s", action="store_true", default=False)
    parser.add_argument("--twilio_sid", "--tsid", default="")
    parser.add_argument("--twilio_auth_token", "--tat", default="")
    parser.add_argument("--twilio_number", "--tn", default="")
    parser.add_argument("--my_phone_number", "--mpn", default="")
    return parser


def parse_bot_watch_args(raw_text):
    tokens = shlex.split(raw_text)
    if tokens and not tokens[0].startswith("-") and tokens[0].startswith("http"):
        tokens = ["--url", tokens[0]] + tokens[1:]
    parser = build_bot_watch_parser()
    args = parser.parse_args(tokens)
    if not args.url:
        raise ValueError(
            "Missing URL. Usage: /watch <url> [--f S51,S52] [--i 60] [--jitter 10] [--after-hit N] [--sms + Twilio flags]"
        )
    validate_bcparks_booking_url(args.url)
    if args.after_hit is not None and args.after_hit < 1:
        raise ValueError("--after-hit must be >= 1")
    args.job_kind = "monitor"
    if args.after_hit is not None:
        args.effective_after_hit_polls = args.after_hit
    else:
        args.effective_after_hit_polls = get_default_after_hit_polls()
    return args


# Telegram UI: callback_data must stay short (64-byte limit).
UD_PENDING = "csl_pending"
UD_RESERVE = "csl_reserve"


def _main_menu_keyboard():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    row2 = [
        InlineKeyboardButton("🛑 /cancel", callback_data="m:c"),
        InlineKeyboardButton("👁 /watch", callback_data="m:w"),
    ]
    if get_reserve_enabled():
        row2.append(InlineKeyboardButton("⛺ /reserve", callback_data="m:r"))
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📋 /jobs", callback_data="m:j"),
                InlineKeyboardButton("🔎 /status", callback_data="m:s"),
            ],
            row2,
            [InlineKeyboardButton("❓ /help", callback_data="m:h")],
        ]
    )


def _job_control_keyboard(job_id):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🛑 Cancel", callback_data="j:c:{}".format(job_id)),
                InlineKeyboardButton("📊 Status", callback_data="j:s:{}".format(job_id)),
            ],
            [InlineKeyboardButton("📋 Jobs", callback_data="j:l")],
        ]
    )


def _reserve_go_more_keyboard():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("▶️ Go", callback_data="r:g"),
                InlineKeyboardButton("⚙️ More", callback_data="r:m"),
            ],
            [InlineKeyboardButton("❌ Cancel wizard", callback_data="r:x")],
        ]
    )


def _reserve_more_menu_keyboard():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Sites (--f)", callback_data="r:o:f")],
            [InlineKeyboardButton("Interval (--i)", callback_data="r:o:i")],
            [InlineKeyboardButton("Jitter secs (--jitter)", callback_data="r:o:j")],
            [
                InlineKeyboardButton("🌅 Warmode", callback_data="r:o:w"),
                InlineKeyboardButton("🐛 Debug", callback_data="r:o:d"),
            ],
            [InlineKeyboardButton("▶ Run", callback_data="r:e")],
            [
                InlineKeyboardButton("🔙 Back", callback_data="r:b"),
                InlineKeyboardButton("🔄 Reset", callback_data="r:z"),
            ],
        ]
    )


def _clear_user_flow(context):
    context.user_data.pop(UD_PENDING, None)
    context.user_data.pop(UD_RESERVE, None)


def _default_reserve_state(url):
    return {
        "url": url,
        "interval": 60,
        "jitter": 10,
        "filter": None,
        "warmode": False,
        "debug": False,
        "sms": False,
        "twilio_sid": "",
        "twilio_auth_token": "",
        "twilio_number": "",
        "my_phone_number": "",
    }


def format_reserve_command_preview(rb):
    parts = ["/reserve", rb["url"]]
    if rb.get("filter"):
        parts.append("--f {}".format(",".join(rb["filter"])))
    iv = int(rb.get("interval") or 60)
    if iv != 60:
        parts.append("--i {}".format(iv))
    jv = max(0, int(rb.get("jitter") if rb.get("jitter") is not None else 10))
    if jv != 10:
        parts.append("--jitter {}".format(jv))
    if rb.get("warmode"):
        parts.append("--warmode")
    if rb.get("debug"):
        parts.append("--debug")
    if rb.get("sms"):
        parts.append("--sms …")
    return " ".join(parts)


def reserve_state_to_shlex_raw(rb):
    """Build a string parse_bot_reserve_args can shlex.split."""
    chunks = [shlex.quote(rb["url"])]
    if rb.get("filter"):
        chunks.append("--f")
        chunks.append(shlex.quote(",".join(rb["filter"])))
    iv = int(rb.get("interval") or 60)
    if iv != 60:
        chunks.append("--i")
        chunks.append(str(iv))
    jv = max(0, int(rb.get("jitter") if rb.get("jitter") is not None else 10))
    if jv != 10:
        chunks.append("--jitter")
        chunks.append(str(jv))
    if rb.get("warmode"):
        chunks.append("--warmode")
    if rb.get("debug"):
        chunks.append("--debug")
    return " ".join(chunks)


def _send_telegram_text(bot, loop, chat_id, text, reply_markup=None):
    async def _send():
        kw = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            kw["reply_markup"] = reply_markup
        await bot.send_message(**kw)

    fut = asyncio.run_coroutine_threadsafe(_send(), loop)
    try:
        fut.result(timeout=8)
    except Exception:
        pass


def _run_monitor_job(job, manager, bot, loop):
    """API-only monitoring; optional SMS. No Selenium."""

    def _tg(line):
        _send_telegram_text(bot, loop, job.chat_id, line)

    set_log_callback(_tg)
    park = bcparks_fetch_park_name(job.args.url)
    set_telegram_job_meta(
        job.job_id,
        job.args.interval,
        park_name=park,
        interval_jitter_seconds=getattr(job.args, "jitter", 0),
    )
    args = job.args
    client = None
    if args.sms:
        try:
            from twilio.rest import Client

            client = Client(args.twilio_sid, args.twilio_auth_token)
        except ImportError:
            _send_telegram_text(bot, loop, job.chat_id, "❌ Twilio module not installed")
            manager.mark_done(job.job_id, "error", error="missing_twilio")
            audit_log(
                "job_aborted",
                user_id=job.user_id,
                chat_id=job.chat_id,
                job_id=job.job_id,
                reason="missing_twilio",
                url=args.url,
            )
            set_log_callback(None)
            return

    extra = getattr(args, "effective_after_hit_polls", None)
    hit_seen = False
    post_remaining = None

    try:
        while True:
            if job.stop_event.is_set():
                break
            was_hit = hit_seen
            wait_s = randomized_probe_wait_seconds(args.interval, args.jitter)
            try:
                sites = bcparks_fetch_sites_map(args.url)
            except Exception as e:
                pp(
                    "❌ API poll failed: {}".format(e),
                    telegram_digest=("api_err", str(e)[:220]),
                )
                if job.stop_event.wait(wait_s):
                    break
                continue

            matching = labels_available_matching_filter(sites, args.filter)
            all_avail = api_available_labels(sites)

            if matching:
                line = "✅ Available sites: {}".format(",".join(matching))
                pp(line, telegram_digest=None)
                if args.sms and client:
                    try:
                        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        prefix = "[{}] ".format(park) if park else ""
                        body = "{} - {}Available sites: {}\n{}".format(
                            ts, prefix, ",".join(matching), shorten_url(args.url)
                        )
                        send_sms(body, client, args.my_phone_number, args.twilio_number)
                    except Exception as e:
                        pp("❌ SMS failed: {}".format(e), telegram_digest=None)
                if not hit_seen:
                    hit_seen = True
                    if extra is not None:
                        post_remaining = int(extra)
                        pp(
                            "⏱️  Monitor: {} more poll(s) after this hit, then stop.".format(
                                post_remaining
                            ),
                            telegram_digest=None,
                        )
            elif not all_avail:
                pp("No availability", telegram_digest=("zero",))
            else:
                labels_csv = ",".join(sorted(all_avail, key=sort_key))
                pp(
                    "✨ Available sites: {}\n"
                    "❌ None of your preferred sites ({}) are free.".format(
                        labels_csv, ",".join(args.filter or [])
                    ),
                    telegram_digest=(
                        "filter_wait",
                        frozenset(all_avail),
                        tuple(args.filter or ()),
                    ),
                )

            if hit_seen and extra is not None and was_hit:
                post_remaining -= 1
                if post_remaining <= 0:
                    pp("✅ Monitor job finished (post-hit poll limit reached).", telegram_digest=None)
                    manager.mark_done(
                        job.job_id,
                        "done",
                        site=",".join(matching) if matching else None,
                    )
                    audit_log(
                        "job_finished",
                        user_id=job.user_id,
                        chat_id=job.chat_id,
                        job_id=job.job_id,
                        status="done",
                        url=args.url,
                        job_kind="monitor",
                    )
                    set_log_callback(None)
                    return

            if job.stop_event.wait(wait_s):
                break

        if job.stop_event.is_set():
            manager.mark_done(job.job_id, "cancelled")
            _send_telegram_text(bot, loop, job.chat_id, "🛑 Job {} cancelled".format(job.job_id))
            audit_log(
                "job_finished",
                user_id=job.user_id,
                chat_id=job.chat_id,
                job_id=job.job_id,
                status="cancelled",
                url=args.url,
                job_kind="monitor",
            )
        else:
            manager.mark_done(job.job_id, "done")
            audit_log(
                "job_finished",
                user_id=job.user_id,
                chat_id=job.chat_id,
                job_id=job.job_id,
                status="done",
                url=args.url,
                job_kind="monitor",
            )
    except Exception as e:
        pp("❌ Monitor job error: {}".format(e), telegram_digest=None)
        manager.mark_done(job.job_id, "error", error=str(e))
        audit_log(
            "job_finished",
            user_id=job.user_id,
            chat_id=job.chat_id,
            job_id=job.job_id,
            status="error",
            error=str(e),
            url=args.url,
            job_kind="monitor",
        )
        _send_telegram_text(bot, loop, job.chat_id, "❌ Job {} error: {}".format(job.job_id, e))
    finally:
        set_log_callback(None)


def _run_job_dispatch(job, manager, bot, loop):
    if getattr(job.args, "job_kind", "reserve") == "monitor":
        _run_monitor_job(job, manager, bot, loop)
    else:
        _run_job(job, manager, bot, loop)


def _run_job(job, manager, bot, loop):
    def _tg(line):
        _send_telegram_text(bot, loop, job.chat_id, line)

    set_log_callback(_tg)
    park = bcparks_fetch_park_name(job.args.url)
    set_telegram_job_meta(
        job.job_id,
        job.args.interval,
        park_name=park,
        interval_jitter_seconds=getattr(job.args, "jitter", 0),
    )
    args = job.args
    client = None
    if args.sms:
        try:
            from twilio.rest import Client

            client = Client(args.twilio_sid, args.twilio_auth_token)
        except ImportError:
            _send_telegram_text(bot, loop, job.chat_id, "❌ Twilio module not installed")
            manager.mark_done(job.job_id, "error", error="missing_twilio")
            audit_log(
                "job_aborted",
                user_id=job.user_id,
                chat_id=job.chat_id,
                job_id=job.job_id,
                reason="missing_twilio",
                url=args.url,
            )
            set_log_callback(None)
            return

    if _REMOTE_CHROME:
        driver = setup_webdriver_remote(_REMOTE_CHROME[0], _REMOTE_CHROME[1])
    else:
        driver = setup_webdriver()
    if not driver:
        _send_telegram_text(bot, loop, job.chat_id, "❌ WebDriver initialization failed")
        manager.mark_done(job.job_id, "error", error="webdriver_init_failed")
        audit_log(
            "job_aborted",
            user_id=job.user_id,
            chat_id=job.chat_id,
            job_id=job.job_id,
            reason="webdriver_init_failed",
            url=args.url,
        )
        set_log_callback(None)
        return

    try:
        if args.warmode:
            reserved_site = reserve_war_mode(
                driver,
                args.url,
                args.filter,
                timezone="US/Pacific",
                debug=args.debug,
                stop_event=job.stop_event,
            )
        else:
            reserved_site = reserve_normal_mode(
                driver,
                args.url,
                args.filter,
                interval=args.interval,
                interval_jitter=args.jitter,
                debug=args.debug,
                stop_event=job.stop_event,
            )

        if job.stop_event.is_set():
            manager.mark_done(job.job_id, "cancelled")
            _send_telegram_text(bot, loop, job.chat_id, "🛑 Job {} cancelled".format(job.job_id))
            audit_log(
                "job_finished",
                user_id=job.user_id,
                chat_id=job.chat_id,
                job_id=job.job_id,
                status="cancelled",
                url=args.url,
            )
            return

        if reserved_site:
            if args.sms:
                send_sms(
                    "{} - 🎯 Reserved: {}\n{}".format(
                        current_time(), reserved_site, shorten_url(args.url)
                    ),
                    client,
                    args.my_phone_number,
                    args.twilio_number,
                )
            manager.mark_done(job.job_id, "success", site=reserved_site)
            _send_telegram_text(
                bot,
                loop,
                job.chat_id,
                "✅ Job {} success. Reserved: {}".format(job.job_id, reserved_site),
            )
            audit_log(
                "job_finished",
                user_id=job.user_id,
                chat_id=job.chat_id,
                job_id=job.job_id,
                status="success",
                result_site=reserved_site,
                url=args.url,
            )
        else:
            manager.mark_done(job.job_id, "failed", error="no_reservation")
            _send_telegram_text(
                bot, loop, job.chat_id, "❌ Job {} finished without reservation".format(job.job_id)
            )
            audit_log(
                "job_finished",
                user_id=job.user_id,
                chat_id=job.chat_id,
                job_id=job.job_id,
                status="failed",
                url=args.url,
            )
    except Exception as e:
        manager.mark_done(job.job_id, "error", error=str(e))
        _send_telegram_text(bot, loop, job.chat_id, "❌ Job {} error: {}".format(job.job_id, e))
        audit_log(
            "job_finished",
            user_id=job.user_id,
            chat_id=job.chat_id,
            job_id=job.job_id,
            status="error",
            error=str(e),
            url=args.url,
        )
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        set_log_callback(None)


def telegram_help_text():
    ah = get_default_after_hit_polls()
    ah_txt = "continuous" if ah is None else str(ah)
    lines = [
        "Campslinger Telegram commands",
        "",
        "/watch <url> [--f S51,S52] [--i 60] [--jitter 10] [--after-hit N] [--sms + Twilio flags]",
        "  API-only monitoring. --after-hit N: after first matching hit, N more polls then stop.",
        "  Omit --after-hit to use bot default (now: {}). Change with /set_after_hit.".format(ah_txt),
        "",
        "/reserve <url> […]  (only if reservation is on; see /settings)",
        "  Selenium map + Reserve. Warmode = 07:00 US/Pacific.",
        "",
        "/settings — show bot prefs (reserve on/off, default --after-hit)",
        "/set_reserve on|off — enable or disable /reserve and Chrome jobs",
        "/set_after_hit continuous|N — default extra polls after first hit for /watch (N >= 1)",
        "",
        "/jobs",
        "/status <job_id>",
        "/cancel <job_id>",
        "/help",
        "",
        "Tip: a plain booking URL starts a /watch job (monitor). Use /reserve … to automate Reserve.",
    ]
    return "\n".join(lines)


async def _tg_reply(update, text, reply_markup=None):
    em = update.effective_message
    if em:
        await em.reply_text(text, reply_markup=reply_markup)
        return True
    return False


def run_telegram_bot(args):
    from telegram.ext import (
        ApplicationBuilder,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )

    set_terminal_log_enabled(args.terminal_log)

    global _REMOTE_CHROME
    rip = (args.remote_ip or "").strip() if args.remote_ip else ""
    rp = args.remote_port
    if bool(rip) != (rp is not None):
        raise RuntimeError(
            "Remote Chrome requires both --rip and --rp (or neither for headless server Chrome)."
        )
    if rip and rp is not None:
        _REMOTE_CHROME = (rip, int(rp))
        _bot_console_line(
            "Using remote Chrome at {}:{} (same LAN as this server; not set from Telegram)".format(
                _REMOTE_CHROME[0], _REMOTE_CHROME[1]
            )
        )
    else:
        _REMOTE_CHROME = None

    # TELEGRAM_BOT_TOKEN must never be logged, written to audit_log, or echoed to users.
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    allowed_raw = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN env var is required")
    if not allowed_raw:
        raise RuntimeError("TELEGRAM_ALLOWED_USER_IDS env var is required")
    allowed_user_ids = {int(x.strip()) for x in allowed_raw.split(",") if x.strip()}
    manager = JobManager(max_concurrent=args.max_concurrent)
    app = ApplicationBuilder().token(token).build()

    def authorized(update):
        return update.effective_user and update.effective_user.id in allowed_user_ids

    async def reject(update):
        uid = update.effective_user.id if update.effective_user else None
        cid = update.effective_chat.id if update.effective_chat else None
        uname = update.effective_user.username if update.effective_user else None
        audit_log("unauthorized", user_id=uid, chat_id=cid, username=uname)
        await _tg_reply(update, "Unauthorized")

    async def telegram_error_handler(update, context: ContextTypes.DEFAULT_TYPE):
        err = context.error
        uid = update.effective_user.id if update and update.effective_user else None
        cid = update.effective_chat.id if update and update.effective_chat else None
        audit_log(
            "handler_error",
            user_id=uid,
            chat_id=cid,
            error_type=type(err).__name__ if err else None,
            error=str(err)[:500] if err else None,
        )
        _bot_console_line("Telegram handler error: {!r}".format(err))
        if _TERMINAL_LOG_ENABLED and err is not None:
            traceback.print_exception(type(err), err, err.__traceback__, file=sys.stderr)
        if update and authorized(update):
            em = getattr(update, "effective_message", None)
            if em:
                try:
                    await em.reply_text(
                        "Error handling this update. If it persists, check server logs."
                    )
                except Exception:
                    pass

    async def _start_job(update, context, raw, kind="auto"):
        if not authorized(update):
            await reject(update)
            return
        uid = update.effective_user.id if update.effective_user else "?"
        _bot_console_line("Job request user={} raw={!r}".format(uid, raw[:200] if raw else ""))
        try:
            if kind == "watch":
                job_args = parse_bot_watch_args(raw)
            elif kind == "reserve":
                if not get_reserve_enabled():
                    await _tg_reply(
                        update,
                        "Reservation is disabled. Use /set_reserve on or /watch …\n\n{}".format(
                            telegram_help_text()
                        ),
                    )
                    return
                job_args = parse_bot_reserve_args(raw)
            else:
                job_args = parse_bot_watch_args(raw)
        except Exception as e:
            audit_log(
                "job_parse_error",
                user_id=uid if isinstance(uid, int) else None,
                chat_id=update.effective_chat.id if update.effective_chat else None,
                error=str(e)[:500],
            )
            await _tg_reply(update, "Parse error: {}\n\n{}".format(e, telegram_help_text()))
            return
        job = manager.create(update.effective_chat.id, update.effective_user.id, job_args)
        if not job:
            audit_log(
                "job_rejected_busy",
                user_id=uid if isinstance(uid, int) else None,
                chat_id=update.effective_chat.id if update.effective_chat else None,
                max_concurrent=manager.max_concurrent,
            )
            await _tg_reply(
                update,
                "Server busy: max {} concurrent jobs reached.".format(manager.max_concurrent),
            )
            return
        audit_log(
            "job_queued",
            user_id=job.user_id,
            chat_id=job.chat_id,
            job_id=job.job_id,
            url=job_args.url,
            job_kind=getattr(job_args, "job_kind", "reserve"),
            warmode=bool(getattr(job_args, "warmode", False)),
            interval=job_args.interval,
            filter=",".join(job_args.filter) if job_args.filter else "",
            after_hit=getattr(job_args, "effective_after_hit_polls", None),
        )
        loop = asyncio.get_running_loop()
        t = threading.Thread(
            target=_run_job_dispatch, args=(job, manager, app.bot, loop), daemon=True
        )
        job.thread = t
        t.start()
        _bot_console_line("Started job {} for user {}".format(job.job_id, uid))
        if job_args.job_kind == "monitor":
            ah = getattr(job_args, "effective_after_hit_polls", None)
            if ah is None:
                mode = "monitor (continuous after first hit)"
            else:
                mode = "monitor ({} post-hit poll(s))".format(ah)
        else:
            mode = "warmode" if job_args.warmode else "normal reserve"
        await _tg_reply(
            update,
            "Started job {}\nkind={}\nmode={}\nfilter={}".format(
                job.job_id,
                job_args.job_kind,
                mode,
                ",".join(job_args.filter or []),
            ),
        )
        cid = update.effective_chat.id
        await context.bot.send_message(
            chat_id=cid,
            text="Job {} - quick actions while it runs:".format(job.job_id),
            reply_markup=_job_control_keyboard(job.job_id),
        )

    def _format_status_text(jid, requester_user_id):
        job = manager.get_for_user(jid, requester_user_id)
        if not job:
            return "Job not found or not yours."
        return "job={} status={} started={} ended={} result={} error={}".format(
            job.job_id,
            job.status,
            job.started_at,
            job.ended_at or "-",
            job.result_site or "-",
            job.error or "-",
        )

    async def help_cmd(update, context: ContextTypes.DEFAULT_TYPE):
        if not authorized(update):
            await reject(update)
            return
        uid = update.effective_user.id if update.effective_user else None
        _bot_console_line("/help user={}".format(uid if uid is not None else "?"))
        audit_log("command_help", user_id=uid, chat_id=update.effective_chat.id if update.effective_chat else None)
        await _tg_reply(update, telegram_help_text())
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Quick actions (tap a button):",
            reply_markup=_main_menu_keyboard(),
        )

    async def reserve_cmd(update, context: ContextTypes.DEFAULT_TYPE):
        if not authorized(update):
            await reject(update)
            return
        if not get_reserve_enabled():
            await _tg_reply(
                update,
                "Reservation is disabled. Use /set_reserve on or /watch …\n\n{}".format(
                    telegram_help_text()
                ),
                reply_markup=_main_menu_keyboard(),
            )
            return
        if context.args:
            raw = " ".join(context.args).strip()
        else:
            em = update.effective_message
            if em and em.text:
                parts = em.text.split(None, 1)
                raw = parts[1].strip() if len(parts) > 1 else ""
            else:
                raw = ""
        if not raw:
            audit_log(
                "command_reserve_usage",
                user_id=update.effective_user.id if update.effective_user else None,
                chat_id=update.effective_chat.id if update.effective_chat else None,
            )
            await _tg_reply(
                update,
                "Type /reserve with a URL and options, send a plain booking URL, or tap ⛺ /reserve below.\n\n{}".format(
                    telegram_help_text()
                ),
                reply_markup=_main_menu_keyboard(),
            )
            return
        await _start_job(update, context, raw, kind="reserve")

    async def watch_cmd(update, context: ContextTypes.DEFAULT_TYPE):
        if not authorized(update):
            await reject(update)
            return
        if context.args:
            raw = " ".join(context.args).strip()
        else:
            em = update.effective_message
            if em and em.text:
                parts = em.text.split(None, 1)
                raw = parts[1].strip() if len(parts) > 1 else ""
            else:
                raw = ""
        if not raw:
            audit_log(
                "command_watch_usage",
                user_id=update.effective_user.id if update.effective_user else None,
                chat_id=update.effective_chat.id if update.effective_chat else None,
            )
            await _tg_reply(
                update,
                "Usage: /watch <url> [--f S51,S52] [--i 60] [--jitter 10] [--after-hit N] …\n"
                "Or tap 👁 /watch and send the booking URL.\n\n{}".format(telegram_help_text()),
                reply_markup=_main_menu_keyboard(),
            )
            return
        await _start_job(update, context, raw, kind="watch")

    async def jobs_cmd(update, context: ContextTypes.DEFAULT_TYPE):
        if not authorized(update):
            await reject(update)
            return
        uid = update.effective_user.id if update.effective_user else None
        _bot_console_line("/jobs user={}".format(uid if uid is not None else "?"))
        audit_log("command_jobs", user_id=uid, chat_id=update.effective_chat.id if update.effective_chat else None)
        active = manager.list_active_for_user(uid)
        recent = manager.list_recent_for_user(uid, 10)
        lines = ["Active jobs: {}".format(len(active))]
        for job in active:
            lines.append(
                "- {} kind={} status={} started={}".format(
                    job.job_id,
                    getattr(job.args, "job_kind", "?"),
                    job.status,
                    job.started_at,
                )
            )
        lines.append("Recent jobs:")
        for job in recent:
            lines.append(
                "- {} kind={} status={} result={} error={}".format(
                    job.job_id,
                    getattr(job.args, "job_kind", "?"),
                    job.status,
                    job.result_site or "-",
                    job.error or "-",
                )
            )
        await _tg_reply(update, "\n".join(lines))

    async def status_cmd(update, context: ContextTypes.DEFAULT_TYPE):
        if not authorized(update):
            await reject(update)
            return
        uid = update.effective_user.id if update.effective_user else None
        cid = update.effective_chat.id if update.effective_chat else None
        if not context.args:
            audit_log("command_status", user_id=uid, chat_id=cid, job_id=None)
            await _tg_reply(update, "Usage: /status <job_id>")
            return
        jid = context.args[0].strip()
        job = manager.get_for_user(jid, uid)
        audit_log(
            "command_status",
            user_id=uid,
            chat_id=cid,
            job_id=jid,
            found=job is not None,
        )
        await _tg_reply(update, _format_status_text(jid, uid))

    async def cancel_cmd(update, context: ContextTypes.DEFAULT_TYPE):
        if not authorized(update):
            await reject(update)
            return
        uid = update.effective_user.id if update.effective_user else None
        cid = update.effective_chat.id if update.effective_chat else None
        if not context.args:
            audit_log("command_cancel", user_id=uid, chat_id=cid, job_id=None)
            await _tg_reply(update, "Usage: /cancel <job_id>")
            return
        jid = context.args[0].strip()
        ok = manager.cancel_for_user(jid, uid)
        audit_log(
            "command_cancel",
            user_id=uid,
            chat_id=cid,
            job_id=jid,
            accepted=ok,
        )
        if ok:
            _bot_console_line("/cancel {} user={}".format(jid, uid if uid is not None else "?"))
            await _tg_reply(update, "Cancellation requested for {}".format(jid))
        else:
            await _tg_reply(update, "Job {} not active or not yours".format(jid))

    async def settings_cmd(update, context: ContextTypes.DEFAULT_TYPE):
        if not authorized(update):
            await reject(update)
            return
        uid = update.effective_user.id if update.effective_user else None
        cid = update.effective_chat.id if update.effective_chat else None
        audit_log("command_settings", user_id=uid, chat_id=cid)
        p = load_bot_prefs()
        ah = p["default_after_hit_polls"]
        ah_s = "continuous" if ah is None else str(ah)
        await _tg_reply(
            update,
            "Bot settings ({})\nreserve_enabled: {}\ndefault_after_hit_polls: {}".format(
                _bot_prefs_path(),
                p["reserve_enabled"],
                ah_s,
            ),
        )

    async def set_reserve_cmd(update, context: ContextTypes.DEFAULT_TYPE):
        if not authorized(update):
            await reject(update)
            return
        uid = update.effective_user.id if update.effective_user else None
        cid = update.effective_chat.id if update.effective_chat else None
        if not context.args:
            audit_log("command_set_reserve_usage", user_id=uid, chat_id=cid)
            await _tg_reply(update, "Usage: /set_reserve on|off")
            return
        v = context.args[0].lower().strip()
        if v in ("on", "true", "1", "yes"):
            en = True
        elif v in ("off", "false", "0", "no"):
            en = False
        else:
            await _tg_reply(update, "Usage: /set_reserve on|off")
            return
        p = load_bot_prefs()
        p["reserve_enabled"] = en
        save_bot_prefs(p)
        audit_log("prefs_set_reserve", user_id=uid, chat_id=cid, reserve_enabled=en)
        await _tg_reply(
            update,
            "reserve_enabled={}. Use /settings to confirm.".format(en),
            reply_markup=_main_menu_keyboard(),
        )

    async def set_after_hit_cmd(update, context: ContextTypes.DEFAULT_TYPE):
        if not authorized(update):
            await reject(update)
            return
        uid = update.effective_user.id if update.effective_user else None
        cid = update.effective_chat.id if update.effective_chat else None
        if not context.args:
            audit_log("command_set_after_hit_usage", user_id=uid, chat_id=cid)
            await _tg_reply(update, "Usage: /set_after_hit continuous|N  (N >= 1)")
            return
        raw = " ".join(context.args).strip().lower()
        if raw in ("continuous", "forever", "off", "none", "no"):
            val = None
        else:
            try:
                n = int(raw.split()[0])
            except ValueError:
                await _tg_reply(update, "Usage: /set_after_hit continuous|N  (N >= 1)")
                return
            if n < 1:
                await _tg_reply(update, "N must be >= 1, or use continuous.")
                return
            val = n
        p = load_bot_prefs()
        p["default_after_hit_polls"] = val
        save_bot_prefs(p)
        audit_log(
            "prefs_set_after_hit",
            user_id=uid,
            chat_id=cid,
            default_after_hit_polls=val,
        )
        shown = "continuous" if val is None else str(val)
        await _tg_reply(
            update,
            "default_after_hit_polls={}. /watch can still override with --after-hit.".format(shown),
        )

    async def text_handler(update, context: ContextTypes.DEFAULT_TYPE):
        if not authorized(update):
            await reject(update)
            return
        em = update.effective_message
        txt = (em.text if em else "") or ""
        txt = txt.strip()
        uid = update.effective_user.id if update.effective_user else None
        cid = update.effective_chat.id if update.effective_chat else None
        pend = context.user_data.get(UD_PENDING)

        if pend in ("status", "cancel", "w_url"):
            if txt.lower() in ("abort", "stop", "nevermind"):
                context.user_data.pop(UD_PENDING, None)
                await _tg_reply(update, "Okay, cancelled.")
                return
        if pend in ("status", "cancel"):
            jid = txt.split()[0].strip()
            if pend == "status":
                context.user_data.pop(UD_PENDING, None)
                audit_log(
                    "command_status",
                    user_id=uid,
                    chat_id=cid,
                    job_id=jid,
                    found=manager.get_for_user(jid, uid) is not None,
                )
                await _tg_reply(update, _format_status_text(jid, uid))
                return
            context.user_data.pop(UD_PENDING, None)
            ok = manager.cancel_for_user(jid, uid)
            audit_log("command_cancel", user_id=uid, chat_id=cid, job_id=jid, accepted=ok)
            await _tg_reply(
                update,
                "Cancellation requested for {}".format(jid) if ok else "Job {} not active or not yours".format(jid),
            )
            return

        if pend == "r_url":
            if not (txt.startswith("http://") or txt.startswith("https://")):
                await _tg_reply(update, "Please send a URL starting with https://camping.bcparks.ca/... or type abort.")
                return
            try:
                validate_bcparks_booking_url(txt)
            except ValueError as e:
                await _tg_reply(update, "Invalid URL: {}".format(e))
                return
            context.user_data.pop(UD_PENDING, None)
            context.user_data[UD_RESERVE] = _default_reserve_state(txt)
            rb = context.user_data[UD_RESERVE]
            await _tg_reply(
                update,
                "URL saved.\n\nCurrent command:\n{}\n\n▶️ Go = run with defaults only.\n⚙️ More = add sites, interval, warmode, …".format(
                    format_reserve_command_preview(rb)
                ),
                reply_markup=_reserve_go_more_keyboard(),
            )
            return

        if pend == "r_f":
            context.user_data.pop(UD_PENDING, None)
            rb = context.user_data.get(UD_RESERVE)
            if not rb:
                await _tg_reply(update, "Wizard expired. Tap ⛺ /reserve again.")
                return
            if txt.lower() not in ("clear", "none", "-"):
                rb["filter"] = comma_separated_list(txt)
            else:
                rb["filter"] = None
            await _tg_reply(
                update,
                "Updated.\n\n{}\n\nUse More to change more options or Run.".format(
                    format_reserve_command_preview(rb)
                ),
                reply_markup=_reserve_more_menu_keyboard(),
            )
            return

        if pend == "r_i":
            context.user_data.pop(UD_PENDING, None)
            rb = context.user_data.get(UD_RESERVE)
            if not rb:
                await _tg_reply(update, "Wizard expired.")
                return
            try:
                rb["interval"] = max(5, int(txt.split()[0]))
            except ValueError:
                await _tg_reply(update, "Send a number of seconds (e.g. 60), or open More again.")
                return
            await _tg_reply(
                update,
                "Updated.\n\n{}\n\n".format(format_reserve_command_preview(rb)),
                reply_markup=_reserve_more_menu_keyboard(),
            )
            return

        if pend == "r_j":
            context.user_data.pop(UD_PENDING, None)
            rb = context.user_data.get(UD_RESERVE)
            if not rb:
                await _tg_reply(update, "Wizard expired.")
                return
            try:
                rb["jitter"] = max(0, int(txt.split()[0]))
            except ValueError:
                await _tg_reply(update, "Send jitter seconds as a non-negative integer (e.g. 10).")
                return
            await _tg_reply(
                update,
                "Updated.\n\n{}\n\n".format(format_reserve_command_preview(rb)),
                reply_markup=_reserve_more_menu_keyboard(),
            )
            return

        if pend == "w_url":
            context.user_data.pop(UD_PENDING, None)
            if not (txt.startswith("http://") or txt.startswith("https://")):
                await _tg_reply(
                    update,
                    "Please send a URL starting with https://camping.bcparks.ca/... or type abort.",
                )
                return
            try:
                validate_bcparks_booking_url(txt)
            except ValueError as e:
                await _tg_reply(update, "Invalid URL: {}".format(e))
                return
            await _start_job(update, context, txt.strip(), kind="watch")
            return

        if txt.startswith("http://") or txt.startswith("https://"):
            await _start_job(update, context, txt, kind="auto")
            return
        audit_log(
            "text_message",
            user_id=uid,
            chat_id=cid,
            preview=txt[:200],
        )
        await _tg_reply(
            update,
            "Send /help, a booking URL, or use the buttons from the last help message.",
        )

    async def callback_handler(update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        if not q:
            return
        if not authorized(update):
            await q.answer("Unauthorized", show_alert=True)
            return
        data = q.data or ""

        async def ack():
            try:
                await q.answer()
            except Exception:
                pass

        uid = update.effective_user.id if update.effective_user else None
        cid = update.effective_chat.id if update.effective_chat else None

        if data == "m:j":
            await ack()
            await jobs_cmd(update, context)
            return
        if data == "m:h":
            await ack()
            await help_cmd(update, context)
            return
        if data == "m:s":
            await ack()
            context.user_data[UD_PENDING] = "status"
            await q.message.reply_text("Reply with a job id (from /jobs), or send abort to cancel.")
            return
        if data == "m:c":
            await ack()
            context.user_data[UD_PENDING] = "cancel"
            await q.message.reply_text("Reply with a job id to cancel, or send abort to cancel.")
            return
        if data == "m:w":
            await ack()
            context.user_data.pop(UD_RESERVE, None)
            context.user_data[UD_PENDING] = "w_url"
            await q.message.reply_text(
                "Send your full BC Parks results URL for API monitoring (/watch).\n"
                "Optional: /watch <url> --f S51 --i 60 --after-hit 5"
            )
            return
        if data == "m:r":
            await ack()
            if not get_reserve_enabled():
                await q.answer("Reservation is disabled (/set_reserve off).", show_alert=True)
                return
            context.user_data.pop(UD_RESERVE, None)
            context.user_data[UD_PENDING] = "r_url"
            await q.message.reply_text(
                "Send your full BC Parks results URL (https://camping.bcparks.ca/create-booking/...).\n"
                "You can still type a full /reserve … command manually anytime."
            )
            return

        if data.startswith("j:c:"):
            await ack()
            jid = data[4:].strip()
            ok = manager.cancel_for_user(jid, uid)
            audit_log("command_cancel", user_id=uid, chat_id=cid, job_id=jid, accepted=ok)
            await q.message.reply_text(
                "Cancellation requested for {}".format(jid) if ok else "Job {} not active or not yours".format(jid)
            )
            return
        if data.startswith("j:s:"):
            await ack()
            jid = data[4:].strip()
            audit_log(
                "command_status",
                user_id=uid,
                chat_id=cid,
                job_id=jid,
                found=manager.get_for_user(jid, uid) is not None,
            )
            await q.message.reply_text(_format_status_text(jid, uid))
            return
        if data == "j:l":
            await ack()
            await jobs_cmd(update, context)
            return

        rb = context.user_data.get(UD_RESERVE)

        if data == "r:x":
            await ack()
            _clear_user_flow(context)
            await q.message.reply_text("Wizard cancelled.")
            return
        if data == "r:g":
            await ack()
            if not rb:
                await q.message.reply_text("No URL in progress. Tap ⛺ /reserve.")
                return
            raw = reserve_state_to_shlex_raw(rb)
            _clear_user_flow(context)
            await _start_job(update, context, raw, kind="reserve")
            return
        if data == "r:e":
            await ack()
            if not rb:
                await q.message.reply_text("No URL in progress.")
                return
            raw = reserve_state_to_shlex_raw(rb)
            _clear_user_flow(context)
            await _start_job(update, context, raw, kind="reserve")
            return
        if data == "r:m":
            await ack()
            if not rb:
                await q.message.reply_text("No URL in progress.")
                return
            try:
                await q.edit_message_text(
                    "{}\n\nPick an option:".format(format_reserve_command_preview(rb)),
                    reply_markup=_reserve_more_menu_keyboard(),
                )
            except Exception:
                await q.message.reply_text(
                    "{}\n\nPick an option:".format(format_reserve_command_preview(rb)),
                    reply_markup=_reserve_more_menu_keyboard(),
                )
            return
        if data == "r:b":
            await ack()
            if not rb:
                await q.message.reply_text("Wizard expired.")
                return
            try:
                await q.edit_message_text(
                    "{}\n\nGo or More:".format(format_reserve_command_preview(rb)),
                    reply_markup=_reserve_go_more_keyboard(),
                )
            except Exception:
                await q.message.reply_text(
                    "{}\n\nGo or More:".format(format_reserve_command_preview(rb)),
                    reply_markup=_reserve_go_more_keyboard(),
                )
            return
        if data == "r:z":
            await ack()
            if not rb or not rb.get("url"):
                await q.message.reply_text("Nothing to reset.")
                return
            url = rb["url"]
            context.user_data[UD_RESERVE] = _default_reserve_state(url)
            rb = context.user_data[UD_RESERVE]
            try:
                await q.edit_message_text(
                    "Reset options.\n\n{}".format(format_reserve_command_preview(rb)),
                    reply_markup=_reserve_more_menu_keyboard(),
                )
            except Exception:
                await q.message.reply_text(
                    "Reset options.\n\n{}".format(format_reserve_command_preview(rb)),
                    reply_markup=_reserve_more_menu_keyboard(),
                )
            return
        if data == "r:o:f":
            await ack()
            if not rb:
                await q.message.reply_text("Start the wizard from ⛺ /reserve.")
                return
            context.user_data[UD_PENDING] = "r_f"
            await q.message.reply_text(
                "Send preferred site labels comma-separated (e.g. S51,S52), or clear to remove --f."
            )
            return
        if data == "r:o:i":
            await ack()
            if not rb:
                return
            context.user_data[UD_PENDING] = "r_i"
            await q.message.reply_text("Send poll interval in seconds (e.g. 60). Minimum 5.")
            return
        if data == "r:o:j":
            await ack()
            if not rb:
                return
            context.user_data[UD_PENDING] = "r_j"
            await q.message.reply_text(
                "Send jitter in seconds (e.g. 10). Each probe wait will vary in [interval-jitter, interval+jitter]."
            )
            return
        if data == "r:o:w":
            await ack()
            if not rb:
                return
            rb["warmode"] = not rb.get("warmode")
            try:
                await q.edit_message_text(
                    "{}\n\nPick an option:".format(format_reserve_command_preview(rb)),
                    reply_markup=_reserve_more_menu_keyboard(),
                )
            except Exception:
                await q.message.reply_text(
                    "{}\n\nPick an option:".format(format_reserve_command_preview(rb)),
                    reply_markup=_reserve_more_menu_keyboard(),
                )
            return
        if data == "r:o:d":
            await ack()
            if not rb:
                return
            rb["debug"] = not rb.get("debug")
            try:
                await q.edit_message_text(
                    "{}\n\nPick an option:".format(format_reserve_command_preview(rb)),
                    reply_markup=_reserve_more_menu_keyboard(),
                )
            except Exception:
                await q.message.reply_text(
                    "{}\n\nPick an option:".format(format_reserve_command_preview(rb)),
                    reply_markup=_reserve_more_menu_keyboard(),
                )
            return

        await ack()

    app.add_error_handler(telegram_error_handler)
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("set_reserve", set_reserve_cmd))
    app.add_handler(CommandHandler("set_after_hit", set_after_hit_cmd))
    app.add_handler(CommandHandler("watch", watch_cmd))
    app.add_handler(CommandHandler("monitor", watch_cmd))
    app.add_handler(CommandHandler("reserve", reserve_cmd))
    app.add_handler(CommandHandler("jobs", jobs_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    _bp = load_bot_prefs()
    audit_log(
        "bot_start",
        max_concurrent=manager.max_concurrent,
        terminal_log=args.terminal_log,
        reserve_enabled=_bp.get("reserve_enabled"),
        default_after_hit_polls=_bp.get("default_after_hit_polls"),
        bot_prefs_path=_bot_prefs_path(),
        remote_chrome="{}:{}".format(_REMOTE_CHROME[0], _REMOTE_CHROME[1])
        if _REMOTE_CHROME
        else None,
        audit_log_path=(os.getenv("CAMPSLINGER_AUDIT_LOG") or "campslinger_telegram_audit.log").strip(),
    )
    _bot_console_line(
        "Telegram bot started (long polling). max_concurrent={} prefs={} terminal_log={}{}".format(
            manager.max_concurrent,
            _bot_prefs_path(),
            args.terminal_log,
            " remote_chrome={}:{}".format(_REMOTE_CHROME[0], _REMOTE_CHROME[1])
            if _REMOTE_CHROME
            else "",
        )
    )
    app.run_polling(drop_pending_updates=True)


def main():
    run_telegram_bot(build_telegram_arg_parser().parse_args())


if __name__ == "__main__":
    main()
