"""Pure-Python utilities shared by CLI and Telegram entrypoints."""

import os
import random
import re
import sys
from datetime import datetime
from urllib.parse import parse_qs, urlparse

SUPPORTED_PARK_HOSTS = (
    "camping.bcparks.ca",
    "reservations.ontarioparks.ca",
    "reservation.pc.gc.ca",
    "camping.manitobaparks.com",
    "camping.novascotia.ca",
    "camping.nbparks.ca",
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
        shown = ", ".join(SUPPORTED_PARK_HOSTS[:4])
        extra = len(SUPPORTED_PARK_HOSTS) - 4
        supported = "{} and {} more".format(shown, extra) if extra > 0 else shown
        raise ValueError(
            "Unsupported park host: {}. Supported platforms include: {}".format(host, supported))
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


def _slugify_park_label(name):
    """Lowercase filesystem-friendly slug from park display name."""
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    if not s:
        return "unknown-park"
    if len(s) > 72:
        s = s[:72].rstrip("-")
    return s


def _stay_window_slug(booking_url):
    """e.g. jun09-jun12 from startDate/endDate query params."""
    try:
        q = parse_qs(urlparse((booking_url or "").strip()).query)
        sd = (q.get("startDate") or [None])[0]
        ed = (q.get("endDate") or [None])[0]
        if not sd or not ed:
            return "nostay"
        d1 = datetime.strptime(sd[:10], "%Y-%m-%d")
        d2 = datetime.strptime(ed[:10], "%Y-%m-%d")
        return "{}-{}".format(d1.strftime("%b%d").lower(), d2.strftime("%b%d").lower())
    except (ValueError, TypeError):
        return "nostay"


def stay_window_label(booking_url):
    """Human-friendly stay window for logs and job listings (e.g. jun15-jun20)."""
    slug = _stay_window_slug(booking_url)
    return slug if slug != "nostay" else "?"


def _debug_artifact_stem(booking_url, park_name, short_tag, file_timestamp=None, job_id=None):
    ts = file_timestamp or datetime.now().strftime("%Y.%m.%d-%H.%M.%S")
    slug = _slugify_park_label(park_name)
    stay = _stay_window_slug(booking_url)
    tag = re.sub(r"[^a-z0-9]", "", (short_tag or "ss").lower())[:16] or "ss"
    jid = re.sub(r"[^a-z0-9]", "", (job_id or "").lower())[:12]
    parts = ["ss", ts, slug, stay]
    if jid:
        parts.append(jid)
    parts.append(tag)
    return "_".join(parts)


def build_debug_screenshot_path(
    booking_url, park_name, short_tag, directory=None, file_timestamp=None, job_id=None
):
    """
    Build a descriptive debug PNG path, e.g.
    ss_2026.04.08-22.03.11_kikomun-creek_jun09-jul14_bcr.png
    With a job id (concurrent jobs): ss_<time>_<park>_<stay>_<jobid>_bcr.png

    short_tag: bcr (before click reserve), acr (after), acs (after click site), mapfail.
    Pass the same file_timestamp for acs/bcr/acr in one attempt so names correlate.
    """
    directory = directory or os.getcwd()
    stem = _debug_artifact_stem(booking_url, park_name, short_tag, file_timestamp, job_id)
    return os.path.join(directory, stem + ".png")


def build_debug_artifact_basename(booking_url, park_name, short_tag, file_timestamp=None, job_id=None):
    """Stem (no extension) for paired .html / .png map-failure dumps."""
    return _debug_artifact_stem(booking_url, park_name, short_tag, file_timestamp, job_id)


def availability_digest(labels):
    """Stable frozenset of normalized site labels for hit-change detection.

    Used to avoid re-notifying (Telegram and SMS) every poll while the set of
    available preferred sites is unchanged; a change re-arms notification.
    """
    return frozenset(
        (s or "").strip().lower() for s in (labels or []) if (s or "").strip()
    )


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
