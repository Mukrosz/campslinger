"""Persistent store for active jobs (survives reboot) and finished-job archive.

Active jobs: single JSON file, atomically rewritten on every change.
  Opt-in via CAMPSLINGER_JOB_PERSIST=1.

Archive: JSONL file (one JSON object per line), append-only.
  Opt-in via CAMPSLINGER_JOB_HISTORY=1.

The two features are independent — enable either or both.
Twilio secrets are never written to disk by either.
"""

import json
import os

_PERSIST_ENV = "CAMPSLINGER_JOB_PERSIST"
_HISTORY_ENV = "CAMPSLINGER_JOB_HISTORY"
_STORE_PATH_ENV = "CAMPSLINGER_JOB_STORE_PATH"
_ARCHIVE_PATH_ENV = "CAMPSLINGER_JOB_ARCHIVE_PATH"
_DEFAULT_STORE = "campslinger_active_jobs.json"
_DEFAULT_ARCHIVE = "campslinger_job_archive.jsonl"
_TRUTHY = ("1", "true", "yes", "on")
_SECRET_FIELDS = ("twilio_sid", "twilio_auth_token", "twilio_number", "my_phone_number")
_STORE_VERSION = 1


def persist_enabled():
    return (os.getenv(_PERSIST_ENV) or "").strip().lower() in _TRUTHY


def history_enabled():
    return (os.getenv(_HISTORY_ENV) or "").strip().lower() in _TRUTHY


def _store_path():
    return (os.getenv(_STORE_PATH_ENV) or "").strip() or _DEFAULT_STORE


def _archive_path():
    return (os.getenv(_ARCHIVE_PATH_ENV) or "").strip() or _DEFAULT_ARCHIVE


def _strip_secrets(state):
    return {k: v for k, v in state.items() if k not in _SECRET_FIELDS}


def _iso_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# Active store (full rewrite)
# ---------------------------------------------------------------------------

def sync_store(active_records):
    """Atomically rewrite the active-jobs file.

    active_records: list of dicts with keys job_id, user_id, chat_id,
    queued_at, state (already secret-stripped).
    """
    if not persist_enabled():
        return
    payload = {"version": _STORE_VERSION, "written_at": _iso_now(), "jobs": active_records}
    path = _store_path()
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"))
            f.write("\n")
        os.replace(tmp, path)
    except OSError as e:
        import logging
        logging.getLogger("campslinger.job_store").warning(
            "Failed to write active store %s: %s", path, e)


def load_store():
    """Read persisted active jobs on startup. Returns list of dicts or empty."""
    if not persist_enabled():
        return []
    path = _store_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        if isinstance(e, FileNotFoundError):
            return []
        import logging
        logging.getLogger("campslinger.job_store").warning(
            "Failed to read active store %s: %s", path, e)
        return []
    if not isinstance(data, dict) or data.get("version") != _STORE_VERSION:
        import logging
        logging.getLogger("campslinger.job_store").warning(
            "Active store version mismatch or bad format: %s", path)
        return []
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        return []
    valid = []
    for rec in jobs:
        if (isinstance(rec, dict) and rec.get("state")
                and isinstance(rec["state"], dict) and rec["state"].get("url")):
            valid.append(rec)
    return valid


def clear_store():
    """Write an empty active store (clean shutdown)."""
    if not persist_enabled():
        return
    path = _store_path()
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": _STORE_VERSION, "written_at": _iso_now(), "jobs": []},
                      f, separators=(",", ":"))
            f.write("\n")
        os.replace(tmp, path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Archive (append-only JSONL)
# ---------------------------------------------------------------------------

def archive_job(record):
    """Append a finished-job record (dict) to the archive file."""
    if not history_enabled():
        return
    path = _archive_path()
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")))
            f.write("\n")
    except OSError as e:
        import logging
        logging.getLogger("campslinger.job_store").warning(
            "Failed to append to archive %s: %s", path, e)


def _load_all_archive():
    """Read all archive lines (oldest first in file)."""
    if not history_enabled():
        return []
    path = _archive_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except (OSError, ValueError):
        return []
    records = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if isinstance(rec, dict) and rec.get("state"):
                records.append(rec)
        except (ValueError, TypeError):
            continue
    return records


def load_archive_for_user(user_id, offset=0, limit=5):
    """Return paginated archive entries for user_id, newest first."""
    all_recs = _load_all_archive()
    user_recs = [r for r in all_recs if r.get("user_id") == user_id]
    user_recs.reverse()
    return user_recs[offset:offset + limit]


def archive_count_for_user(user_id):
    """Total archived entries for a user."""
    all_recs = _load_all_archive()
    return sum(1 for r in all_recs if r.get("user_id") == user_id)


def archive_get_by_id(job_id):
    """Find a specific archived job by job_id (for re-run/edit)."""
    all_recs = _load_all_archive()
    for rec in reversed(all_recs):
        if rec.get("job_id") == job_id:
            return rec
    return None
