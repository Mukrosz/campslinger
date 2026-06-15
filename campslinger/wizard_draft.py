"""Optional on-disk persistence for in-progress monitor wizard drafts.

Opt-in via CAMPSLINGER_WIZARD_PERSIST=1.  Lets a user resume a half-built
/monitor configuration after a bot restart.  Running jobs are NOT persisted
here (use /exportall for those); this only saves the wizard the user was
still editing.  Twilio secrets are never written to disk.
"""

import json
import os
import time

_PERSIST_ENV = "CAMPSLINGER_WIZARD_PERSIST"
_DIR_ENV = "CAMPSLINGER_WIZARD_DRAFT_DIR"
_DEFAULT_DIR = "campslinger_wizard_drafts"
_TTL_SECONDS = 7 * 24 * 3600
_SECRET_FIELDS = ("twilio_sid", "twilio_auth_token", "twilio_number", "my_phone_number")
_TRUTHY = ("1", "true", "yes", "on")


def persist_enabled():
    return (os.getenv(_PERSIST_ENV) or "").strip().lower() in _TRUTHY


def _draft_dir():
    return (os.getenv(_DIR_ENV) or "").strip() or _DEFAULT_DIR


def _draft_path(user_id):
    return os.path.join(_draft_dir(), "{}.json".format(int(user_id)))


def save_draft(user_id, state, pending=None):
    if not persist_enabled() or not state or user_id is None:
        return
    safe = {k: v for k, v in state.items() if k not in _SECRET_FIELDS}
    # A restarted bot can't replace a job whose thread is gone.
    safe.pop("_replace_job_id", None)
    rec = {"ts": time.time(), "pending": pending, "state": safe}
    try:
        os.makedirs(_draft_dir(), exist_ok=True)
        path = _draft_path(user_id)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f)
        os.replace(tmp, path)
    except (OSError, ValueError):
        pass


def load_draft(user_id):
    """Return (state, pending) or None.  Expired/invalid drafts are removed."""
    if not persist_enabled() or user_id is None:
        return None
    try:
        with open(_draft_path(user_id), "r", encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict) or time.time() - rec.get("ts", 0) > _TTL_SECONDS:
        delete_draft(user_id)
        return None
    state = rec.get("state")
    if not isinstance(state, dict) or not state.get("url"):
        return None
    return state, rec.get("pending")


def delete_draft(user_id):
    if user_id is None:
        return
    try:
        os.remove(_draft_path(user_id))
    except OSError:
        pass
