#!/usr/bin/env python3
"""
BC Parks reservation helper: API polling (normal mode) + Selenium for map / Reserve.
Warmode uses Selenium only — prefetch at T-1 minute, click Reserve at 7:00 (no API calls).
"""

import argparse
import asyncio
import json
import os
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
_audit_lock = threading.Lock()
_audit_write_warned = False


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


def set_telegram_job_meta(job_id, interval_seconds):
    """Per-job thread: job id and interval for /cancel footer on poll status messages."""
    _LOGGER_LOCAL.telegram_job_id = job_id
    _LOGGER_LOCAL.telegram_interval = int(interval_seconds)
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
    jid = getattr(_LOGGER_LOCAL, "telegram_job_id", "") or ""
    iv = getattr(_LOGGER_LOCAL, "telegram_interval", 60)
    return (
        "\n\nPolling every {}s. To stop this job, send:\n/cancel {}".format(iv, jid)
    )


def pp(message, error=False, telegram_digest=None, skip_telegram=False):
    """
    Log a line. If telegram_digest is a tuple, Telegram mirror is sent only when the
    digest changes (dedupe routine poll status). Some digest kinds append a /cancel
    footer (see set below). telegram_digest None = always mirror to Telegram (if callback).
    skip_telegram=True: print to terminal only; do not notify Telegram or change poll_digest.
    """
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
    driver, url, requested_sites, interval, debug=False, stop_event=None
):
    """
    One API poll per loop iteration (same pace as --interval). No extra requests.
    When API shows a target available, load the map once and click Reserve.
    """
    while True:
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
            if stop_event and stop_event.wait(interval):
                pp("🛑 Cancellation requested")
                return ""
            time.sleep(0 if stop_event else interval)
            continue

        avail = api_available_labels(sites)
        if not avail:
            pp("❌ No Availability (API)", telegram_digest=("zero",))
            if stop_event and stop_event.wait(interval):
                pp("🛑 Cancellation requested")
                return ""
            time.sleep(0 if stop_event else interval)
            continue

        target = pick_api_target(sites, requested_sites)
        if not target:
            labels_csv = ",".join(sorted(avail, key=sort_key))
            if requested_sites:
                # Single digest for "have availability, not our sites" so we do not alternate
                # digests with a separate avail line every poll (which re-sent Telegram every interval).
                pp(
                    "✨ Available sites (API): {}\n"
                    "❌ None of your preferred sites are free (API). Prefer: {} - currently available listed above.".format(
                        labels_csv, ",".join(requested_sites)
                    ),
                    telegram_digest=("filter_wait", frozenset(avail), tuple(requested_sites)),
                )
            else:
                pp(
                    "✨ Available sites (API): {}\n"
                    "❌ Could not pick a target site (API); retrying…".format(labels_csv),
                    telegram_digest=("no_pick_wait", frozenset(avail)),
                )
            if stop_event and stop_event.wait(interval):
                pp("🛑 Cancellation requested")
                return ""
            time.sleep(0 if stop_event else interval)
            continue

        # Proceeding to map: console-only avail line so poll_digest is not clobbered before a hit.
        pp(
            "✨ Available sites (API): {}".format(",".join(sorted(avail, key=sort_key))),
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
                "waiting {}s…".format(label, interval),
                telegram_digest=("map_wait", label),
            )
            if stop_event and stop_event.wait(interval):
                pp("🛑 Cancellation requested")
                return ""
            time.sleep(0 if stop_event else interval)
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

        if stop_event and stop_event.wait(interval):
            pp("🛑 Cancellation requested")
            return ""
        time.sleep(0 if stop_event else interval)


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
        "⚔️  Warmode: prefetch at {} {}…".format(
            fmt_ampm(target_time - timedelta(minutes=1)), timezone
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
        "    ./reserve_site.py --url 'https://camping.bcparks.ca/create-booking/...'\n"
        "    ./reserve_site.py --u   'https://camping.bcparks.ca/create-booking/...'\n\n"
        "  Prefer specific sites (left-to-right order = try order; first match wins).\n"
        "    ./reserve_site.py --url '...' --f 'S51,S52,S53'\n\n"
        "  Poll every 30 seconds instead of 60.\n"
        "    ./reserve_site.py --url '...' --f 'S51' --interval 30\n\n"
        "  Warmode at 7:00 Pacific (prefetch ~6:59).\n"
        "    ./reserve_site.py --url '...' --f 'S51' --warmode\n\n"
        "  Remote Chrome (log in first), then attach (ChromeDriver on PATH must match Chrome).\n"
        "    google-chrome --user-data-dir=$HOME/.bcparks-profile \\\n"
        "      --remote-debugging-port=9222 --no-first-run --no-default-browser-check\n"
        "    ./reserve_site.py --url '...' --rip 127.0.0.1 --rp 9222 --f 'S51'\n\n"
        "  Headed browser (debug map/timeouts on a machine with a display).\n"
        "    ./reserve_site.py --url '...' --f 'S51' --headed\n\n"
        "  SMS when a reservation succeeds (requires Twilio credentials).\n"
        "    ./reserve_site.py --url '...' --f 'S51' --sms \\\n"
        "      --twilio_sid … --twilio_auth_token … --twilio_number … --my_phone_number …\n\n"
        "  Save map step screenshots / failure HTML when the map fails to load.\n"
        "    ./reserve_site.py --url '...' --f 'S51' --debug"
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
        required=False,
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
    parser.add_argument(
        "--telegram-bot",
        action="store_true",
        help="Run Telegram bot mode (long polling) instead of one CLI run.",
    )
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=3,
        help="Max concurrent reservation jobs in Telegram bot mode.",
    )
    parser.add_argument(
        "--no-terminal-log",
        action="store_false",
        dest="terminal_log",
        default=True,
        help="With --telegram-bot: do not print job or bot log lines to the terminal.",
    )
    return parser


class _BotCommandParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def build_bot_reserve_parser():
    parser = _BotCommandParser(add_help=False)
    parser.add_argument("--url", "--u", dest="url", required=False)
    parser.add_argument("--interval", "--i", type=int, default=60)
    parser.add_argument("--filter", "--f", type=comma_separated_list, required=False)
    parser.add_argument("--sms", "--s", action="store_true", default=False)
    parser.add_argument("--twilio_sid", "--tsid", default="")
    parser.add_argument("--twilio_auth_token", "--tat", default="")
    parser.add_argument("--twilio_number", "--tn", default="")
    parser.add_argument("--my_phone_number", "--mpn", default="")
    parser.add_argument("--warmode", "--w", action="store_true", default=False)
    parser.add_argument("--debug", "--d", action="store_true", default=False)
    parser.add_argument("--headed", action="store_true", default=False)
    parser.add_argument("--timezone", default="US/Pacific")
    parser.add_argument("--remote_ip", "--rip", required=False)
    parser.add_argument("--remote_port", "--rp", type=int, required=False)
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
    return args


def _send_telegram_text(bot, loop, chat_id, text):
    async def _send():
        await bot.send_message(chat_id=chat_id, text=text)

    fut = asyncio.run_coroutine_threadsafe(_send(), loop)
    try:
        fut.result(timeout=8)
    except Exception:
        pass


def _run_job(job, manager, bot, loop):
    def _tg(line):
        _send_telegram_text(bot, loop, job.chat_id, line)

    set_log_callback(_tg)
    set_telegram_job_meta(job.job_id, job.args.interval)
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

    if args.remote_ip and args.remote_port:
        driver = setup_webdriver_remote(args.remote_ip, args.remote_port)
    else:
        driver = setup_webdriver(headed=args.headed)
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

    use_remote = bool(args.remote_ip and args.remote_port)
    try:
        if args.warmode:
            reserved_site = reserve_war_mode(
                driver,
                args.url,
                args.filter,
                timezone=args.timezone,
                debug=args.debug,
                stop_event=job.stop_event,
            )
        else:
            reserved_site = reserve_normal_mode(
                driver,
                args.url,
                args.filter,
                interval=args.interval,
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
        if not use_remote:
            try:
                driver.quit()
            except Exception:
                pass
        set_log_callback(None)


def telegram_help_text():
    return (
        "reserve_site_v2 Telegram commands\n\n"
        "/reserve <url> [--f S51,S52] [--i 60] [--warmode] [--timezone US/Pacific] "
        "[--debug] [--headed] [--rip host --rp 9222]\n"
        "/jobs\n"
        "/status <job_id>\n"
        "/cancel <job_id>\n"
        "/help\n\n"
        "Also supported: send a plain URL message to start a default normal-mode job."
    )


async def _tg_reply(update, text):
    em = update.effective_message
    if em:
        await em.reply_text(text)
    return em is not None


def run_telegram_bot(args):
    from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

    set_terminal_log_enabled(args.terminal_log)

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

    async def _start_job(update, raw):
        if not authorized(update):
            await reject(update)
            return
        uid = update.effective_user.id if update.effective_user else "?"
        _bot_console_line("Job request user={} raw={!r}".format(uid, raw[:200] if raw else ""))
        try:
            job_args = parse_bot_reserve_args(raw)
        except Exception as e:
            audit_log(
                "reserve_parse_error",
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
            warmode=bool(job_args.warmode),
            interval=job_args.interval,
            filter=",".join(job_args.filter) if job_args.filter else "",
        )
        loop = asyncio.get_running_loop()
        t = threading.Thread(target=_run_job, args=(job, manager, app.bot, loop), daemon=True)
        job.thread = t
        t.start()
        _bot_console_line("Started job {} for user {}".format(job.job_id, uid))
        await _tg_reply(
            update,
            "Started job {}\nmode={}\nfilter={}".format(
                job.job_id,
                "warmode" if job_args.warmode else "normal",
                ",".join(job_args.filter or []),
            ),
        )

    async def help_cmd(update, context: ContextTypes.DEFAULT_TYPE):
        if not authorized(update):
            await reject(update)
            return
        uid = update.effective_user.id if update.effective_user else None
        _bot_console_line("/help user={}".format(uid if uid is not None else "?"))
        audit_log("command_help", user_id=uid, chat_id=update.effective_chat.id if update.effective_chat else None)
        await _tg_reply(update, telegram_help_text())

    async def reserve_cmd(update, context: ContextTypes.DEFAULT_TYPE):
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
                "command_reserve_usage",
                user_id=update.effective_user.id if update.effective_user else None,
                chat_id=update.effective_chat.id if update.effective_chat else None,
            )
            await _tg_reply(
                update,
                "Usage: /reserve <url> [--f S1,S2] [--i 60] ...\n\n{}".format(telegram_help_text()),
            )
            return
        await _start_job(update, raw)

    async def jobs_cmd(update, context: ContextTypes.DEFAULT_TYPE):
        if not authorized(update):
            await reject(update)
            return
        uid = update.effective_user.id if update.effective_user else None
        _bot_console_line("/jobs user={}".format(uid if uid is not None else "?"))
        audit_log("command_jobs", user_id=uid, chat_id=update.effective_chat.id if update.effective_chat else None)
        active = manager.list_active()
        recent = manager.list_recent(10)
        lines = ["Active jobs: {}".format(len(active))]
        for job in active:
            lines.append("- {} status={} started={}".format(job.job_id, job.status, job.started_at))
        lines.append("Recent jobs:")
        for job in recent:
            lines.append(
                "- {} status={} result={} error={}".format(
                    job.job_id, job.status, job.result_site or "-", job.error or "-"
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
        job = manager.get(jid)
        audit_log(
            "command_status",
            user_id=uid,
            chat_id=cid,
            job_id=jid,
            found=job is not None,
        )
        if not job:
            await _tg_reply(update, "Job not found")
            return
        await _tg_reply(
            update,
            "job={} status={} started={} ended={} result={} error={}".format(
                job.job_id,
                job.status,
                job.started_at,
                job.ended_at or "-",
                job.result_site or "-",
                job.error or "-",
            ),
        )

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
        ok = manager.cancel(jid)
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
            await _tg_reply(update, "Job {} not active".format(jid))

    async def text_handler(update, context: ContextTypes.DEFAULT_TYPE):
        if not authorized(update):
            await reject(update)
            return
        em = update.effective_message
        txt = (em.text if em else "") or ""
        txt = txt.strip()
        uid = update.effective_user.id if update.effective_user else None
        cid = update.effective_chat.id if update.effective_chat else None
        if txt.startswith("http://") or txt.startswith("https://"):
            await _start_job(update, txt)
            return
        audit_log(
            "text_message",
            user_id=uid,
            chat_id=cid,
            preview=txt[:200],
        )
        await _tg_reply(update, "Send /help for commands.")

    app.add_error_handler(telegram_error_handler)
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("reserve", reserve_cmd))
    app.add_handler(CommandHandler("jobs", jobs_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    audit_log(
        "bot_start",
        max_concurrent=manager.max_concurrent,
        terminal_log=args.terminal_log,
        audit_log_path=(os.getenv("CAMPSLINGER_AUDIT_LOG") or "campslinger_telegram_audit.log").strip(),
    )
    _bot_console_line(
        "Telegram bot started (long polling). max_concurrent={} terminal_log={}".format(
            manager.max_concurrent, args.terminal_log
        )
    )
    app.run_polling(drop_pending_updates=True)


def main():
    args = build_arg_parser().parse_args()

    if args.telegram_bot:
        run_telegram_bot(args)
        return

    if not args.url:
        sys.exit("Error: --url/--u is required in CLI mode")

    try:
        validate_bcparks_booking_url(args.url)
    except ValueError as e:
        sys.exit("Invalid booking URL: {}".format(e))

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
                stop_event=None,
            )
        else:
            reserved = reserve_normal_mode(
                driver,
                args.url,
                args.filter,
                interval=args.interval,
                debug=args.debug,
                stop_event=None,
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
