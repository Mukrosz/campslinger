# Audit log

`campslinger_tg.py` writes one JSON object per line to the audit log. The default path is `./campslinger_telegram_audit.log`; override with the `CAMPSLINGER_AUDIT_LOG` environment variable.

## Goals

- **Operator visibility.** Every authorization decision, command, and job lifecycle event is recorded.
- **Forensic-friendly.** Every line is a complete JSON object; you can filter with `jq` without context.
- **Privacy by construction.** Twilio credentials never reach this file — whether typed in the wizard or loaded from `CAMPSLINGER_TWILIO_*` env vars. Export commands and `/exportall` also omit secrets from Telegram messages.

## Field reference

| Field | When written | Meaning |
|---|---|---|
| `ts` | always | ISO 8601 timestamp (seconds precision). |
| `action` | always | Event kind. See the action catalogue below. |
| `user_id` | when applicable | Numeric Telegram user ID. |
| `chat_id` | when applicable | Numeric Telegram chat ID. |
| `username` | `unauthorized` only | The rejected user's `@username`, if Telegram exposes one. |
| `job_id` | job-related actions | 8-char hex job id. |
| `job_kind` | job-related actions | `monitor` or `reserve`. |
| `url` | job-related actions | Full booking URL (used to filter by park). |
| `park` | `job_queued` | Resolved park display name. |
| `stay` | `job_queued` | Stay window label (e.g. `jun15-jun20`). |
| `reserve` | `job_queued` | Boolean: did the user opt into Reserve? |
| `loop` | `job_queued` | `continuous` or `once`. |
| `warmode` | `job_queued` | Boolean. |
| `interval` | `job_queued` | Poll interval in seconds. |
| `filter` | `job_queued` | Comma-separated preferred site labels (lowercased). |
| `status` | `job_finished` | `done`, `cancelled`, `failed`, `success`, or `error`. |
| `result_site` | `job_finished` (success) | Reserved site label. |
| `error` | `job_finished` (error) / `job_aborted` | Truncated error string. |
| `reason` | `job_aborted` | Short machine-readable reason (`missing_twilio`, `missing_twilio_creds`, `webdriver_init_failed`). |
| `accepted` | `command_cancel` | Boolean: did the cancellation succeed? |
| `found` | `command_status` | Boolean: did the requested job belong to the requester? |
| `count` | bulk commands | Number of jobs affected. |
| `job_ids` | `command_cancelall` | Comma-separated ids that received a cancel signal. |
| `max_concurrent` | `bot_start`, `job_rejected_busy` | Configured concurrency cap. |
| `terminal_log` | `bot_start` | Whether `--no-terminal-log` was passed. |
| `remote_chrome` | `bot_start` | `host:port` of the operator's remote Chrome, or absent. |
| `audit_log_path` | `bot_start` | Resolved path to this very file. |
| `error_type` | `handler_error` | Python exception class name. |

## Action catalogue

| Action | Trigger |
|---|---|
| `bot_start` | Process started; logs configuration. |
| `unauthorized` | Telegram update from a user not in `TELEGRAM_ALLOWED_USER_IDS`. |
| `command_help` | `/help` invoked. |
| `command_monitor_usage` | `/monitor` with no arguments (wizard prompt). |
| `command_jobs` | `/jobs` invoked. |
| `command_menu` | `/menu` invoked (alias for `/jobs`). |
| `command_status` | `/status` invoked or status callback used. |
| `command_cancel` | `/cancel` invoked or cancel callback used. |
| `command_cancelall` | `/cancelall` or **Cancel all** button. |
| `command_exportall` | `/exportall` or **Export all** button. |
| `job_queued` | A new job has been accepted into the JobManager. |
| `job_rejected_busy` | `--max-concurrent` cap hit. |
| `job_parse_error` | Malformed `/monitor …` arguments. |
| `job_aborted` | Job ended before running (missing Twilio module/creds, WebDriver init failure). |
| `job_finished` | Job ended (any terminal status). |
| `text_message` | Free-text message that wasn't a command, URL, or wizard reply. |
| `handler_error` | Telegram framework error raised inside a handler. |

## Sample lines

```json
{"ts":"2026-06-15T02:30:00","action":"bot_start","max_concurrent":3,"terminal_log":true,"audit_log_path":"/var/log/campslinger/audit.log"}
{"ts":"2026-06-15T02:31:00","action":"job_queued","user_id":11111111,"chat_id":11111111,"job_id":"a1b2c3d4","url":"https://camping.bcparks.ca/create-booking/results?…","job_kind":"monitor","reserve":false,"loop":"continuous","warmode":false,"interval":60,"filter":"s51,s52","park":"Kikomun Creek Provincial Park","stay":"jun15-jun20"}
{"ts":"2026-06-15T02:45:00","action":"command_cancelall","user_id":11111111,"chat_id":11111111,"count":2,"job_ids":"a1b2c3d4,b5c6d7e8"}
{"ts":"2026-06-15T02:46:00","action":"command_exportall","user_id":11111111,"chat_id":11111111,"count":3}
{"ts":"2026-06-15T03:10:00","action":"job_finished","user_id":11111111,"chat_id":11111111,"job_id":"a1b2c3d4","status":"done","url":"https://camping.bcparks.ca/create-booking/results?…","job_kind":"monitor"}
```

## Useful `jq` queries

```bash
# All jobs for a given Telegram user
jq -c 'select(.user_id == 11111111)' audit.log

# Successful reservations only
jq -c 'select(.action == "job_finished" and .status == "success")' audit.log

# Jobs at a specific park (by display name)
jq -c 'select(.action == "job_queued" and .park | test("Kikomun"))' audit.log

# Bulk cancel events
jq -c 'select(.action == "command_cancelall")' audit.log

# Unauthorized attempts in the last 24h
jq -c 'select(.action == "unauthorized")' audit.log | tail -50

# Per-user job counts
jq -r 'select(.action == "job_queued") | .user_id' audit.log | sort | uniq -c | sort -rn
```

## Retention and rotation

- Use `logrotate` for production deployments. Sample drop-in `/etc/logrotate.d/campslinger`:

```text
/var/log/campslinger/audit.log {
    weekly
    rotate 12
    compress
    missingok
    notifempty
    copytruncate
    create 640 campslinger campslinger
}
```

- Set tight permissions (`chmod 640`, dedicated user/group). The file contains booking URLs which embed query parameters that some operators may treat as sensitive.
- The default location (`./campslinger_telegram_audit.log`) is gitignored, but for production prefer an absolute path under `/var/log`.

## What is **not** in the audit log

- Twilio Account SID, auth token, "from" number, "to" number — never written, including when loaded from env.
- The Telegram bot token — never written.
- Exported `/monitor …` command strings from `/exportall` — sent to Telegram chat only, not logged.
- Map screenshots and HTML dumps from `--debug` — saved to disk separately, not into the audit log.
- Free-text bodies of cancellation prompts and other `text_handler` messages — only a 200-character preview is logged for `text_message` events.
