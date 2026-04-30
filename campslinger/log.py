"""Unified logging: terminal + optional Telegram callback with digest deduplication."""

import sys
import threading

from campslinger.util import current_time

_LOGGER_LOCAL = threading.local()
_TERMINAL_LOG_ENABLED = True


def set_log_callback(callback):
    _LOGGER_LOCAL.callback = callback


def set_telegram_job_meta(job_id, interval_seconds, park_name=None, interval_jitter_seconds=0):
    _LOGGER_LOCAL.telegram_job_id = job_id
    _LOGGER_LOCAL.telegram_interval = int(interval_seconds)
    _LOGGER_LOCAL.telegram_interval_jitter = max(0, int(interval_jitter_seconds or 0))
    _LOGGER_LOCAL.park_name = (park_name or "").strip() or None
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


def _bot_console_line(message):
    if _TERMINAL_LOG_ENABLED:
        print("{} - {}".format(current_time(), message), flush=True)


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
