#!/usr/bin/env python3
"""
Campslinger Telegram bot (campslinger_tg.py): campsite monitoring and optional reservation.

Works with any park on the Aspira / GoingToCamp platform (BC Parks, Ontario Parks,
Parks Canada, Manitoba, Nova Scotia, New Brunswick, NL, Yukon, Michigan, Maryland,
Mississippi, Nebraska, and more).

/menu is the single go-to command: it opens a hub with your active and recent jobs
(each with inline buttons) plus shortcuts to start a monitor or read help.  Everything
else (/monitor, /status, /cancel, /cancelall, /exportall, /help) still works but is
reachable from the menu via buttons.

Primary action is monitoring (API-only polling with notifications).  The "Auto-reserve"
toggle in the wizard's More menu enables Selenium reservation on hit.  "Loop" controls
whether the job keeps running after the first availability hit (continuous) or stops (once).

Operator host flags --rip / --rp attach to Chrome on the same LAN (used only when
Auto-reserve is toggled on).  Warmode targets 07:00 in the job's --timezone (default
US/Pacific).
"""

import argparse
import asyncio
import json
import os
import shlex
import sys
import threading
import traceback
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from campslinger.core import (
    api_available_labels,
    fetch_park_name,
    fetch_sites_map,
    labels_available_matching_filter,
)
from campslinger.log import (
    _bot_console_line,
    configure_log_timestamps,
    pp,
    set_job_log_context,
    set_log_callback,
    set_terminal_log_enabled,
    terminal_log_enabled,
)
from campslinger.reserve_modes import reserve_normal_mode, reserve_war_mode
from campslinger.selenium_ops import setup_webdriver, setup_webdriver_remote
from campslinger.util import (
    availability_digest,
    comma_separated_list,
    current_time,
    randomized_probe_wait_seconds,
    send_sms,
    shorten_url,
    sort_key,
    stay_window_label,
    validate_booking_url,
)
from campslinger import wizard_draft
from campslinger import job_store

_DEFAULT_TIMEZONE = "US/Pacific"
_remote_chrome_lock = threading.Lock()

_REMOTE_CHROME = None
_audit_lock = threading.Lock()
_audit_write_warned = False

_TWILIO_ENV = {
    "twilio_sid": "CAMPSLINGER_TWILIO_SID",
    "twilio_auth_token": "CAMPSLINGER_TWILIO_AUTH_TOKEN",
    "twilio_number": "CAMPSLINGER_TWILIO_NUMBER",
    "my_phone_number": "CAMPSLINGER_MY_PHONE_NUMBER",
}


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


# ---------------------------------------------------------------------------
# Job state / manager
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
    park_name: Optional[str] = None
    stay_label: Optional[str] = None
    site_filter: Optional[str] = None


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
        job_store.archive_job(_job_to_archive_record(job))
        _sync_active_store(self)

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

    def cancel_all_for_user(self, user_id):
        with self._lock:
            cancelled = []
            for job_id, job in list(self.active.items()):
                if job.user_id == user_id:
                    job.stop_event.set()
                    cancelled.append(job_id)
            return cancelled

    def is_active(self, job_id):
        with self._lock:
            return job_id in self.active


# ---------------------------------------------------------------------------
# Process-level argparse (host flags only)
# ---------------------------------------------------------------------------

def _env_max_concurrent():
    v = os.getenv("CAMPSLINGER_MAX_CONCURRENT", "").strip()
    return int(v) if v.isdigit() and int(v) > 0 else 3


def build_telegram_arg_parser():
    p = argparse.ArgumentParser(
        description="Campslinger Telegram bot: campsite monitoring and optional reservation."
    )
    p.add_argument("--max-concurrent", type=int, default=_env_max_concurrent(),
                   help="Max concurrent jobs (env: CAMPSLINGER_MAX_CONCURRENT, default 3).")
    p.add_argument("--no-terminal-log", action="store_false", dest="terminal_log", default=True,
                    help="Do not print job lines to the server terminal.")
    p.add_argument("--rip", "--remote_ip", dest="remote_ip", default=None, metavar="HOST",
                    help="Operator only: Chrome remote debugging host (same LAN). Use with --rp.")
    p.add_argument("--rp", "--remote_port", dest="remote_port", type=int, default=None, metavar="PORT",
                    help="Operator only: remote debugging port (e.g. 9222). Use with --rip.")
    dp_group = p.add_mutually_exclusive_group()
    dp_group.add_argument("--drop-pending-updates", action="store_true", dest="drop_pending_updates",
                          default=True, help="Ignore Telegram updates queued while the bot was down (default).")
    dp_group.add_argument("--keep-pending-updates", action="store_false", dest="drop_pending_updates",
                          help="Process updates that arrived while the bot was offline.")
    ts_group = p.add_mutually_exclusive_group()
    ts_group.add_argument("--log-timestamp", action="store_true", dest="log_timestamp",
                          default=None, help="Prefix log lines with a timestamp (default: auto).")
    ts_group.add_argument("--no-log-timestamp", action="store_false", dest="log_timestamp",
                          help="Omit script timestamps (auto-off under systemd journald).")
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
    parser.add_argument("--warmode-click-delay", "--wcd", type=int, default=0)
    parser.add_argument("--timezone", "--tz", default=_DEFAULT_TIMEZONE)
    parser.add_argument("--debug", "--d", action="store_true", default=False)
    parser.add_argument("--sms", "--s", action="store_true", default=False)
    parser.add_argument("--twilio_sid", "--tsid", default="")
    parser.add_argument("--twilio_auth_token", "--tat", default="")
    parser.add_argument("--twilio_number", "--tn", default="")
    parser.add_argument("--my_phone_number", "--mpn", default="")
    return parser


def _valid_timezone(tz):
    if not tz:
        return False
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz)
        return True
    except Exception:
        pass
    try:
        import pytz
        return tz in pytz.all_timezones_set
    except Exception:
        return True  # can't validate; accept and let warmode surface errors


def parse_bot_monitor_args(raw_text):
    tokens = shlex.split(raw_text)
    if tokens and not tokens[0].startswith("-") and tokens[0].startswith("http"):
        tokens = ["--url", tokens[0]] + tokens[1:]
    parser = build_bot_monitor_parser()
    args = parser.parse_args(tokens)
    if not args.url:
        raise ValueError("Missing URL. Usage: /monitor <url> [--f S51 --i 60 --reserve --loop once ...]")
    validate_booking_url(args.url)
    wcd = int(getattr(args, "warmode_click_delay", 0) or 0)
    if wcd < 0:
        raise ValueError("--warmode-click-delay must be >= 0")
    if wcd and not args.warmode:
        raise ValueError("--warmode-click-delay requires --warmode")
    tz = (getattr(args, "timezone", "") or _DEFAULT_TIMEZONE).strip() or _DEFAULT_TIMEZONE
    if not _valid_timezone(tz):
        raise ValueError("Unknown --timezone {!r} (use an IANA zone like America/Toronto)".format(tz))
    args.timezone = tz
    args.job_kind = "reserve" if args.reserve else "monitor"
    return args


# ---------------------------------------------------------------------------
# Twilio env defaults + job display / export helpers
# ---------------------------------------------------------------------------

def _twilio_from_env():
    return {k: (os.getenv(env) or "").strip() for k, env in _TWILIO_ENV.items()}


def _twilio_env_ready():
    return all(_twilio_from_env().values())


def _apply_twilio_env_to_args(args):
    for field, value in _twilio_from_env().items():
        if value and not getattr(args, field, ""):
            setattr(args, field, value)


def _args_to_monitor_state(args):
    return {
        "url": args.url,
        "interval": args.interval,
        "jitter": getattr(args, "jitter", 10),
        "filter": list(args.filter) if args.filter else None,
        "reserve": bool(args.reserve),
        "loop": args.loop,
        "warmode": bool(getattr(args, "warmode", False)),
        "warmode_click_delay": int(getattr(args, "warmode_click_delay", 0) or 0),
        "timezone": (getattr(args, "timezone", "") or _DEFAULT_TIMEZONE),
        "debug": bool(getattr(args, "debug", False)),
        "sms": bool(args.sms),
        "twilio_sid": args.twilio_sid or "",
        "twilio_auth_token": args.twilio_auth_token or "",
        "twilio_number": args.twilio_number or "",
        "my_phone_number": args.my_phone_number or "",
    }


_STORE_SECRET_FIELDS = ("twilio_sid", "twilio_auth_token", "twilio_number", "my_phone_number")


def _job_to_store_record(job):
    """Build a secret-free dict suitable for job_store persistence."""
    state = {k: v for k, v in _args_to_monitor_state(job.args).items()
             if k not in _STORE_SECRET_FIELDS}
    return {
        "job_id": job.job_id,
        "user_id": job.user_id,
        "chat_id": job.chat_id,
        "queued_at": job.started_at,
        "state": state,
    }


def _job_to_archive_record(job):
    """Build a secret-free archive record with outcome fields."""
    state = {k: v for k, v in _args_to_monitor_state(job.args).items()
             if k not in _STORE_SECRET_FIELDS}
    return {
        "job_id": job.job_id,
        "user_id": job.user_id,
        "chat_id": job.chat_id,
        "queued_at": job.started_at,
        "ended_at": job.ended_at,
        "status": job.status,
        "result_site": job.result_site,
        "park_name": job.park_name,
        "stay_label": job.stay_label,
        "state": state,
    }


def _sync_active_store(manager):
    """Persist the current active job list to disk (best-effort)."""
    records = [_job_to_store_record(j) for j in manager.list_active()]
    job_store.sync_store(records)


def monitor_args_to_command(args):
    """Copy-pasteable /monitor line; never includes Twilio secrets."""
    parts = ["/monitor", args.url]
    if args.filter:
        parts.append("--f {}".format(",".join(args.filter)))
    if int(args.interval or 60) != 60:
        parts.append("--i {}".format(args.interval))
    jv = max(0, int(getattr(args, "jitter", 10) or 10))
    if jv != 10:
        parts.append("--jitter {}".format(jv))
    if args.reserve:
        parts.append("--reserve")
    if args.loop != "continuous":
        parts.append("--loop {}".format(args.loop))
    if getattr(args, "warmode", False):
        parts.append("--warmode")
        wcd = int(getattr(args, "warmode_click_delay", 0) or 0)
        if wcd > 0:
            parts.append("--warmode-click-delay {}".format(wcd))
        tz = (getattr(args, "timezone", "") or _DEFAULT_TIMEZONE)
        if tz != _DEFAULT_TIMEZONE:
            parts.append("--timezone {}".format(tz))
    if getattr(args, "debug", False):
        parts.append("--debug")
    if args.sms:
        parts.append("--sms")
    return " ".join(parts)


def _populate_job_metadata_fast(job):
    """Cheap metadata (no network) so the queue acknowledgement is instant."""
    job.stay_label = stay_window_label(job.args.url)
    job.site_filter = ",".join(job.args.filter) if job.args.filter else None


def _populate_job_metadata(job):
    if not job.stay_label:
        job.stay_label = stay_window_label(job.args.url)
    if job.site_filter is None and job.args.filter:
        job.site_filter = ",".join(job.args.filter)
    job.park_name = fetch_park_name(job.args.url) or "Unknown park"


def _job_filter_display(job):
    return job.site_filter or "all"


def _job_kind_label(job):
    if getattr(job.args, "reserve", False):
        return "warmode" if getattr(job.args, "warmode", False) else "reserve"
    return "monitor"


_STATUS_LABELS = {
    "running": "▶️ running",
    "done": "✅ done",
    "success": "✅ reserved",
    "failed": "❌ no reservation",
    "cancelled": "🛑 cancelled",
    "error": "⚠️ error",
}


def _status_label(status):
    return _STATUS_LABELS.get(status, status)


def _job_brief_line(job):
    return "{} · {} · {} · {} · {} · {}".format(
        job.job_id, _job_kind_label(job), job.park_name or "?", job.stay_label or "?",
        _job_filter_display(job), _status_label(job.status))


def _job_is_active(job, manager):
    return manager.is_active(job.job_id)


def _format_status_text(job):
    if not job:
        return "Job not found or not yours."
    opts = []
    if job.args.loop != "continuous":
        opts.append("loop={}".format(job.args.loop))
    if getattr(job.args, "warmode", False):
        tz = getattr(job.args, "timezone", _DEFAULT_TIMEZONE) or _DEFAULT_TIMEZONE
        opts.append("warmode@07:00 {}".format(tz))
        wcd = int(getattr(job.args, "warmode_click_delay", 0) or 0)
        if wcd:
            opts.append("wcd={}ms".format(wcd))
    if job.args.sms:
        opts.append("sms")
    lines = [
        "Job {} — {}".format(job.job_id, _status_label(job.status)),
        "{} · {} · sites {}".format(
            job.park_name or "?", job.stay_label or "?", _job_filter_display(job)),
        "{} · every ~{}s{}".format(
            _job_kind_label(job), getattr(job.args, "interval", 60),
            " · " + ", ".join(opts) if opts else ""),
        "started {}".format(job.started_at),
    ]
    if job.ended_at:
        lines.append("ended {}".format(job.ended_at))
    if job.result_site:
        lines.append("reserved: {}".format(job.result_site))
    if job.error:
        lines.append("error: {}".format(job.error))
    return "\n".join(lines)


def _recent_finished_for_user(manager, user_id, count=5):
    active_ids = {j.job_id for j in manager.list_active_for_user(user_id)}
    return [j for j in manager.list_recent_for_user(user_id, count) if j.job_id not in active_ids]


def _jobs_overview_text(manager, user_id, include_recent=True):
    active = manager.list_active_for_user(user_id)
    total_active = len(manager.list_active())
    lines = ["Menu — your running jobs ({}, server {}/{}):".format(
        len(active), total_active, manager.max_concurrent)]
    if active:
        for job in active:
            lines.append("• " + _job_brief_line(job))
    else:
        lines.append("No running jobs. Tap 📡 Monitor to start one.")
    if include_recent:
        finished = _recent_finished_for_user(manager, user_id, 5)
        if finished:
            lines.append("")
            lines.append("Recent (tap a job for details / restart):")
            for job in finished:
                lines.append("• " + _job_brief_line(job))
    return "\n".join(lines)


def _jobs_overview_keyboard(manager, user_id):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    active = manager.list_active_for_user(user_id)
    rows = []
    for job in active:
        label = "{} {} {}".format(job.job_id, job.stay_label or "?", _job_filter_display(job))
        if len(label) > 60:
            label = label[:57] + "..."
        rows.append([InlineKeyboardButton("📌 " + label, callback_data="j:d:{}".format(job.job_id))])
    if active:
        rows.append([
            InlineKeyboardButton("🛑 Cancel all", callback_data="j:ca"),
            InlineKeyboardButton("🗂 Export all", callback_data="j:xa"),
        ])
    finished = _recent_finished_for_user(manager, user_id, 5)
    for job in finished:
        label = "{} {} {}".format(job.job_id, job.stay_label or "?", _status_label(job.status))
        if len(label) > 60:
            label = label[:57] + "..."
        rows.append([InlineKeyboardButton("↩️ " + label, callback_data="j:d:{}".format(job.job_id))])
    if finished:
        rows.append([
            InlineKeyboardButton("🔁 Restart recent", callback_data="j:rr"),
            InlineKeyboardButton("🗂 Export recent", callback_data="j:xr"),
        ])
    rows.append([
        InlineKeyboardButton("📡 Monitor", callback_data="m:mo"),
        InlineKeyboardButton("📂 History", callback_data="h:list:0"),
        InlineKeyboardButton("❓ Help", callback_data="m:h"),
    ])
    return InlineKeyboardMarkup(rows)


def _job_end_keyboard(job_id, can_restart=True):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    row = [InlineKeyboardButton("📋 Export", callback_data="j:x:{}".format(job_id))]
    if can_restart:
        row.insert(0, InlineKeyboardButton("🔁 Restart", callback_data="j:r:{}".format(job_id)))
    return InlineKeyboardMarkup([row, [InlineKeyboardButton("📋 Menu", callback_data="j:l")]])


def _job_detail_keyboard(job, manager):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    jid = job.job_id
    is_active = _job_is_active(job, manager)
    rows = [
        [
            InlineKeyboardButton("📊 Status", callback_data="j:s:{}".format(jid)),
            InlineKeyboardButton("📋 Export", callback_data="j:x:{}".format(jid)),
        ],
    ]
    row2 = []
    if is_active:
        row2.append(InlineKeyboardButton("🛑 Cancel", callback_data="j:c:{}".format(jid)))
    else:
        row2.append(InlineKeyboardButton("🔁 Restart", callback_data="j:r:{}".format(jid)))
    row2.append(InlineKeyboardButton("✏️ Edit", callback_data="j:e:{}".format(jid)))
    rows.append(row2)
    rows.append([InlineKeyboardButton("🔙 Jobs", callback_data="j:l")])
    return InlineKeyboardMarkup(rows)


def _sms_field_label(field, rb):
    if rb.get(field):
        return "✓"
    env_key = _TWILIO_ENV.get(field)
    if env_key and os.getenv(env_key, "").strip():
        return "env"
    return "✗"


def _args_for_restart_shlex(args):
    state = _args_to_monitor_state(args)
    state["twilio_sid"] = ""
    state["twilio_auth_token"] = ""
    state["twilio_number"] = ""
    state["my_phone_number"] = ""
    return monitor_state_to_shlex_raw(state)


def _setup_job_log_context(job):
    set_job_log_context(
        park_name=job.park_name,
        stay_label=job.stay_label,
        site_filter=job.site_filter,
        interval_seconds=job.args.interval,
        interval_jitter_seconds=getattr(job.args, "jitter", 0),
        job_id=job.job_id,
    )


# ---------------------------------------------------------------------------
# Telegram UI constants and keyboards
# ---------------------------------------------------------------------------

UD_PENDING = "csl_pending"
UD_MONITOR = "csl_monitor"


def _persist_wizard(context, uid):
    """Best-effort save of the in-progress wizard (opt-in; secrets stripped)."""
    if not wizard_draft.persist_enabled():
        return
    state = context.user_data.get(UD_MONITOR)
    if state and state.get("url"):
        wizard_draft.save_draft(uid, state, pending=context.user_data.get(UD_PENDING))


def _drop_wizard_draft(uid):
    if wizard_draft.persist_enabled():
        wizard_draft.delete_draft(uid)


async def _site_label_hint(url):
    """Best-effort sample of available site labels for the URL (non-fatal)."""
    try:
        sites = await asyncio.to_thread(fetch_sites_map, url)
    except Exception:
        return ""
    avail = api_available_labels(sites)
    total = len(sites)
    if avail:
        sample = ",".join(avail[:8])
        more = " …" if len(avail) > 8 else ""
        return "\n\n{} site(s) on map; available now: {}{}".format(total, sample, more)
    if total:
        return "\n\n{} site(s) on map; none available right now.".format(total)
    return ""


def _main_menu_keyboard():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📡 Monitor", callback_data="m:mo"),
            InlineKeyboardButton("📋 Menu", callback_data="m:menu"),
            InlineKeyboardButton("❓ Help", callback_data="m:h"),
        ],
    ])


def _job_control_keyboard(job_id):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Status", callback_data="j:s:{}".format(job_id)),
            InlineKeyboardButton("🛑 Cancel", callback_data="j:c:{}".format(job_id)),
        ],
        [
            InlineKeyboardButton("📋 Export", callback_data="j:x:{}".format(job_id)),
            InlineKeyboardButton("📋 Jobs", callback_data="j:l"),
        ],
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


def _resume_draft_keyboard():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("▶️ Go", callback_data="r:g"),
            InlineKeyboardButton("⚙️ More", callback_data="r:m"),
        ],
        [
            InlineKeyboardButton("🆕 New URL", callback_data="r:dd"),
            InlineKeyboardButton("❌ Cancel wizard", callback_data="r:x"),
        ],
    ])


def _monitor_more_menu_keyboard(rb):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    reserve_on = rb.get("reserve", False)
    reserve_label = "⛺ Auto-reserve: ON" if reserve_on else "⛺ Auto-reserve: off"
    loop_label = "🔄 Loop: {}".format(rb.get("loop", "continuous"))
    sms_label = "📱 SMS: {}".format("ON" if rb.get("sms") else "off")
    sites_val = ",".join(rb["filter"]) if rb.get("filter") else "All"
    iv = int(rb.get("interval") or 60)
    jv = max(0, int(rb.get("jitter") if rb.get("jitter") is not None else 10))
    rows = [
        [InlineKeyboardButton("🎯 Sites: {}".format(sites_val), callback_data="r:o:f")],
        [InlineKeyboardButton("⏱ Interval: {}s".format(iv), callback_data="r:o:i")],
        [InlineKeyboardButton("🎲 Jitter: {}s".format(jv), callback_data="r:o:j")],
        [InlineKeyboardButton(reserve_label, callback_data="r:o:rv")],
    ]
    if reserve_on:
        wm = rb.get("warmode")
        wm_btn = InlineKeyboardButton(
            "🌅 Warmode{}".format(" ✓" if wm else ""), callback_data="r:o:w")
        if wm:
            wcd_val = int(rb.get("warmode_click_delay") or 0)
            delay_btn = InlineKeyboardButton(
                "⏱ WM delay: {}ms".format(wcd_val), callback_data="r:o:wcd")
            rows.append([wm_btn, delay_btn])
            tz_val = rb.get("timezone") or _DEFAULT_TIMEZONE
            rows.append([InlineKeyboardButton("🌐 TZ: {}".format(tz_val), callback_data="r:o:tz")])
        else:
            rows.append([wm_btn])
        rows.append([
            InlineKeyboardButton("🐛 Debug{}".format(" ✓" if rb.get("debug") else ""), callback_data="r:o:d"),
        ])
    rows.extend([
        [InlineKeyboardButton(loop_label, callback_data="r:o:l")],
        [InlineKeyboardButton(sms_label, callback_data="r:o:sms")],
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
    if not sms_on and _twilio_env_ready():
        toggle_label += " (env ready)"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle_label, callback_data="r:s:tog")],
        [InlineKeyboardButton("Twilio SID [{}]".format(_sms_field_label("twilio_sid", rb)), callback_data="r:s:sid")],
        [InlineKeyboardButton("Auth Token [{}]".format(_sms_field_label("twilio_auth_token", rb)), callback_data="r:s:at")],
        [InlineKeyboardButton("Twilio Number [{}]".format(_sms_field_label("twilio_number", rb)), callback_data="r:s:tn")],
        [InlineKeyboardButton("Your Phone [{}]".format(_sms_field_label("my_phone_number", rb)), callback_data="r:s:mpn")],
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
        "url": url, "interval": 60, "jitter": 10, "filter": None,
        "reserve": False, "loop": "continuous", "warmode": False,
        "warmode_click_delay": 0, "timezone": _DEFAULT_TIMEZONE, "debug": False,
        "sms": False, "twilio_sid": "", "twilio_auth_token": "",
        "twilio_number": "", "my_phone_number": "",
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
        wcd = int(rb.get("warmode_click_delay") or 0)
        if wcd > 0:
            parts.append("--warmode-click-delay {}".format(wcd))
        tz = rb.get("timezone") or _DEFAULT_TIMEZONE
        if tz != _DEFAULT_TIMEZONE:
            parts.append("--timezone {}".format(tz))
    if rb.get("debug"):
        parts.append("--debug")
    if rb.get("sms"):
        parts.append("--sms …")
    return " ".join(parts)


def monitor_state_to_shlex_raw(rb):
    chunks = [shlex.quote(rb["url"])]
    if rb.get("filter"):
        chunks.extend(["--f", shlex.quote(",".join(rb["filter"]))])
    iv = int(rb.get("interval") or 60)
    if iv != 60:
        chunks.extend(["--i", str(iv)])
    jv = max(0, int(rb.get("jitter") if rb.get("jitter") is not None else 10))
    if jv != 10:
        chunks.extend(["--jitter", str(jv)])
    if rb.get("reserve"):
        chunks.append("--reserve")
    if rb.get("loop", "continuous") != "continuous":
        chunks.extend(["--loop", rb["loop"]])
    if rb.get("warmode"):
        chunks.append("--warmode")
        wcd = int(rb.get("warmode_click_delay") or 0)
        if wcd > 0:
            chunks.extend(["--warmode-click-delay", str(wcd)])
        tz = rb.get("timezone") or _DEFAULT_TIMEZONE
        if tz != _DEFAULT_TIMEZONE:
            chunks.extend(["--timezone", shlex.quote(tz)])
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
    except Exception as e:
        _bot_console_line("Telegram send to chat {} failed: {!r}".format(chat_id, e))


def _run_monitor_job(job, manager, bot, loop):
    def _tg(line):
        _send_telegram_text(bot, loop, job.chat_id, line)

    set_log_callback(_tg)
    if not job.park_name:
        _populate_job_metadata(job)
    _setup_job_log_context(job)
    args = job.args
    _apply_twilio_env_to_args(args)
    client = None
    if args.sms:
        if not (args.twilio_sid and args.twilio_auth_token and args.twilio_number and args.my_phone_number):
            _send_telegram_text(bot, loop, job.chat_id,
                                "❌ SMS enabled but Twilio credentials missing (wizard or env)")
            manager.mark_done(job.job_id, "error", error="missing_twilio_creds")
            audit_log("job_aborted", user_id=job.user_id, chat_id=job.chat_id,
                      job_id=job.job_id, reason="missing_twilio_creds", url=args.url)
            set_log_callback(None)
            return
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

    park = job.park_name
    loop_mode = getattr(args, "loop", "continuous")
    last_hit = None
    try:
        while True:
            if job.stop_event.is_set():
                break
            wait_s = randomized_probe_wait_seconds(args.interval, args.jitter)
            try:
                sites = fetch_sites_map(args.url)
            except Exception as e:
                pp("❌ API poll failed ({}): {}".format(type(e).__name__, e),
                   telegram_digest=("api_err", type(e).__name__, str(e)[:200]))
                if job.stop_event.wait(wait_s):
                    break
                continue
            matching = labels_available_matching_filter(sites, args.filter)
            all_avail = api_available_labels(sites)
            if matching:
                new_hit = availability_digest(matching)
                changed = new_hit != last_hit
                last_hit = new_hit
                if loop_mode == "once":
                    pp("✅ Available sites: {}".format(",".join(matching)), telegram_digest=None)
                else:
                    pp("✅ Available sites: {} — checking again in ~{}s".format(
                        ",".join(matching), wait_s), telegram_digest=("hit", new_hit))
                # SMS costs money: only on availability transitions (or loop=once).
                if args.sms and client and (loop_mode == "once" or changed):
                    try:
                        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        prefix = "[{}] ".format(park) if park else ""
                        body = "{} - {}Available sites: {}\n{}".format(
                            ts, prefix, ",".join(matching), shorten_url(args.url))
                        send_sms(body, client, args.my_phone_number, args.twilio_number)
                    except Exception as e:
                        pp("❌ SMS failed: {}".format(e), telegram_digest=None)
                elif args.sms and client and not changed:
                    pp("SMS skipped (same availability as last hit)", skip_telegram=True)
                if loop_mode == "once":
                    pp("✅ Monitor job finished (loop=once, first hit).", telegram_digest=None)
                    manager.mark_done(job.job_id, "done", site=",".join(matching))
                    audit_log("job_finished", user_id=job.user_id, chat_id=job.chat_id,
                              job_id=job.job_id, status="done", url=args.url, job_kind="monitor")
                    _send_telegram_text(
                        bot, loop, job.chat_id,
                        "✅ Job {} done — available: {}".format(job.job_id, ",".join(matching)),
                        reply_markup=_job_end_keyboard(job.job_id))
                    set_log_callback(None)
                    return
            elif not all_avail:
                last_hit = None
                pp("No availability. Checking again in ~{}s".format(wait_s), telegram_digest=("zero",))
            else:
                last_hit = None
                labels_csv = ",".join(sorted(all_avail, key=sort_key))
                pp("✨ Available: {}\n❌ None of your preferred sites ({}) are free. Checking again in ~{}s".format(
                    labels_csv, ",".join(args.filter or []), wait_s),
                    telegram_digest=("filter_wait", frozenset(all_avail), tuple(args.filter or ())))
            if job.stop_event.wait(wait_s):
                break

        if job.stop_event.is_set():
            manager.mark_done(job.job_id, "cancelled")
            _bot_console_line("Job {} cancelled (monitor) user={}".format(job.job_id, job.user_id))
            _send_telegram_text(bot, loop, job.chat_id, "🛑 Job {} cancelled".format(job.job_id),
                                reply_markup=_job_end_keyboard(job.job_id))
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
    def _tg(line):
        _send_telegram_text(bot, loop, job.chat_id, line)

    set_log_callback(_tg)
    if not job.park_name:
        _populate_job_metadata(job)
    _setup_job_log_context(job)
    args = job.args
    _apply_twilio_env_to_args(args)
    client = None
    if args.sms:
        if not (args.twilio_sid and args.twilio_auth_token and args.twilio_number and args.my_phone_number):
            _send_telegram_text(bot, loop, job.chat_id,
                                "❌ SMS enabled but Twilio credentials missing (wizard or env)")
            manager.mark_done(job.job_id, "error", error="missing_twilio_creds")
            audit_log("job_aborted", user_id=job.user_id, chat_id=job.chat_id,
                      job_id=job.job_id, reason="missing_twilio_creds", url=args.url)
            set_log_callback(None)
            return
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

    use_remote = bool(_REMOTE_CHROME)
    if use_remote:
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

    # A shared remote Chrome can't drive two reservations at once; serialize.
    sel_lock = _remote_chrome_lock if use_remote else None
    if sel_lock:
        sel_lock.acquire()
    try:
        if args.warmode:
            reserved_site, reason = reserve_war_mode(
                driver, args.url, args.filter,
                timezone=getattr(args, "timezone", _DEFAULT_TIMEZONE) or _DEFAULT_TIMEZONE,
                debug=args.debug, stop_event=job.stop_event,
                warmode_click_delay_ms=int(getattr(args, "warmode_click_delay", 0) or 0),
            )
        else:
            reserved_site, reason = reserve_normal_mode(
                driver, args.url, args.filter,
                interval=args.interval, interval_jitter=args.jitter,
                debug=args.debug, stop_event=job.stop_event)
        if job.stop_event.is_set():
            manager.mark_done(job.job_id, "cancelled")
            _bot_console_line("Job {} cancelled (reserve) user={}".format(job.job_id, job.user_id))
            _send_telegram_text(bot, loop, job.chat_id, "🛑 Job {} cancelled".format(job.job_id),
                                reply_markup=_job_end_keyboard(job.job_id))
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
                                "✅ Job {} success. Reserved: {}".format(job.job_id, reserved_site),
                                reply_markup=_job_end_keyboard(job.job_id))
            audit_log("job_finished", user_id=job.user_id, chat_id=job.chat_id,
                      job_id=job.job_id, status="success", result_site=reserved_site, url=args.url)
        else:
            manager.mark_done(job.job_id, "failed", error=reason or "no_reservation")
            _send_telegram_text(bot, loop, job.chat_id,
                                "❌ Job {} finished without reservation (reason: {})".format(
                                    job.job_id, reason or "unknown"),
                                reply_markup=_job_end_keyboard(job.job_id))
            audit_log("job_finished", user_id=job.user_id, chat_id=job.chat_id,
                      job_id=job.job_id, status="failed", error=reason or "no_reservation", url=args.url)
    except ImportError as e:
        manager.mark_done(job.job_id, "error", error="missing_dependency")
        _send_telegram_text(bot, loop, job.chat_id,
                            "❌ Job {} error: missing dependency ({}). Warmode needs pytz.".format(job.job_id, e))
        audit_log("job_finished", user_id=job.user_id, chat_id=job.chat_id,
                  job_id=job.job_id, status="error", error="missing_dependency: {}".format(e), url=args.url)
    except Exception as e:
        manager.mark_done(job.job_id, "error", error=str(e))
        _send_telegram_text(bot, loop, job.chat_id, "❌ Job {} error: {}".format(job.job_id, e))
        audit_log("job_finished", user_id=job.user_id, chat_id=job.chat_id,
                  job_id=job.job_id, status="error", error=str(e), url=args.url)
    finally:
        if sel_lock:
            sel_lock.release()
        # Don't quit a shared remote Chrome out from under other jobs.
        if not use_remote:
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
        "Campslinger — /menu is all you need\n\n"
        "Open /menu to see your active and recent jobs, each with buttons:\n"
        "Status · Cancel · Export · Edit · Restart, plus Cancel all / Export all.\n"
        "Tap 📡 Monitor to start a job (paste a URL), or send a booking URL anytime.\n\n"
        "Power-user one-liner:\n"
        "/monitor <url> [--f S51,S52] [--i 60] [--jitter 10] [--reserve] "
        "[--loop once|continuous] [--warmode [--warmode-click-delay MS] [--timezone TZ]] "
        "[--debug] [--sms]\n\n"
        "Other commands (also reachable from /menu buttons):\n"
        "/status <job_id> · /cancel <job_id> · /cancelall · /exportall\n\n"
        "Defaults: API-only monitoring, continuous loop.  --reserve (Auto-reserve)\n"
        "switches the job to Selenium and clicks Reserve on hit.  --loop once stops\n"
        "after the first hit.  Warmode fires at 07:00 in --timezone (default\n"
        "US/Pacific); --warmode-click-delay (ms) waits briefly after the open time.\n\n"
        "SMS: toggle in the wizard; Twilio creds can live in server env (see\n"
        ".env.example).  In continuous mode SMS is sent on availability changes,\n"
        "not every poll.  Credentials are never written to the audit log.\n\n"
        "Before a reboot: /exportall, save the lines, paste them back afterwards.\n"
        "Operator note: with --rip/--rp set, run the bot with --max-concurrent 1\n"
        "since all jobs share one Chrome session."
    )


async def _tg_reply(update, text, reply_markup=None):
    em = update.effective_message
    if em:
        await em.reply_text(text, reply_markup=reply_markup)
        return True
    return False


# ---------------------------------------------------------------------------
# Restore helper (starts a job without a Telegram update object)
# ---------------------------------------------------------------------------

def _start_job_from_record(record, manager, bot, ev_loop):
    """Start a job from a persisted store record. Returns the JobState or None."""
    state = record.get("state", {})
    try:
        raw = monitor_state_to_shlex_raw(state)
        job_args = parse_bot_monitor_args(raw)
    except Exception as e:
        _bot_console_line("Restore skip (parse error): {!r}".format(e))
        return None
    job = manager.create(record.get("chat_id", 0), record.get("user_id", 0), job_args)
    if not job:
        _bot_console_line("Restore skip (capacity): {}".format(record.get("job_id", "?")))
        return None
    _populate_job_metadata_fast(job)
    _apply_twilio_env_to_args(job.args)
    t = threading.Thread(target=_run_job_dispatch, args=(job, manager, bot, ev_loop), daemon=True)
    job.thread = t
    t.start()
    _bot_console_line("Restored job {} for user {} ({})".format(
        job.job_id, record.get("user_id", "?"), job.stay_label))
    return job


# ---------------------------------------------------------------------------
# Telegram bot entry
# ---------------------------------------------------------------------------

def run_telegram_bot(args):
    from telegram.ext import (
        ApplicationBuilder, CallbackQueryHandler, CommandHandler,
        ContextTypes, MessageHandler, filters,
    )

    set_terminal_log_enabled(args.terminal_log)
    configure_log_timestamps(getattr(args, "log_timestamp", None))

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
        if terminal_log_enabled() and err is not None:
            traceback.print_exception(type(err), err, err.__traceback__, file=sys.stderr)
        if update and authorized(update):
            em = getattr(update, "effective_message", None)
            if em:
                try:
                    await em.reply_text("Error handling this update. If it persists, check server logs.")
                except Exception:
                    pass

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
            await _tg_reply(update,
                            "⚠️ {}\n\nSend /help for the full command reference, or tap 📡 Monitor for the wizard.".format(e),
                            reply_markup=_main_menu_keyboard())
            return
        job = manager.create(update.effective_chat.id, update.effective_user.id, job_args)
        if not job:
            audit_log("job_rejected_busy",
                      user_id=uid if isinstance(uid, int) else None,
                      chat_id=update.effective_chat.id if update.effective_chat else None,
                      max_concurrent=manager.max_concurrent)
            await _tg_reply(update, "Server busy: max {} concurrent jobs reached.".format(manager.max_concurrent))
            return
        # Cheap metadata now (no network) so the ack is instant; the worker
        # resolves the park name when it starts.
        _populate_job_metadata_fast(job)
        _apply_twilio_env_to_args(job.args)
        audit_log("job_queued", user_id=job.user_id, chat_id=job.chat_id,
                  job_id=job.job_id, url=job_args.url, job_kind=job_args.job_kind,
                  reserve=bool(job_args.reserve), loop=job_args.loop,
                  warmode=bool(getattr(job_args, "warmode", False)),
                  interval=job_args.interval,
                  filter=",".join(job_args.filter) if job_args.filter else "",
                  stay=job.stay_label)
        ev_loop = asyncio.get_running_loop()
        t = threading.Thread(target=_run_job_dispatch, args=(job, manager, app.bot, ev_loop), daemon=True)
        job.thread = t
        t.start()
        _sync_active_store(manager)
        _bot_console_line("Started job {} for user {} ({})".format(
            job.job_id, uid, job.stay_label))
        if job_args.job_kind == "monitor":
            mode = "monitor ({})".format(job_args.loop)
        else:
            mode = "reserve{}".format(" (warmode)" if job_args.warmode else "")
        await _tg_reply(update, "Started job {}\n{} · sites={}\nkind={} mode={}".format(
            job.job_id, job.stay_label, _job_filter_display(job),
            job_args.job_kind, mode))
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Job {} - quick actions:".format(job.job_id),
            reply_markup=_job_control_keyboard(job.job_id))

    def _status_for_user(jid, requester_user_id):
        job = manager.get_for_user(jid, requester_user_id)
        return _format_status_text(job)

    async def _send_jobs_overview(update, user_id, include_recent=True):
        text = _jobs_overview_text(manager, user_id, include_recent=include_recent)
        await _tg_reply(update, text, reply_markup=_jobs_overview_keyboard(manager, user_id))

    async def help_cmd(update, context: ContextTypes.DEFAULT_TYPE):
        if not authorized(update):
            await reject(update)
            return
        uid = update.effective_user.id if update.effective_user else None
        _bot_console_line("/help user={}".format(uid if uid is not None else "?"))
        audit_log("command_help", user_id=uid, chat_id=update.effective_chat.id if update.effective_chat else None)
        await _tg_reply(update, telegram_help_text(), reply_markup=_main_menu_keyboard())

    async def start_cmd(update, context: ContextTypes.DEFAULT_TYPE):
        if not authorized(update):
            await reject(update)
            return
        uid = update.effective_user.id if update.effective_user else None
        _bot_console_line("/start user={}".format(uid if uid is not None else "?"))
        audit_log("command_start", user_id=uid,
                  chat_id=update.effective_chat.id if update.effective_chat else None)
        await _send_jobs_overview(update, uid)

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
            await _tg_reply(update,
                            "Send your park booking results URL (e.g. https://camping.bcparks.ca/create-booking/...).\n"
                            "Or type a full /monitor … command. Tap ❓ Help for all options.",
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
        await _send_jobs_overview(update, uid)

    async def menu_cmd(update, context: ContextTypes.DEFAULT_TYPE):
        if not authorized(update):
            await reject(update)
            return
        uid = update.effective_user.id if update.effective_user else None
        _bot_console_line("/menu user={}".format(uid if uid is not None else "?"))
        audit_log("command_menu", user_id=uid, chat_id=update.effective_chat.id if update.effective_chat else None)
        await _send_jobs_overview(update, uid)

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
        await _tg_reply(update, _status_for_user(jid, uid))

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

    async def cancelall_cmd(update, context: ContextTypes.DEFAULT_TYPE):
        if not authorized(update):
            await reject(update)
            return
        uid = update.effective_user.id if update.effective_user else None
        cid = update.effective_chat.id if update.effective_chat else None
        cancelled = manager.cancel_all_for_user(uid)
        audit_log("command_cancelall", user_id=uid, chat_id=cid, count=len(cancelled),
                  job_ids=",".join(cancelled))
        if cancelled:
            _bot_console_line("/cancelall {} jobs user={}".format(len(cancelled), uid))
            await _tg_reply(update, "Cancellation requested for {} job(s):\n{}".format(
                len(cancelled), ", ".join(cancelled)))
        else:
            await _tg_reply(update, "No active jobs to cancel.")

    async def exportall_cmd(update, context: ContextTypes.DEFAULT_TYPE):
        if not authorized(update):
            await reject(update)
            return
        uid = update.effective_user.id if update.effective_user else None
        cid = update.effective_chat.id if update.effective_chat else None
        active = manager.list_active_for_user(uid)
        audit_log("command_exportall", user_id=uid, chat_id=cid, count=len(active))
        if not active:
            await _tg_reply(update, "No active jobs to export.")
            return
        lines = [monitor_args_to_command(j.args) for j in active]
        note = ""
        if any(j.args.sms for j in active):
            note = "\n\n(SMS jobs use --sms; Twilio credentials come from server env.)"
        await _tg_reply(update, "Copy to re-run after reboot:\n\n```\n{}\n```{}".format(
            "\n".join(lines), note), parse_mode="Markdown")

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
                await _tg_reply(update, _status_for_user(jid, uid))
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
                await _tg_reply(update, "Please send a park booking URL (e.g. https://camping.bcparks.ca/create-booking/...) or type abort.")
                return
            try:
                validate_booking_url(txt)
            except ValueError as e:
                await _tg_reply(update, "Invalid URL: {}".format(e))
                return
            context.user_data.pop(UD_PENDING, None)
            context.user_data[UD_MONITOR] = _default_monitor_state(txt)
            rb = context.user_data[UD_MONITOR]
            _persist_wizard(context, uid)
            hint = await _site_label_hint(txt)
            await _tg_reply(update,
                            "URL saved.{}\n\nCurrent command:\n{}\n\n▶️ Go = run with defaults.\n⚙️ More = sites, interval, auto-reserve, loop, SMS …".format(
                                hint, format_monitor_command_preview(rb)),
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
            _persist_wizard(context, uid)
            await _tg_reply(update, "Updated.\n\n{}\n\nUse More to change more options or Run.".format(
                format_monitor_command_preview(rb)), reply_markup=_monitor_more_menu_keyboard(rb))
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
            _persist_wizard(context, uid)
            await _tg_reply(update, "Updated.\n\n{}".format(format_monitor_command_preview(rb)),
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
            _persist_wizard(context, uid)
            await _tg_reply(update, "Updated.\n\n{}".format(format_monitor_command_preview(rb)),
                            reply_markup=_monitor_more_menu_keyboard(rb))
            return

        if pend == "r_wcd":
            context.user_data.pop(UD_PENDING, None)
            rb = context.user_data.get(UD_MONITOR)
            if not rb:
                await _tg_reply(update, "Wizard expired.")
                return
            if not rb.get("warmode"):
                await _tg_reply(update, "Warmode is off; turn it on in More first.")
                return
            try:
                ms = int(txt.split()[0])
            except ValueError:
                await _tg_reply(update, "Send a whole number of milliseconds (0–120000), or open More again.")
                return
            rb["warmode_click_delay"] = max(0, min(120000, ms))
            _persist_wizard(context, uid)
            await _tg_reply(update, "Updated.\n\n{}".format(format_monitor_command_preview(rb)),
                            reply_markup=_monitor_more_menu_keyboard(rb))
            return

        if pend == "r_tz":
            context.user_data.pop(UD_PENDING, None)
            rb = context.user_data.get(UD_MONITOR)
            if not rb:
                await _tg_reply(update, "Wizard expired.")
                return
            tz = txt.strip()
            if not _valid_timezone(tz):
                await _tg_reply(update,
                                "Unknown timezone {!r}. Use an IANA name like America/Toronto or US/Pacific.".format(tz))
                return
            rb["timezone"] = tz
            _persist_wizard(context, uid)
            await _tg_reply(update, "Updated.\n\n{}".format(format_monitor_command_preview(rb)),
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
                await update.effective_message.reply_text("Saved.\n\nSMS settings:",
                                                         reply_markup=_sms_submenu_keyboard(rb))
            except Exception:
                await _tg_reply(update, "Saved.")
            return

        if txt.startswith("http://") or txt.startswith("https://"):
            try:
                validate_booking_url(txt)
            except ValueError as e:
                await _tg_reply(update, "Invalid URL: {}\n\nTap ❓ Help for supported parks.".format(e),
                                reply_markup=_main_menu_keyboard())
                return
            context.user_data.pop(UD_PENDING, None)
            context.user_data[UD_MONITOR] = _default_monitor_state(txt)
            rb = context.user_data[UD_MONITOR]
            _persist_wizard(context, uid)
            hint = await _site_label_hint(txt)
            await _tg_reply(update,
                            "URL saved.{}\n\nCurrent command:\n{}\n\n▶️ Go = run with defaults.\n⚙️ More = sites, interval, auto-reserve, loop, SMS …".format(
                                hint, format_monitor_command_preview(rb)),
                            reply_markup=_monitor_go_more_keyboard())
            return

        audit_log("text_message", user_id=uid, chat_id=cid, preview=txt[:200])
        await _tg_reply(update, "Send /menu, a booking URL, or tap a button.",
                        reply_markup=_main_menu_keyboard())

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
        rb = context.user_data.get(UD_MONITOR)

        async def _edit_or_reply(text, markup):
            try:
                await q.edit_message_text(text, reply_markup=markup)
            except Exception:
                await q.message.reply_text(text, reply_markup=markup)

        if data == "m:j":
            await ack(); await jobs_cmd(update, context); return
        if data == "m:menu":
            await ack(); await menu_cmd(update, context); return
        if data == "m:h":
            await ack(); await help_cmd(update, context); return
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
            draft = wizard_draft.load_draft(uid)
            if draft:
                state, _pending = draft
                context.user_data[UD_MONITOR] = state
                context.user_data.pop(UD_PENDING, None)
                await q.message.reply_text(
                    "↩️ Resumed your saved draft.\n\n{}\n\n▶️ Go to run · ⚙️ More to edit · 🆕 New URL to start over.".format(
                        format_monitor_command_preview(state)),
                    reply_markup=_resume_draft_keyboard())
                return
            context.user_data.pop(UD_MONITOR, None)
            context.user_data[UD_PENDING] = "r_url"
            await q.message.reply_text(
                "Send your full park booking results URL (e.g. https://camping.bcparks.ca/create-booking/...).\n"
                "Works with BC Parks, Ontario Parks, Parks Canada, and other supported platforms.\n"
                "You can still type a full /monitor … command manually anytime.")
            return
        if data.startswith("j:c:"):
            await ack()
            jid = data[4:].strip()
            ok = manager.cancel_for_user(jid, uid)
            audit_log("command_cancel", user_id=uid, chat_id=cid, job_id=jid, accepted=ok)
            await q.message.reply_text(
                "Cancellation requested for {}".format(jid) if ok else "Job {} not active or not yours".format(jid))
            return
        if data == "j:ca":
            await ack()
            cancelled = manager.cancel_all_for_user(uid)
            audit_log("command_cancelall", user_id=uid, chat_id=cid, count=len(cancelled),
                      job_ids=",".join(cancelled))
            if cancelled:
                _bot_console_line("/cancelall {} jobs user={}".format(len(cancelled), uid))
                await q.message.reply_text("Cancellation requested for {} job(s):\n{}".format(
                    len(cancelled), ", ".join(cancelled)))
            else:
                await q.message.reply_text("No active jobs to cancel.")
            return
        if data == "j:xa":
            await ack()
            active = manager.list_active_for_user(uid)
            audit_log("command_exportall", user_id=uid, chat_id=cid, count=len(active))
            if not active:
                await q.message.reply_text("No active jobs to export.")
                return
            lines = [monitor_args_to_command(j.args) for j in active]
            note = ""
            if any(j.args.sms for j in active):
                note = "\n(SMS jobs use --sms; Twilio credentials come from server env.)"
            await q.message.reply_text("Copy to re-run after reboot:\n\n```\n{}\n```{}".format(
                "\n".join(lines), note), parse_mode="Markdown")
            return
        if data == "j:xr":
            await ack()
            finished = _recent_finished_for_user(manager, uid, 5)
            audit_log("command_export_recent", user_id=uid, chat_id=cid, count=len(finished))
            if not finished:
                await q.message.reply_text("No recent finished jobs to export.")
                return
            lines = [monitor_args_to_command(j.args) for j in finished]
            await q.message.reply_text("Recent finished jobs (copy to re-run):\n\n```\n{}\n```".format(
                "\n".join(lines)), parse_mode="Markdown")
            return
        if data == "j:rr":
            await ack()
            finished = _recent_finished_for_user(manager, uid, 5)
            audit_log("command_restart_recent", user_id=uid, chat_id=cid, count=len(finished))
            if not finished:
                await q.message.reply_text("No recent finished jobs to restart.")
                return
            for j in finished:
                if len(manager.list_active()) >= manager.max_concurrent:
                    await q.message.reply_text("Capacity reached ({} jobs); stopped restarting the rest.".format(
                        manager.max_concurrent))
                    break
                await _start_job(update, context, _args_for_restart_shlex(j.args))
            return
        # --- History (archive) callbacks ---
        if data.startswith("h:list:"):
            await ack()
            offset = int(data[7:]) if data[7:].isdigit() else 0
            page_size = 5
            entries = job_store.load_archive_for_user(uid, offset=offset, limit=page_size)
            total = job_store.archive_count_for_user(uid)
            if not entries:
                await q.message.reply_text("No job history yet. Finished jobs will appear here.")
                return
            page_num = (offset // page_size) + 1
            lines = ["📂 Job History (page {}, {} total):".format(page_num, total), ""]
            for e in entries:
                status_icon = {"success": "✅", "done": "✅", "failed": "❌",
                               "cancelled": "🛑", "error": "⚠️"}.get(e.get("status"), "•")
                ended = (e.get("ended_at") or "")[:10]
                lines.append("{} {} — {} {} ({})".format(
                    status_icon, e.get("job_id", "?")[:8],
                    e.get("park_name") or "Unknown",
                    e.get("stay_label") or "",
                    ended))
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            rows = []
            for e in entries:
                jid = e.get("job_id", "")[:8]
                rows.append([
                    InlineKeyboardButton("🔁 Re-run {}".format(jid), callback_data="h:r:{}".format(jid)),
                    InlineKeyboardButton("✏️ Edit {}".format(jid), callback_data="h:e:{}".format(jid)),
                ])
            nav = []
            if offset > 0:
                nav.append(InlineKeyboardButton("⏪ Prev", callback_data="h:list:{}".format(max(0, offset - page_size))))
            if offset + page_size < total:
                nav.append(InlineKeyboardButton("⏩ Next", callback_data="h:list:{}".format(offset + page_size)))
            if nav:
                rows.append(nav)
            rows.append([InlineKeyboardButton("🔙 Menu", callback_data="j:l")])
            await q.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))
            return
        if data.startswith("h:r:"):
            await ack()
            jid = data[4:].strip()
            rec = job_store.archive_get_by_id(jid)
            if not rec or rec.get("user_id") != uid:
                await q.message.reply_text("Archived job not found.")
                return
            raw = monitor_state_to_shlex_raw(rec["state"])
            await _start_job(update, context, raw)
            return
        if data.startswith("h:e:"):
            await ack()
            jid = data[4:].strip()
            rec = job_store.archive_get_by_id(jid)
            if not rec or rec.get("user_id") != uid:
                await q.message.reply_text("Archived job not found.")
                return
            state = dict(rec["state"])
            context.user_data[UD_MONITOR] = state
            msg = "Edit archived job {}.\n\n{}".format(jid, format_monitor_command_preview(state))
            await q.message.reply_text(msg, reply_markup=_monitor_more_menu_keyboard(state))
            return
        if data.startswith("j:d:"):
            await ack()
            jid = data[4:].strip()
            job = manager.get_for_user(jid, uid)
            if not job:
                await q.message.reply_text("Job {} not found or not yours.".format(jid))
                return
            await _edit_or_reply(_format_status_text(job), _job_detail_keyboard(job, manager))
            return
        if data.startswith("j:x:"):
            await ack()
            jid = data[4:].strip()
            job = manager.get_for_user(jid, uid)
            if not job:
                await q.message.reply_text("Job not found or not yours.")
                return
            cmd = monitor_args_to_command(job.args)
            note = ""
            if job.args.sms:
                note = "\n(SMS uses server env credentials.)"
            await q.message.reply_text("Copy to re-run:\n\n`{}`{}".format(cmd, note), parse_mode="Markdown")
            return
        if data.startswith("j:e:"):
            await ack()
            jid = data[4:].strip()
            job = manager.get_for_user(jid, uid)
            if not job:
                await q.message.reply_text("Job not found or not yours.")
                return
            state = _args_to_monitor_state(job.args)
            if manager.is_active(jid):
                state["_replace_job_id"] = jid
            context.user_data[UD_MONITOR] = state
            msg = "Edit job {}.\nRun replaces the active job if still running.\n\n{}".format(
                jid, format_monitor_command_preview(state))
            await q.message.reply_text(msg, reply_markup=_monitor_more_menu_keyboard(state))
            return
        if data.startswith("j:r:"):
            await ack()
            jid = data[4:].strip()
            job = manager.get_for_user(jid, uid)
            if not job:
                await q.message.reply_text("Job not found or not yours.")
                return
            raw = _args_for_restart_shlex(job.args)
            await _start_job(update, context, raw)
            return
        if data.startswith("j:s:"):
            await ack()
            jid = data[4:].strip()
            audit_log("command_status", user_id=uid, chat_id=cid, job_id=jid,
                      found=manager.get_for_user(jid, uid) is not None)
            await q.message.reply_text(_status_for_user(jid, uid))
            return
        if data == "j:l":
            await ack(); await jobs_cmd(update, context); return
        if data == "r:x":
            await ack(); _clear_user_flow(context); _drop_wizard_draft(uid)
            await q.message.reply_text("Wizard cancelled."); return
        if data == "r:dd":
            await ack()
            _clear_user_flow(context)
            _drop_wizard_draft(uid)
            context.user_data[UD_PENDING] = "r_url"
            await q.message.reply_text(
                "Draft discarded. Send a new park booking results URL, or type a full /monitor … command.")
            return
        if data in ("r:g", "r:e"):
            await ack()
            if not rb:
                await q.message.reply_text("No URL in progress. Tap 📡 Monitor.")
                return
            if rb.get("sms"):
                env = _twilio_from_env()
                missing = [k for k in _TWILIO_ENV if not ((rb.get(k) or "").strip() or env.get(k))]
                if missing:
                    nice = {"twilio_sid": "Twilio SID", "twilio_auth_token": "Auth Token",
                            "twilio_number": "Twilio Number", "my_phone_number": "Your Phone"}
                    await q.message.reply_text(
                        "📱 SMS is ON but missing: {}.\nAdd them in More → SMS, set them in server env, or turn SMS off.".format(
                            ", ".join(nice.get(m, m) for m in missing)),
                        reply_markup=_monitor_more_menu_keyboard(rb))
                    return
            replace_id = rb.pop("_replace_job_id", None)
            raw = monitor_state_to_shlex_raw(rb)
            _clear_user_flow(context)
            _drop_wizard_draft(uid)
            if replace_id and manager.cancel_for_user(replace_id, uid):
                _bot_console_line("Replacing job {} user={}".format(replace_id, uid))
                await q.message.reply_text("Replacing job {}…".format(replace_id))
            await _start_job(update, context, raw)
            return
        if data == "r:m":
            await ack()
            if not rb:
                await q.message.reply_text("No URL in progress.")
                return
            await _edit_or_reply("{}\n\nPick an option:".format(format_monitor_command_preview(rb)),
                                _monitor_more_menu_keyboard(rb))
            return
        if data == "r:b":
            await ack()
            if not rb:
                await q.message.reply_text("Wizard expired.")
                return
            await _edit_or_reply("{}\n\nGo or More:".format(format_monitor_command_preview(rb)),
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
            await _edit_or_reply("Reset options.\n\n{}".format(format_monitor_command_preview(rb)),
                                _monitor_more_menu_keyboard(rb))
            return
        if data == "r:o:f":
            await ack()
            if not rb:
                await q.message.reply_text("Start the wizard from 📡 Monitor.")
                return
            context.user_data[UD_PENDING] = "r_f"
            _persist_wizard(context, uid)
            await q.message.reply_text("Send preferred site labels comma-separated (e.g. S51,S52), or clear to remove.")
            return
        if data == "r:o:i":
            await ack()
            if not rb: return
            context.user_data[UD_PENDING] = "r_i"
            _persist_wizard(context, uid)
            await q.message.reply_text("Send poll interval in seconds (e.g. 60). Minimum 5.")
            return
        if data == "r:o:j":
            await ack()
            if not rb: return
            context.user_data[UD_PENDING] = "r_j"
            _persist_wizard(context, uid)
            await q.message.reply_text("Send jitter in seconds (e.g. 10).")
            return
        if data == "r:o:rv":
            await ack()
            if not rb: return
            rb["reserve"] = not rb.get("reserve", False)
            _persist_wizard(context, uid)
            await _edit_or_reply("{}\n\nPick an option:".format(format_monitor_command_preview(rb)),
                                _monitor_more_menu_keyboard(rb))
            return
        if data == "r:o:w":
            await ack()
            if not rb: return
            rb["warmode"] = not rb.get("warmode")
            if not rb["warmode"]:
                rb["warmode_click_delay"] = 0
            _persist_wizard(context, uid)
            await _edit_or_reply("{}\n\nPick an option:".format(format_monitor_command_preview(rb)),
                                _monitor_more_menu_keyboard(rb))
            return
        if data == "r:o:wcd":
            await ack()
            if not rb or not rb.get("warmode"):
                await q.message.reply_text("Turn on Warmode first.")
                return
            context.user_data[UD_PENDING] = "r_wcd"
            _persist_wizard(context, uid)
            await q.message.reply_text(
                "Send warmode click delay in milliseconds after 07:00 open (e.g. 300). Range 0–120000, or abort.")
            return
        if data == "r:o:tz":
            await ack()
            if not rb or not rb.get("warmode"):
                await q.message.reply_text("Turn on Warmode first.")
                return
            context.user_data[UD_PENDING] = "r_tz"
            _persist_wizard(context, uid)
            await q.message.reply_text(
                "Send the warmode timezone as an IANA name (e.g. America/Toronto, US/Pacific), or abort.")
            return
        if data == "r:o:d":
            await ack()
            if not rb: return
            rb["debug"] = not rb.get("debug")
            _persist_wizard(context, uid)
            await _edit_or_reply("{}\n\nPick an option:".format(format_monitor_command_preview(rb)),
                                _monitor_more_menu_keyboard(rb))
            return
        if data == "r:o:l":
            await ack()
            if not rb: return
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
                _persist_wizard(context, uid)
            await _edit_or_reply("{}\n\nPick an option:".format(format_monitor_command_preview(rb or {})),
                                _monitor_more_menu_keyboard(rb or {}))
            return
        if data == "r:l:o":
            await ack()
            if rb:
                rb["loop"] = "once"
                _persist_wizard(context, uid)
            await _edit_or_reply("{}\n\nPick an option:".format(format_monitor_command_preview(rb or {})),
                                _monitor_more_menu_keyboard(rb or {}))
            return
        if data == "r:l:bk":
            await ack()
            if rb:
                await _edit_or_reply("{}\n\nPick an option:".format(format_monitor_command_preview(rb)),
                                    _monitor_more_menu_keyboard(rb))
            return
        if data == "r:o:sms":
            await ack()
            if not rb: return
            await _edit_or_reply("SMS / Twilio settings:", _sms_submenu_keyboard(rb))
            return
        if data == "r:s:tog":
            await ack()
            if rb:
                rb["sms"] = not rb.get("sms", False)
                _persist_wizard(context, uid)
            await _edit_or_reply("SMS / Twilio settings:", _sms_submenu_keyboard(rb or {}))
            return
        if data == "r:s:sid":
            await ack()
            if not rb: return
            context.user_data[UD_PENDING] = "r_tsid"
            await q.message.reply_text("Send your Twilio Account SID:")
            return
        if data == "r:s:at":
            await ack()
            if not rb: return
            context.user_data[UD_PENDING] = "r_tat"
            await q.message.reply_text("Send your Twilio Auth Token:")
            return
        if data == "r:s:tn":
            await ack()
            if not rb: return
            context.user_data[UD_PENDING] = "r_tn"
            await q.message.reply_text("Send your Twilio phone number (e.g. +1234567890):")
            return
        if data == "r:s:mpn":
            await ack()
            if not rb: return
            context.user_data[UD_PENDING] = "r_mpn"
            await q.message.reply_text("Send your phone number to receive SMS (e.g. +1234567890):")
            return
        if data == "r:s:bk":
            await ack()
            if rb:
                await _edit_or_reply("{}\n\nPick an option:".format(format_monitor_command_preview(rb)),
                                    _monitor_more_menu_keyboard(rb))
            return
        await ack()

    async def _post_init(application):
        try:
            from telegram import BotCommand
            await application.bot.set_my_commands([
                BotCommand("menu", "Open the campslinger menu (jobs + actions)"),
            ])
        except Exception as e:
            _bot_console_line("Could not set bot commands: {!r}".format(e))
        # --- Restore persisted jobs ---
        stored = job_store.load_store()
        if stored:
            ev_loop = asyncio.get_running_loop()
            restored_by_chat = {}
            for rec in stored:
                job = _start_job_from_record(rec, manager, application.bot, ev_loop)
                if job:
                    restored_by_chat.setdefault(job.chat_id, []).append(job)
            if restored_by_chat:
                _sync_active_store(manager)
                total = sum(len(v) for v in restored_by_chat.values())
                audit_log("jobs_restored_on_start", count=total,
                          job_ids=",".join(j.job_id for jobs in restored_by_chat.values() for j in jobs))
                for chat_id, jobs in restored_by_chat.items():
                    lines = ["Restored {} job(s) after restart:".format(len(jobs))]
                    for j in jobs:
                        lines.append("• {} {} {}".format(j.job_id, j.stay_label or "?", _job_filter_display(j)))
                    try:
                        await application.bot.send_message(chat_id=chat_id, text="\n".join(lines))
                    except Exception:
                        pass
                _bot_console_line("Restored {} job(s) from store".format(total))

    app.post_init = _post_init

    app.add_error_handler(telegram_error_handler)
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("monitor", monitor_cmd))
    app.add_handler(CommandHandler("jobs", jobs_cmd))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("cancelall", cancelall_cmd))
    app.add_handler(CommandHandler("exportall", exportall_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    audit_log("bot_start", max_concurrent=manager.max_concurrent, terminal_log=args.terminal_log,
              remote_chrome="{}:{}".format(_REMOTE_CHROME[0], _REMOTE_CHROME[1]) if _REMOTE_CHROME else None,
              audit_log_path=(os.getenv("CAMPSLINGER_AUDIT_LOG") or "campslinger_telegram_audit.log").strip())
    if _REMOTE_CHROME and manager.max_concurrent > 1:
        _bot_console_line(
            "⚠️  Remote Chrome is shared but --max-concurrent={}; reserve jobs are serialized. "
            "Run with --max-concurrent 1 to avoid surprises.".format(manager.max_concurrent))
    import signal as _signal

    def _sigterm_handler(signum, frame):
        _bot_console_line("SIGTERM received — syncing store and shutting down…")
        _sync_active_store(manager)
        for job in manager.list_active():
            job.stop_event.set()

    _signal.signal(_signal.SIGTERM, _sigterm_handler)

    drop_pending = bool(getattr(args, "drop_pending_updates", True))
    _bot_console_line("Telegram bot started (long polling). max_concurrent={} terminal_log={} drop_pending={}{}{}".format(
        manager.max_concurrent, args.terminal_log, drop_pending,
        " remote_chrome={}:{}".format(_REMOTE_CHROME[0], _REMOTE_CHROME[1]) if _REMOTE_CHROME else "",
        " job_persist=on" if job_store.persist_enabled() else ""))
    app.run_polling(drop_pending_updates=drop_pending)


def main():
    run_telegram_bot(build_telegram_arg_parser().parse_args())


if __name__ == "__main__":
    main()
