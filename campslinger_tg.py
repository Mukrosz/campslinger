#!/usr/bin/env python3
"""
Campslinger Telegram bot (campslinger_tg.py): BC Parks monitoring and optional reservation.

Primary action is /monitor (API-only polling with notifications).  The "Reserve" toggle
in the wizard's More menu enables Selenium reservation on hit.  "Loop" controls whether
the job keeps running after the first availability hit (continuous) or stops (once).

Operator host flags --rip / --rp attach to Chrome on the same LAN (used only when
Reserve is toggled on).  Warmode uses 07:00 US/Pacific; there is no timezone option.
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
_BCPARKS_BOOKING_HOST = "camping.bcparks.ca"
_BCPARKS_BOOKING_PATH_PREFIX = "/create-booking/"
BCPARKS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

_LOGGER_LOCAL = threading.local()
_TERMINAL_LOG_ENABLED = True
_REMOTE_CHROME = None
_audit_lock = threading.Lock()
_audit_write_warned = False


def validate_bcparks_booking_url(url):
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
    _LOGGER_LOCAL.telegram_job_id = job_id
    _LOGGER_LOCAL.telegram_interval = int(interval_seconds)
    _LOGGER_LOCAL.telegram_interval_jitter = max(0, int(interval_jitter_seconds or 0))
    _LOGGER_LOCAL.park_name = (park_name or "").strip() or None
    _LOGGER_LOCAL.poll_digest = None


def set_terminal_log_enabled(enabled):
    global _TERMINAL_LOG_ENABLED
    _TERMINAL_LOG_ENABLED = bool(enabled)


def _bot_console_line(message):
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
        pp("\U0001f4f8 {}: {}".format(message, path))
    except Exception as e:
        pp("\u26a0\ufe0f  Screenshot failed ({}): {}".format(path, e))


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
    validate_bcparks_booking_url(booking_url)
    site_name_params = bcparks_parse_url(booking_url, ["resourceLocationId", "mapId"])
    site_status_params = bcparks_parse_url(booking_url, ["mapId", "startDate", "endDate"])
    names_url = "{}resourcelocation/resources?{}".format(BCPARKS_API_BASE, urlencode(site_name_params))
    status_url = "{}availability/map?{}".format(BCPARKS_API_BASE, urlencode(site_status_params))
    r1 = requests.get(names_url, headers=BCPARKS_HEADERS, timeout=30)
    r1.raise_for_status()
    r2 = requests.get(status_url, headers=BCPARKS_HEADERS, timeout=30)
    r2.raise_for_status()
    return bcparks_normalize_sites(r1.json(), r2.json())


def bcparks_fetch_park_name(booking_url):
    try:
        validate_bcparks_booking_url(booking_url)
        p = bcparks_parse_url(booking_url, ["resourceLocationId"])
        rid_str = p["resourceLocationId"]
        rid_int = int(rid_str)
        loc_url = "{}resourcelocation?resourceLocationId={}".format(BCPARKS_API_BASE, rid_str)
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
    labels = []
    for key in sorted(sites.keys(), key=sort_key):
        if sites[key].get("status") == 0:
            labels.append(sites[key].get("label", key))
    return labels


def pick_api_target(sites, requested_sites):
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
    options.add_argument("--user-agent={}".format(BCPARKS_HEADERS["User-Agent"]))
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
    try:
        pp("   Diagnostic: title={!r}".format(driver.title))
        pp("   Current URL: {}".format(driver.current_url))
        n_mc = len(driver.find_elements(By.CSS_SELECTOR, ".map-container"))
        n_mi = len(driver.find_elements(By.CLASS_NAME, "map-icon"))
        pp("   Elements: .map-container={}  .map-icon={}".format(n_mc, n_mi))
    except Exception as e:
        pp("   Could not inspect page: {}".format(e))
    if not debug:
        pp("   Re-run with --debug to save reserve_map_failure.html and reserve_map_failure.png here.")
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


def get_available_sites(driver, url, max_attempts=5, retry_delay=1, debug=False, stop_event=None):
    for attempt in range(max_attempts):
        if stop_event and stop_event.is_set():
            pp("🛑 Cancellation requested")
            return {}
        available = {}
        try:
            pp("⏳ Scanning map for available sites (attempt {}/{})...".format(attempt + 1, max_attempts))
            driver.get(url)
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
                pp("✨ Map reports {} available site(s): {}".format(
                    len(available), ",".join(sorted(available.keys(), key=sort_key))))
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
    return get_available_sites(driver, url, max_attempts=3, retry_delay=1, debug=debug, stop_event=stop_event)


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
                        debug_screenshot(driver, os.path.join(os.getcwd(), "ss-after_clicking_site.png"))
                    return site, reserve_button
            except Exception as e:
                pp("⚠️  Skipped site {} due to: {}".format(site, e))
    pp("❌ None of the preferred sites are available on the map")
    return "", None


def reserve_normal_mode(driver, url, requested_sites, interval, interval_jitter=10, debug=False, stop_event=None):
    while True:
        wait_s = randomized_probe_wait_seconds(interval, interval_jitter)
        if stop_event and stop_event.is_set():
            pp("🛑 Cancellation requested")
            return ""
        try:
            sites = bcparks_fetch_sites_map(url)
        except Exception as e:
            pp("❌ API poll failed: {}".format(e), telegram_digest=("api_err", str(e)[:220]))
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
                pp("✨ Available sites: {}\n❌ None of your preferred sites ({}) are free.".format(
                    labels_csv, ",".join(requested_sites)),
                    telegram_digest=("filter_wait", frozenset(avail), tuple(requested_sites)))
            else:
                pp("✨ Available sites: {}\n❌ Could not pick a target site; retrying…".format(labels_csv),
                    telegram_digest=("no_pick_wait", frozenset(avail)))
            if stop_event and stop_event.wait(wait_s):
                pp("🛑 Cancellation requested")
                return ""
            time.sleep(0 if stop_event else wait_s)
            continue
        pp("✨ Available sites: {}".format(",".join(sorted(avail, key=sort_key))), skip_telegram=True)
        label = sites[target].get("label", target)
        pp("🎯 Trying map + Reserve for: {} …".format(label))
        on_map = collect_available_icons_from_map(driver, url, debug=debug, stop_event=stop_event)
        if target not in on_map:
            pp("⚠️  API shows {} but map has no matching available icon yet; waiting {}s…".format(
                label, wait_s), telegram_digest=("map_wait", label))
            if stop_event and stop_event.wait(wait_s):
                pp("🛑 Cancellation requested")
                return ""
            time.sleep(0 if stop_event else wait_s)
            continue
        site, reserve_button = prepare_reservation(driver, on_map, [target], debug=debug)
        if site and reserve_button:
            try:
                driver.execute_script("arguments[0].scrollIntoView(true);", reserve_button)
                if debug:
                    debug_screenshot(driver, os.path.join(os.getcwd(), "ss-before_clicking_reserve.png"))
                driver.execute_script("arguments[0].click();", reserve_button)
                pp("✅ Clicked the Reserve button")
                time.sleep(5)
                if debug:
                    debug_screenshot(driver, os.path.join(os.getcwd(), "ss-after_clicking_reserve.png"))
                return site
            except Exception as e:
                pp("❌ Failed to click reserve button: {}".format(e))
        else:
            pp("❌ Could not prepare reservation", telegram_digest=("prep_fail", label))
        if stop_event and stop_event.wait(wait_s):
            pp("🛑 Cancellation requested")
            return ""
        time.sleep(0 if stop_event else wait_s)


def reserve_war_mode(driver, url, requested_sites, timezone="US/Pacific", debug=False, stop_event=None):
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

    pp("⚔️  Warmode: prefetch at {} US/Pacific (07:00 window)…".format(
        fmt_ampm(target_time - timedelta(minutes=1))))
    if not wait_until(target_time - timedelta(minutes=1)):
        pp("🛑 Cancellation requested")
        return ""
    available_sites = get_available_sites(driver, url, debug=debug, stop_event=stop_event)
    if not available_sites:
        pp("❌ No available sites on map at prefetch time")
        return ""
    site, reserve_button = prepare_reservation(driver, available_sites, requested_sites, debug=debug)
    if not site or not reserve_button:
        pp("❌ Could not prepare reservation (prefetch)")
        return ""
    pp("✅ Prefetch done. Waiting to click Reserve for {} at {}…".format(site, fmt_ampm(target_time)))
    if not wait_until(target_time):
        pp("🛑 Cancellation requested")
        return ""
    try:
        driver.execute_script("arguments[0].scrollIntoView(true);", reserve_button)
        if debug:
            debug_screenshot(driver, os.path.join(os.getcwd(), "ss-before_clicking_reserve.png"))
        driver.execute_script("arguments[0].click();", reserve_button)
        pp("✅ Clicked the Reserve button")
        time.sleep(5)
        if debug:
            debug_screenshot(driver, os.path.join(os.getcwd(), "ss-after_clicking_reserve.png"))
        return site
    except Exception as e:
        pp("❌ Failed to click reserve button at {}: {}".format(fmt_ampm(target_time), e))
        return ""


# ---------------------------------------------------------------------------
# Job state / manager (unchanged from reserve_tg.py)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Process-level argparse (host flags only)
# ---------------------------------------------------------------------------

def build_telegram_arg_parser():
    p = argparse.ArgumentParser(
        description="Campslinger Telegram bot: BC Parks monitoring and optional reservation."
    )
    p.add_argument("--max-concurrent", type=int, default=3, help="Max concurrent jobs.")
    p.add_argument("--no-terminal-log", action="store_false", dest="terminal_log", default=True,
                    help="Do not print job lines to the server terminal.")
    p.add_argument("--rip", "--remote_ip", dest="remote_ip", default=None, metavar="HOST",
                    help="Operator only: Chrome remote debugging host (same LAN). Use with --rp.")
    p.add_argument("--rp", "--remote_port", dest="remote_port", type=int, default=None, metavar="PORT",
                    help="Operator only: remote debugging port (e.g. 9222). Use with --rip.")
    return p


# ---------------------------------------------------------------------------
# Telegram command parser (unified /monitor)
# ---------------------------------------------------------------------------

class _BotCommandParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def build_bot_monitor_parser():
    parser = _BotCommandParser(add_help=False)
    parser.add_argument("--url", "--u", dest="url", required=False)
    parser.add_argument("--interval", "--i", type=int, default=60)
    parser.add_argument("--jitter", "--interval-jitter", "--ij", type=int, default=10)
    parser.add_argument("--filter", "--f", type=comma_separated_list, required=False)
    parser.add_argument("--reserve", "--r", action="store_true", default=False)
    parser.add_argument("--loop", choices=["continuous", "once"], default="continuous")
    parser.add_argument("--warmode", "--w", action="store_true", default=False)
    parser.add_argument("--debug", "--d", action="store_true", default=False)
    parser.add_argument("--sms", "--s", action="store_true", default=False)
    parser.add_argument("--twilio_sid", "--tsid", default="")
    parser.add_argument("--twilio_auth_token", "--tat", default="")
    parser.add_argument("--twilio_number", "--tn", default="")
    parser.add_argument("--my_phone_number", "--mpn", default="")
    return parser


def parse_bot_monitor_args(raw_text):
    tokens = shlex.split(raw_text)
    if tokens and not tokens[0].startswith("-") and tokens[0].startswith("http"):
        tokens = ["--url", tokens[0]] + tokens[1:]
    parser = build_bot_monitor_parser()
    args = parser.parse_args(tokens)
    if not args.url:
        raise ValueError("Missing URL. Usage: /monitor <url> [--f S51 --i 60 --reserve --loop once ...]")
    validate_bcparks_booking_url(args.url)
    args.job_kind = "reserve" if args.reserve else "monitor"
    return args


# ---------------------------------------------------------------------------
# Telegram UI constants and keyboards
# ---------------------------------------------------------------------------

UD_PENDING = "csl_pending"
UD_MONITOR = "csl_monitor"


def _main_menu_keyboard():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📋 /jobs", callback_data="m:j"),
            InlineKeyboardButton("🔎 /status", callback_data="m:s"),
        ],
        [
            InlineKeyboardButton("🛑 /cancel", callback_data="m:c"),
            InlineKeyboardButton("📡 Monitor", callback_data="m:mo"),
        ],
        [InlineKeyboardButton("❓ /help", callback_data="m:h")],
    ])


def _job_control_keyboard(job_id):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🛑 Cancel", callback_data="j:c:{}".format(job_id)),
            InlineKeyboardButton("📊 Status", callback_data="j:s:{}".format(job_id)),
        ],
        [InlineKeyboardButton("📋 Jobs", callback_data="j:l")],
    ])


def _monitor_go_more_keyboard():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("▶️ Go", callback_data="r:g"),
            InlineKeyboardButton("⚙️ More", callback_data="r:m"),
        ],
        [InlineKeyboardButton("❌ Cancel wizard", callback_data="r:x")],
    ])


def _monitor_more_menu_keyboard(rb):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    reserve_on = rb.get("reserve", False)
    reserve_label = "⛺ Reserve: ON" if reserve_on else "⛺ Reserve: off"
    loop_label = "🔄 Loop: {}".format(rb.get("loop", "continuous"))
    sms_label = "📱 SMS / Twilio{}".format(" ✓" if rb.get("sms") else "")

    rows = [
        [InlineKeyboardButton("🎯 Sites (--f)", callback_data="r:o:f")],
        [InlineKeyboardButton("⏱ Interval (--i)", callback_data="r:o:i")],
        [InlineKeyboardButton("🎲 Jitter (--jitter)", callback_data="r:o:j")],
        [InlineKeyboardButton(reserve_label, callback_data="r:o:rv")],
        [InlineKeyboardButton(loop_label, callback_data="r:o:l")],
        [InlineKeyboardButton(sms_label, callback_data="r:o:sms")],
    ]
    if reserve_on:
        rows.append([
            InlineKeyboardButton(
                "🌅 Warmode{}".format(" ✓" if rb.get("warmode") else ""),
                callback_data="r:o:w",
            ),
            InlineKeyboardButton(
                "🐛 Debug{}".format(" ✓" if rb.get("debug") else ""),
                callback_data="r:o:d",
            ),
        ])
    rows.append([InlineKeyboardButton("▶ Run", callback_data="r:e")])
    rows.append([
        InlineKeyboardButton("🔙 Back", callback_data="r:b"),
        InlineKeyboardButton("🔄 Reset", callback_data="r:z"),
    ])
    return InlineKeyboardMarkup(rows)


def _loop_submenu_keyboard():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔁 Continuous", callback_data="r:l:c"),
            InlineKeyboardButton("1️⃣ Once", callback_data="r:l:o"),
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="r:l:bk")],
    ])


def _sms_submenu_keyboard(rb):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    sms_on = rb.get("sms", False)
    toggle_label = "📱 SMS: ON" if sms_on else "📱 SMS: off"
    sid_status = "✓" if rb.get("twilio_sid") else "✗"
    at_status = "✓" if rb.get("twilio_auth_token") else "✗"
    tn_status = "✓" if rb.get("twilio_number") else "✗"
    mpn_status = "✓" if rb.get("my_phone_number") else "✗"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle_label, callback_data="r:s:tog")],
        [InlineKeyboardButton("Twilio SID [{}]".format(sid_status), callback_data="r:s:sid")],
        [InlineKeyboardButton("Auth Token [{}]".format(at_status), callback_data="r:s:at")],
        [InlineKeyboardButton("Twilio Number [{}]".format(tn_status), callback_data="r:s:tn")],
        [InlineKeyboardButton("Your Phone [{}]".format(mpn_status), callback_data="r:s:mpn")],
        [InlineKeyboardButton("🔙 Back", callback_data="r:s:bk")],
    ])


# ---------------------------------------------------------------------------
# Wizard state helpers
# ---------------------------------------------------------------------------

def _clear_user_flow(context):
    context.user_data.pop(UD_PENDING, None)
    context.user_data.pop(UD_MONITOR, None)


def _default_monitor_state(url):
    return {
        "url": url,
        "interval": 60,
        "jitter": 10,
        "filter": None,
        "reserve": False,
        "loop": "continuous",
        "warmode": False,
        "debug": False,
        "sms": False,
        "twilio_sid": "",
        "twilio_auth_token": "",
        "twilio_number": "",
        "my_phone_number": "",
    }


def format_monitor_command_preview(rb):
    parts = ["/monitor", rb["url"]]
    if rb.get("filter"):
        parts.append("--f {}".format(",".join(rb["filter"])))
    iv = int(rb.get("interval") or 60)
    if iv != 60:
        parts.append("--i {}".format(iv))
    jv = max(0, int(rb.get("jitter") if rb.get("jitter") is not None else 10))
    if jv != 10:
        parts.append("--jitter {}".format(jv))
    if rb.get("reserve"):
        parts.append("--reserve")
    if rb.get("loop", "continuous") != "continuous":
        parts.append("--loop {}".format(rb["loop"]))
    if rb.get("warmode"):
        parts.append("--warmode")
    if rb.get("debug"):
        parts.append("--debug")
    if rb.get("sms"):
        parts.append("--sms …")
    return " ".join(parts)


def monitor_state_to_shlex_raw(rb):
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
    if rb.get("reserve"):
        chunks.append("--reserve")
    if rb.get("loop", "continuous") != "continuous":
        chunks.append("--loop")
        chunks.append(rb["loop"])
    if rb.get("warmode"):
        chunks.append("--warmode")
    if rb.get("debug"):
        chunks.append("--debug")
    if rb.get("sms"):
        chunks.append("--sms")
        if rb.get("twilio_sid"):
            chunks.extend(["--tsid", shlex.quote(rb["twilio_sid"])])
        if rb.get("twilio_auth_token"):
            chunks.extend(["--tat", shlex.quote(rb["twilio_auth_token"])])
        if rb.get("twilio_number"):
            chunks.extend(["--tn", shlex.quote(rb["twilio_number"])])
        if rb.get("my_phone_number"):
            chunks.extend(["--mpn", shlex.quote(rb["my_phone_number"])])
    return " ".join(chunks)


# ---------------------------------------------------------------------------
# Job runners
# ---------------------------------------------------------------------------

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
        job.job_id, job.args.interval,
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
            audit_log("job_aborted", user_id=job.user_id, chat_id=job.chat_id,
                      job_id=job.job_id, reason="missing_twilio", url=args.url)
            set_log_callback(None)
            return

    loop_mode = getattr(args, "loop", "continuous")
    try:
        while True:
            if job.stop_event.is_set():
                break
            wait_s = randomized_probe_wait_seconds(args.interval, args.jitter)
            try:
                sites = bcparks_fetch_sites_map(args.url)
            except Exception as e:
                pp("❌ API poll failed: {}".format(e), telegram_digest=("api_err", str(e)[:220]))
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
                            ts, prefix, ",".join(matching), shorten_url(args.url))
                        send_sms(body, client, args.my_phone_number, args.twilio_number)
                    except Exception as e:
                        pp("❌ SMS failed: {}".format(e), telegram_digest=None)
                if loop_mode == "once":
                    pp("✅ Monitor job finished (loop=once, first hit).", telegram_digest=None)
                    manager.mark_done(job.job_id, "done", site=",".join(matching))
                    audit_log("job_finished", user_id=job.user_id, chat_id=job.chat_id,
                              job_id=job.job_id, status="done", url=args.url, job_kind="monitor")
                    set_log_callback(None)
                    return
            elif not all_avail:
                pp("No availability", telegram_digest=("zero",))
            else:
                labels_csv = ",".join(sorted(all_avail, key=sort_key))
                pp("✨ Available sites: {}\n❌ None of your preferred sites ({}) are free.".format(
                    labels_csv, ",".join(args.filter or [])),
                    telegram_digest=("filter_wait", frozenset(all_avail), tuple(args.filter or ())))

            if job.stop_event.wait(wait_s):
                break

        if job.stop_event.is_set():
            manager.mark_done(job.job_id, "cancelled")
            _send_telegram_text(bot, loop, job.chat_id, "🛑 Job {} cancelled".format(job.job_id))
            audit_log("job_finished", user_id=job.user_id, chat_id=job.chat_id,
                      job_id=job.job_id, status="cancelled", url=args.url, job_kind="monitor")
        else:
            manager.mark_done(job.job_id, "done")
    except Exception as e:
        pp("❌ Monitor job error: {}".format(e), telegram_digest=None)
        manager.mark_done(job.job_id, "error", error=str(e))
        _send_telegram_text(bot, loop, job.chat_id, "❌ Job {} error: {}".format(job.job_id, e))
        audit_log("job_finished", user_id=job.user_id, chat_id=job.chat_id,
                  job_id=job.job_id, status="error", error=str(e), url=args.url, job_kind="monitor")
    finally:
        set_log_callback(None)


def _run_reserve_job(job, manager, bot, loop):
    """Selenium reservation job (same as reserve_tg.py _run_job)."""
    def _tg(line):
        _send_telegram_text(bot, loop, job.chat_id, line)

    set_log_callback(_tg)
    park = bcparks_fetch_park_name(job.args.url)
    set_telegram_job_meta(
        job.job_id, job.args.interval,
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
            audit_log("job_aborted", user_id=job.user_id, chat_id=job.chat_id,
                      job_id=job.job_id, reason="missing_twilio", url=args.url)
            set_log_callback(None)
            return

    if _REMOTE_CHROME:
        driver = setup_webdriver_remote(_REMOTE_CHROME[0], _REMOTE_CHROME[1])
    else:
        driver = setup_webdriver()
    if not driver:
        _send_telegram_text(bot, loop, job.chat_id, "❌ WebDriver initialization failed")
        manager.mark_done(job.job_id, "error", error="webdriver_init_failed")
        audit_log("job_aborted", user_id=job.user_id, chat_id=job.chat_id,
                  job_id=job.job_id, reason="webdriver_init_failed", url=args.url)
        set_log_callback(None)
        return

    try:
        if args.warmode:
            reserved_site = reserve_war_mode(
                driver, args.url, args.filter,
                timezone="US/Pacific", debug=args.debug, stop_event=job.stop_event)
        else:
            reserved_site = reserve_normal_mode(
                driver, args.url, args.filter,
                interval=args.interval, interval_jitter=args.jitter,
                debug=args.debug, stop_event=job.stop_event)

        if job.stop_event.is_set():
            manager.mark_done(job.job_id, "cancelled")
            _send_telegram_text(bot, loop, job.chat_id, "🛑 Job {} cancelled".format(job.job_id))
            audit_log("job_finished", user_id=job.user_id, chat_id=job.chat_id,
                      job_id=job.job_id, status="cancelled", url=args.url)
            return

        if reserved_site:
            if args.sms and client:
                send_sms("{} - 🎯 Reserved: {}\n{}".format(
                    current_time(), reserved_site, shorten_url(args.url)),
                    client, args.my_phone_number, args.twilio_number)
            manager.mark_done(job.job_id, "success", site=reserved_site)
            _send_telegram_text(bot, loop, job.chat_id,
                                "✅ Job {} success. Reserved: {}".format(job.job_id, reserved_site))
            audit_log("job_finished", user_id=job.user_id, chat_id=job.chat_id,
                      job_id=job.job_id, status="success", result_site=reserved_site, url=args.url)
        else:
            manager.mark_done(job.job_id, "failed", error="no_reservation")
            _send_telegram_text(bot, loop, job.chat_id,
                                "❌ Job {} finished without reservation".format(job.job_id))
            audit_log("job_finished", user_id=job.user_id, chat_id=job.chat_id,
                      job_id=job.job_id, status="failed", url=args.url)
    except Exception as e:
        manager.mark_done(job.job_id, "error", error=str(e))
        _send_telegram_text(bot, loop, job.chat_id, "❌ Job {} error: {}".format(job.job_id, e))
        audit_log("job_finished", user_id=job.user_id, chat_id=job.chat_id,
                  job_id=job.job_id, status="error", error=str(e), url=args.url)
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        set_log_callback(None)


def _run_job_dispatch(job, manager, bot, loop):
    if getattr(job.args, "job_kind", "monitor") == "reserve":
        _run_reserve_job(job, manager, bot, loop)
    else:
        _run_monitor_job(job, manager, bot, loop)


# ---------------------------------------------------------------------------
# Help text
# ---------------------------------------------------------------------------

def telegram_help_text():
    return (
        "Campslinger Telegram commands\n\n"
        "/monitor <url> [--f S51,S52] [--i 60] [--jitter 10] [--reserve] "
        "[--loop once|continuous] [--warmode] [--debug] [--sms + Twilio flags]\n\n"
        "Default: API-only monitoring, continuous.\n"
        "--reserve: also click Reserve on hit (Selenium).\n"
        "--loop once: stop after first hit.\n"
        "Warmode uses 07:00 US/Pacific.\n\n"
        "/jobs\n"
        "/status <job_id>\n"
        "/cancel <job_id>\n"
        "/help\n\n"
        "Tip: tap 📡 Monitor or send a plain booking URL for defaults."
    )


async def _tg_reply(update, text, reply_markup=None):
    em = update.effective_message
    if em:
        await em.reply_text(text, reply_markup=reply_markup)
        return True
    return False


# ---------------------------------------------------------------------------
# Telegram bot entry
# ---------------------------------------------------------------------------

def run_telegram_bot(args):
    from telegram.ext import (
        ApplicationBuilder, CallbackQueryHandler, CommandHandler,
        ContextTypes, MessageHandler, filters,
    )

    set_terminal_log_enabled(args.terminal_log)

    global _REMOTE_CHROME
    rip = (args.remote_ip or "").strip() if args.remote_ip else ""
    rp = args.remote_port
    if bool(rip) != (rp is not None):
        raise RuntimeError("Remote Chrome requires both --rip and --rp (or neither).")
    if rip and rp is not None:
        _REMOTE_CHROME = (rip, int(rp))
        _bot_console_line("Using remote Chrome at {}:{} (same LAN)".format(
            _REMOTE_CHROME[0], _REMOTE_CHROME[1]))
    else:
        _REMOTE_CHROME = None

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
        audit_log("handler_error", user_id=uid, chat_id=cid,
                  error_type=type(err).__name__ if err else None,
                  error=str(err)[:500] if err else None)
        _bot_console_line("Telegram handler error: {!r}".format(err))
        if _TERMINAL_LOG_ENABLED and err is not None:
            traceback.print_exception(type(err), err, err.__traceback__, file=sys.stderr)
        if update and authorized(update):
            em = getattr(update, "effective_message", None)
            if em:
                try:
                    await em.reply_text("Error handling this update. If it persists, check server logs.")
                except Exception:
                    pass

    # -------------------------------------------------------------------
    # _start_job: unified entry point
    # -------------------------------------------------------------------
    async def _start_job(update, context, raw):
        if not authorized(update):
            await reject(update)
            return
        uid = update.effective_user.id if update.effective_user else "?"
        _bot_console_line("Job request user={} raw={!r}".format(uid, raw[:200] if raw else ""))
        try:
            job_args = parse_bot_monitor_args(raw)
        except Exception as e:
            audit_log("job_parse_error",
                      user_id=uid if isinstance(uid, int) else None,
                      chat_id=update.effective_chat.id if update.effective_chat else None,
                      error=str(e)[:500])
            await _tg_reply(update, "Parse error: {}\n\n{}".format(e, telegram_help_text()))
            return
        job = manager.create(update.effective_chat.id, update.effective_user.id, job_args)
        if not job:
            audit_log("job_rejected_busy",
                      user_id=uid if isinstance(uid, int) else None,
                      chat_id=update.effective_chat.id if update.effective_chat else None,
                      max_concurrent=manager.max_concurrent)
            await _tg_reply(update, "Server busy: max {} concurrent jobs reached.".format(manager.max_concurrent))
            return
        audit_log("job_queued", user_id=job.user_id, chat_id=job.chat_id,
                  job_id=job.job_id, url=job_args.url,
                  job_kind=job_args.job_kind,
                  reserve=bool(job_args.reserve),
                  loop=job_args.loop,
                  warmode=bool(getattr(job_args, "warmode", False)),
                  interval=job_args.interval,
                  filter=",".join(job_args.filter) if job_args.filter else "")
        ev_loop = asyncio.get_running_loop()
        t = threading.Thread(target=_run_job_dispatch, args=(job, manager, app.bot, ev_loop), daemon=True)
        job.thread = t
        t.start()
        _bot_console_line("Started job {} for user {}".format(job.job_id, uid))
        if job_args.job_kind == "monitor":
            mode = "monitor ({})".format(job_args.loop)
        else:
            mode = "reserve{}".format(" (warmode)" if job_args.warmode else "")
        await _tg_reply(update, "Started job {}\nkind={}\nmode={}\nfilter={}".format(
            job.job_id, job_args.job_kind, mode, ",".join(job_args.filter or [])))
        cid = update.effective_chat.id
        await context.bot.send_message(
            chat_id=cid,
            text="Job {} - quick actions while it runs:".format(job.job_id),
            reply_markup=_job_control_keyboard(job.job_id))

    def _format_status_text(jid, requester_user_id):
        job = manager.get_for_user(jid, requester_user_id)
        if not job:
            return "Job not found or not yours."
        return "job={} kind={} status={} started={} ended={} result={} error={}".format(
            job.job_id, getattr(job.args, "job_kind", "?"),
            job.status, job.started_at, job.ended_at or "-",
            job.result_site or "-", job.error or "-")

    # -------------------------------------------------------------------
    # Command handlers
    # -------------------------------------------------------------------
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
            reply_markup=_main_menu_keyboard())

    async def monitor_cmd(update, context: ContextTypes.DEFAULT_TYPE):
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
            audit_log("command_monitor_usage",
                      user_id=update.effective_user.id if update.effective_user else None,
                      chat_id=update.effective_chat.id if update.effective_chat else None)
            context.user_data.pop(UD_MONITOR, None)
            context.user_data[UD_PENDING] = "r_url"
            await _tg_reply(
                update,
                "Send your full BC Parks results URL, or type a full /monitor … command.\n\n{}".format(
                    telegram_help_text()),
                reply_markup=_main_menu_keyboard())
            return
        await _start_job(update, context, raw)

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
            lines.append("- {} kind={} status={} started={}".format(
                job.job_id, getattr(job.args, "job_kind", "?"), job.status, job.started_at))
        lines.append("Recent jobs:")
        for job in recent:
            lines.append("- {} kind={} status={} result={} error={}".format(
                job.job_id, getattr(job.args, "job_kind", "?"),
                job.status, job.result_site or "-", job.error or "-"))
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
        audit_log("command_status", user_id=uid, chat_id=cid, job_id=jid, found=job is not None)
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
        audit_log("command_cancel", user_id=uid, chat_id=cid, job_id=jid, accepted=ok)
        if ok:
            _bot_console_line("/cancel {} user={}".format(jid, uid if uid is not None else "?"))
            await _tg_reply(update, "Cancellation requested for {}".format(jid))
        else:
            await _tg_reply(update, "Job {} not active or not yours".format(jid))

    # -------------------------------------------------------------------
    # Text handler (wizard text inputs + bare URLs)
    # -------------------------------------------------------------------
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

        if pend in ("status", "cancel"):
            if txt.lower() in ("abort", "stop", "nevermind"):
                context.user_data.pop(UD_PENDING, None)
                await _tg_reply(update, "Okay, cancelled.")
                return
            jid = txt.split()[0].strip()
            if pend == "status":
                context.user_data.pop(UD_PENDING, None)
                audit_log("command_status", user_id=uid, chat_id=cid, job_id=jid,
                          found=manager.get_for_user(jid, uid) is not None)
                await _tg_reply(update, _format_status_text(jid, uid))
                return
            context.user_data.pop(UD_PENDING, None)
            ok = manager.cancel_for_user(jid, uid)
            audit_log("command_cancel", user_id=uid, chat_id=cid, job_id=jid, accepted=ok)
            await _tg_reply(update,
                            "Cancellation requested for {}".format(jid) if ok
                            else "Job {} not active or not yours".format(jid))
            return

        if pend == "r_url":
            if txt.lower() in ("abort", "stop", "nevermind"):
                context.user_data.pop(UD_PENDING, None)
                await _tg_reply(update, "Okay, cancelled.")
                return
            if not (txt.startswith("http://") or txt.startswith("https://")):
                await _tg_reply(update, "Please send a URL starting with https://camping.bcparks.ca/... or type abort.")
                return
            try:
                validate_bcparks_booking_url(txt)
            except ValueError as e:
                await _tg_reply(update, "Invalid URL: {}".format(e))
                return
            context.user_data.pop(UD_PENDING, None)
            context.user_data[UD_MONITOR] = _default_monitor_state(txt)
            rb = context.user_data[UD_MONITOR]
            await _tg_reply(
                update,
                "URL saved.\n\nCurrent command:\n{}\n\n▶️ Go = run with defaults.\n⚙️ More = sites, interval, reserve, loop, SMS …".format(
                    format_monitor_command_preview(rb)),
                reply_markup=_monitor_go_more_keyboard())
            return

        if pend == "r_f":
            context.user_data.pop(UD_PENDING, None)
            rb = context.user_data.get(UD_MONITOR)
            if not rb:
                await _tg_reply(update, "Wizard expired. Tap 📡 Monitor again.")
                return
            if txt.lower() not in ("clear", "none", "-"):
                rb["filter"] = comma_separated_list(txt)
            else:
                rb["filter"] = None
            await _tg_reply(update,
                            "Updated.\n\n{}\n\nUse More to change more options or Run.".format(
                                format_monitor_command_preview(rb)),
                            reply_markup=_monitor_more_menu_keyboard(rb))
            return

        if pend == "r_i":
            context.user_data.pop(UD_PENDING, None)
            rb = context.user_data.get(UD_MONITOR)
            if not rb:
                await _tg_reply(update, "Wizard expired.")
                return
            try:
                rb["interval"] = max(5, int(txt.split()[0]))
            except ValueError:
                await _tg_reply(update, "Send a number of seconds (e.g. 60), or open More again.")
                return
            await _tg_reply(update,
                            "Updated.\n\n{}".format(format_monitor_command_preview(rb)),
                            reply_markup=_monitor_more_menu_keyboard(rb))
            return

        if pend == "r_j":
            context.user_data.pop(UD_PENDING, None)
            rb = context.user_data.get(UD_MONITOR)
            if not rb:
                await _tg_reply(update, "Wizard expired.")
                return
            try:
                rb["jitter"] = max(0, int(txt.split()[0]))
            except ValueError:
                await _tg_reply(update, "Send jitter seconds as a non-negative integer (e.g. 10).")
                return
            await _tg_reply(update,
                            "Updated.\n\n{}".format(format_monitor_command_preview(rb)),
                            reply_markup=_monitor_more_menu_keyboard(rb))
            return

        if pend in ("r_tsid", "r_tat", "r_tn", "r_mpn"):
            context.user_data.pop(UD_PENDING, None)
            rb = context.user_data.get(UD_MONITOR)
            if not rb:
                await _tg_reply(update, "Wizard expired.")
                return
            field_map = {"r_tsid": "twilio_sid", "r_tat": "twilio_auth_token",
                         "r_tn": "twilio_number", "r_mpn": "my_phone_number"}
            rb[field_map[pend]] = txt.strip()
            try:
                await update.effective_message.reply_text(
                    "Saved.\n\nSMS settings:",
                    reply_markup=_sms_submenu_keyboard(rb))
            except Exception:
                await _tg_reply(update, "Saved.")
            return

        if txt.startswith("http://") or txt.startswith("https://"):
            await _start_job(update, context, txt)
            return

        audit_log("text_message", user_id=uid, chat_id=cid, preview=txt[:200])
        await _tg_reply(update, "Send /help, a booking URL, or use the buttons from the last help message.")

    # -------------------------------------------------------------------
    # Callback handler (main menu, job controls, wizard)
    # -------------------------------------------------------------------
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

        # --- main menu ---
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
        if data == "m:mo":
            await ack()
            context.user_data.pop(UD_MONITOR, None)
            context.user_data[UD_PENDING] = "r_url"
            await q.message.reply_text(
                "Send your full BC Parks results URL (https://camping.bcparks.ca/create-booking/...).\n"
                "You can still type a full /monitor … command manually anytime.")
            return

        # --- job control ---
        if data.startswith("j:c:"):
            await ack()
            jid = data[4:].strip()
            ok = manager.cancel_for_user(jid, uid)
            audit_log("command_cancel", user_id=uid, chat_id=cid, job_id=jid, accepted=ok)
            await q.message.reply_text(
                "Cancellation requested for {}".format(jid) if ok
                else "Job {} not active or not yours".format(jid))
            return
        if data.startswith("j:s:"):
            await ack()
            jid = data[4:].strip()
            audit_log("command_status", user_id=uid, chat_id=cid, job_id=jid,
                      found=manager.get_for_user(jid, uid) is not None)
            await q.message.reply_text(_format_status_text(jid, uid))
            return
        if data == "j:l":
            await ack()
            await jobs_cmd(update, context)
            return

        # --- wizard ---
        rb = context.user_data.get(UD_MONITOR)

        async def _edit_or_reply(text, markup):
            try:
                await q.edit_message_text(text, reply_markup=markup)
            except Exception:
                await q.message.reply_text(text, reply_markup=markup)

        if data == "r:x":
            await ack()
            _clear_user_flow(context)
            await q.message.reply_text("Wizard cancelled.")
            return
        if data in ("r:g", "r:e"):
            await ack()
            if not rb:
                await q.message.reply_text("No URL in progress. Tap 📡 Monitor.")
                return
            raw = monitor_state_to_shlex_raw(rb)
            _clear_user_flow(context)
            await _start_job(update, context, raw)
            return
        if data == "r:m":
            await ack()
            if not rb:
                await q.message.reply_text("No URL in progress.")
                return
            await _edit_or_reply(
                "{}\n\nPick an option:".format(format_monitor_command_preview(rb)),
                _monitor_more_menu_keyboard(rb))
            return
        if data == "r:b":
            await ack()
            if not rb:
                await q.message.reply_text("Wizard expired.")
                return
            await _edit_or_reply(
                "{}\n\nGo or More:".format(format_monitor_command_preview(rb)),
                _monitor_go_more_keyboard())
            return
        if data == "r:z":
            await ack()
            if not rb or not rb.get("url"):
                await q.message.reply_text("Nothing to reset.")
                return
            url = rb["url"]
            context.user_data[UD_MONITOR] = _default_monitor_state(url)
            rb = context.user_data[UD_MONITOR]
            await _edit_or_reply(
                "Reset options.\n\n{}".format(format_monitor_command_preview(rb)),
                _monitor_more_menu_keyboard(rb))
            return

        # --- wizard: option buttons ---
        if data == "r:o:f":
            await ack()
            if not rb:
                await q.message.reply_text("Start the wizard from 📡 Monitor.")
                return
            context.user_data[UD_PENDING] = "r_f"
            await q.message.reply_text("Send preferred site labels comma-separated (e.g. S51,S52), or clear to remove.")
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
            await q.message.reply_text("Send jitter in seconds (e.g. 10).")
            return
        if data == "r:o:rv":
            await ack()
            if not rb:
                return
            rb["reserve"] = not rb.get("reserve", False)
            await _edit_or_reply(
                "{}\n\nPick an option:".format(format_monitor_command_preview(rb)),
                _monitor_more_menu_keyboard(rb))
            return
        if data == "r:o:w":
            await ack()
            if not rb:
                return
            rb["warmode"] = not rb.get("warmode")
            await _edit_or_reply(
                "{}\n\nPick an option:".format(format_monitor_command_preview(rb)),
                _monitor_more_menu_keyboard(rb))
            return
        if data == "r:o:d":
            await ack()
            if not rb:
                return
            rb["debug"] = not rb.get("debug")
            await _edit_or_reply(
                "{}\n\nPick an option:".format(format_monitor_command_preview(rb)),
                _monitor_more_menu_keyboard(rb))
            return

        # --- Loop submenu ---
        if data == "r:o:l":
            await ack()
            if not rb:
                return
            cur = rb.get("loop", "continuous")
            await _edit_or_reply(
                "What to do after first availability hit?\n\nCurrent: {}\n\n"
                "Continuous = keep polling.\nOnce = stop after first hit.".format(cur),
                _loop_submenu_keyboard())
            return
        if data == "r:l:c":
            await ack()
            if rb:
                rb["loop"] = "continuous"
            await _edit_or_reply(
                "{}\n\nPick an option:".format(format_monitor_command_preview(rb or {})),
                _monitor_more_menu_keyboard(rb or {}))
            return
        if data == "r:l:o":
            await ack()
            if rb:
                rb["loop"] = "once"
            await _edit_or_reply(
                "{}\n\nPick an option:".format(format_monitor_command_preview(rb or {})),
                _monitor_more_menu_keyboard(rb or {}))
            return
        if data == "r:l:bk":
            await ack()
            if rb:
                await _edit_or_reply(
                    "{}\n\nPick an option:".format(format_monitor_command_preview(rb)),
                    _monitor_more_menu_keyboard(rb))
            return

        # --- SMS / Twilio submenu ---
        if data == "r:o:sms":
            await ack()
            if not rb:
                return
            await _edit_or_reply("SMS / Twilio settings:", _sms_submenu_keyboard(rb))
            return
        if data == "r:s:tog":
            await ack()
            if rb:
                rb["sms"] = not rb.get("sms", False)
            await _edit_or_reply("SMS / Twilio settings:", _sms_submenu_keyboard(rb or {}))
            return
        if data == "r:s:sid":
            await ack()
            if not rb:
                return
            context.user_data[UD_PENDING] = "r_tsid"
            await q.message.reply_text("Send your Twilio Account SID:")
            return
        if data == "r:s:at":
            await ack()
            if not rb:
                return
            context.user_data[UD_PENDING] = "r_tat"
            await q.message.reply_text("Send your Twilio Auth Token:")
            return
        if data == "r:s:tn":
            await ack()
            if not rb:
                return
            context.user_data[UD_PENDING] = "r_tn"
            await q.message.reply_text("Send your Twilio phone number (e.g. +1234567890):")
            return
        if data == "r:s:mpn":
            await ack()
            if not rb:
                return
            context.user_data[UD_PENDING] = "r_mpn"
            await q.message.reply_text("Send your phone number to receive SMS (e.g. +1234567890):")
            return
        if data == "r:s:bk":
            await ack()
            if rb:
                await _edit_or_reply(
                    "{}\n\nPick an option:".format(format_monitor_command_preview(rb)),
                    _monitor_more_menu_keyboard(rb))
            return

        await ack()

    # -------------------------------------------------------------------
    # Register handlers and start
    # -------------------------------------------------------------------
    app.add_error_handler(telegram_error_handler)
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("monitor", monitor_cmd))
    app.add_handler(CommandHandler("jobs", jobs_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    audit_log("bot_start", max_concurrent=manager.max_concurrent, terminal_log=args.terminal_log,
              remote_chrome="{}:{}".format(_REMOTE_CHROME[0], _REMOTE_CHROME[1]) if _REMOTE_CHROME else None,
              audit_log_path=(os.getenv("CAMPSLINGER_AUDIT_LOG") or "campslinger_telegram_audit.log").strip())
    _bot_console_line("Telegram bot started (long polling). max_concurrent={} terminal_log={}{}".format(
        manager.max_concurrent, args.terminal_log,
        " remote_chrome={}:{}".format(_REMOTE_CHROME[0], _REMOTE_CHROME[1]) if _REMOTE_CHROME else ""))
    app.run_polling(drop_pending_updates=True)


def main():
    run_telegram_bot(build_telegram_arg_parser().parse_args())


if __name__ == "__main__":
    main()
