"""Unified logging: terminal + optional Telegram callback with digest deduplication."""

import os
import sys
import threading

from campslinger.util import current_time

_LOGGER_LOCAL = threading.local()
_TERMINAL_LOG_ENABLED = True
_LOG_TIMESTAMP_ENABLED = True


def set_log_callback(callback):
    _LOGGER_LOCAL.callback = callback


def set_telegram_job_meta(
    job_id,
    interval_seconds,
    park_name=None,
    interval_jitter_seconds=0,
    stay_label=None,
    site_filter=None,
):
    _LOGGER_LOCAL.telegram_job_id = job_id
    _LOGGER_LOCAL.telegram_interval = int(interval_seconds)
    _LOGGER_LOCAL.telegram_interval_jitter = max(0, int(interval_jitter_seconds or 0))
    _LOGGER_LOCAL.park_name = (park_name or "").strip() or None
    _LOGGER_LOCAL.stay_label = (stay_label or "").strip() or None
    _LOGGER_LOCAL.site_filter = (site_filter or "").strip() or None
    _LOGGER_LOCAL.poll_digest = None


def set_job_log_context(
    park_name=None,
    stay_label=None,
    site_filter=None,
    interval_seconds=60,
    interval_jitter_seconds=0,
    job_id=None,
):
    """Set per-thread log context for CLI jobs and Telegram worker threads."""
    if job_id is not None:
        _LOGGER_LOCAL.telegram_job_id = job_id
    _LOGGER_LOCAL.telegram_interval = int(interval_seconds)
    _LOGGER_LOCAL.telegram_interval_jitter = max(0, int(interval_jitter_seconds or 0))
    _LOGGER_LOCAL.park_name = (park_name or "").strip() or None
    _LOGGER_LOCAL.stay_label = (stay_label or "").strip() or None
    _LOGGER_LOCAL.site_filter = (site_filter or "").strip() or None
    _LOGGER_LOCAL.poll_digest = None


def set_park_name(name):
    """CLI convenience: set park name on the main thread (no job meta needed)."""
    _LOGGER_LOCAL.park_name = (name or "").strip() or None


def set_terminal_log_enabled(enabled):
    global _TERMINAL_LOG_ENABLED
    _TERMINAL_LOG_ENABLED = bool(enabled)


def terminal_log_enabled():
    """Live read of the flag.  Use this from other modules instead of
    importing _TERMINAL_LOG_ENABLED by name (which captures a stale copy)."""
    return _TERMINAL_LOG_ENABLED


def set_log_timestamp_enabled(enabled):
    global _LOG_TIMESTAMP_ENABLED
    _LOG_TIMESTAMP_ENABLED = bool(enabled)


def log_timestamp_enabled():
    return _LOG_TIMESTAMP_ENABLED


def configure_log_timestamps(log_timestamp=None):
    """log_timestamp: True=force on, False=force off, None=auto (journald)."""
    if log_timestamp is True:
        set_log_timestamp_enabled(True)
    elif log_timestamp is False:
        set_log_timestamp_enabled(False)
    elif os.environ.get("JOURNAL_STREAM"):
        set_log_timestamp_enabled(False)


def _build_log_prefix():
    parts = []
    park = getattr(_LOGGER_LOCAL, "park_name", None)
    stay = getattr(_LOGGER_LOCAL, "stay_label", None)
    filt = getattr(_LOGGER_LOCAL, "site_filter", None)
    if park:
        parts.append(park)
    if stay:
        parts.append(stay)
    if filt:
        parts.append(filt)
    if parts:
        return "[{}]".format(" | ".join(parts))
    return None


def _format_log_line(message):
    prefix = _build_log_prefix()
    if prefix:
        body = "{} {}".format(prefix, message)
    else:
        body = message
    if _LOG_TIMESTAMP_ENABLED:
        return "{} - {}".format(current_time(), body)
    return body


def _bot_console_line(message):
    if _TERMINAL_LOG_ENABLED:
        print(_format_log_line(message), flush=True)


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
    Log a line.  When a Telegram callback is registered (bot mode), mirror
    messages there with optional digest-based deduplication.  In CLI mode
    (no callback), this is a plain timestamped print.
    """
    line = _format_log_line(message)
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
