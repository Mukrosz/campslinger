"""Pure-Python utilities shared by CLI and Telegram entrypoints."""

import random
import re
import sys
from datetime import datetime
from urllib.parse import urlparse

SUPPORTED_PARK_HOSTS = (
    "camping.bcparks.ca",
    "reservations.ontarioparks.ca",
    "reservation.pc.gc.ca",
    "camping.manitobaparks.com",
    "camping.novascotia.ca",
    "camping.nbbparks.ca",
    "camping.nlcamping.ca",
    "yukon.goingtocamp.com",
    "midnrreservations.com",
    "parkreservations.maryland.gov",
    "mississippi.goingtocamp.com",
    "nebraska.goingtocamp.com",
)

_BOOKING_PATH_PREFIX = "/create-booking/"


def validate_booking_url(url):
    """Reject URLs not on a known Aspira/GoingToCamp park platform (SSRF hardening)."""
    if not url or not isinstance(url, str):
        raise ValueError("Missing booking URL")
    u = url.strip()
    p = urlparse(u)
    if (p.scheme or "").lower() != "https":
        raise ValueError("Booking URL must use https")
    host = (p.hostname or "").lower()
    if host not in SUPPORTED_PARK_HOSTS:
        raise ValueError(
            "Unsupported park host: {}. Supported: {}".format(
                host, ", ".join(SUPPORTED_PARK_HOSTS)))
    path = p.path or ""
    if not path.startswith(_BOOKING_PATH_PREFIX):
        raise ValueError("Booking URL path must start with {}".format(_BOOKING_PATH_PREFIX))
    if p.username or p.password:
        raise ValueError("Booking URL must not contain embedded credentials")
    if p.port not in (None, 443):
        raise ValueError("Booking URL must use default HTTPS port")
    return u


def api_base_from_url(booking_url):
    """Derive the API base (e.g. https://host/api/) from a booking URL."""
    p = urlparse(booking_url.strip())
    return "https://{}/api/".format(p.hostname.lower())


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
