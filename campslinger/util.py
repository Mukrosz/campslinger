"""Pure-Python utilities shared by CLI and Telegram entrypoints."""

import random
import re
import sys
from datetime import datetime
from urllib.parse import urlparse

_BCPARKS_BOOKING_HOST = "camping.bcparks.ca"
_BCPARKS_BOOKING_PATH_PREFIX = "/create-booking/"


def validate_bcparks_booking_url(url):
    """Reject non-BC-Parks URLs (SSRF hardening)."""
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
    except Exception:
        return url


def send_sms(message, client, to_number, from_number):
    from campslinger.log import pp
    msg = client.messages.create(to=to_number, from_=from_number, body=message)
    pp("SMS sent: {}".format(msg.sid))


def debug_screenshot(driver, path, message="Debug screenshot saved"):
    from campslinger.log import pp
    try:
        driver.save_screenshot(path)
        pp("\U0001f4f8 {}: {}".format(message, path))
    except Exception as e:
        pp("\u26a0\ufe0f  Screenshot failed ({}): {}".format(path, e))
