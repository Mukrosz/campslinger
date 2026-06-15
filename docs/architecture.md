# Architecture

Internal layout and runtime behaviour of campslinger. For user-facing documentation see the [README](../README.md).

## Package layout

```text
campslinger/
├── campslinger.py            # CLI entrypoint (monitor + optional reserve)
├── campslinger_tg.py         # Telegram bot entrypoint
├── campslinger/              # shared library (the source of truth for behaviour)
│   ├── __init__.py           # __version__
│   ├── core.py               # park API: fetch_sites_map, fetch_park_name, label helpers
│   ├── log.py                # unified terminal + Telegram logging with digest dedup
│   ├── reserve_modes.py      # reserve_normal_mode, reserve_war_mode
│   ├── selenium_ops.py       # webdriver setup, map navigation, click flows
│   ├── util.py               # URL validation, screenshot naming, SMS, helpers
│   └── wizard_draft.py       # optional on-disk persistence of in-progress wizards
├── docs/                     # this folder
├── _archive/                 # legacy scripts kept for reference
├── .env.example              # operator environment template
├── CHANGELOG.md
├── requirements.txt
└── README.md
```

## Module responsibilities

| Module | Responsibility |
|---|---|
| `core.py` | Stateless park-API client. Pulls site availability JSON and resolves the park's display name. The API base is derived from the booking URL host (`api_base_from_url`). |
| `log.py` | `pp()` is the single log entry point. Builds `[job_id | Park | dates | sites]` context prefixes via `set_job_log_context()` (job id included so concurrent jobs are unambiguous; `current_job_id()` exposes it). Auto-suppresses script timestamps under systemd journald (`JOURNAL_STREAM`); override with `--log-timestamp` / `--no-log-timestamp`. In bot mode, `set_log_callback()` mirrors lines to Telegram with digest-based deduplication. Telegram mirror failures are logged to stderr, not swallowed. |
| `reserve_modes.py` | High-level reservation strategies returning a `(site, reason)` tuple: `reserve_normal_mode` polls until a match is available then drives Selenium; `reserve_war_mode` prefetches the map at 06:59 and clicks Reserve at 07:00 (+ optional click-delay) in the requested timezone, emitting periodic countdowns. Missing `pytz` **raises** (it no longer `sys.exit`s the process). API errors log the exception class name. |
| `selenium_ops.py` | All WebDriver interaction: `setup_webdriver`, `setup_webdriver_remote`, `prepare_reservation`, `get_available_sites`, `_dump_map_load_failure` (failure dumps include the job id). |
| `util.py` | URL allowlist + validation, `stay_window_label()` for human-friendly date ranges, descriptive screenshot path builder (now job-id aware), `availability_digest()` for notification dedup, comma list / sort helpers, SMS helper. |
| `wizard_draft.py` | Opt-in (`CAMPSLINGER_WIZARD_PERSIST=1`) save/load/delete of an in-progress monitor wizard per user, so a half-built configuration survives a bot restart. Twilio secrets are stripped before writing; drafts expire after 7 days. |

## Shared library, thin entrypoints

```mermaid
flowchart LR
    cli[campslinger.py] --> pkg[campslinger/]
    tg[campslinger_tg.py] --> pkg
    pkg --> api[Park API]
    pkg -.->|optional| browser[Selenium Chrome]
    tg -.->|long polling| telegram[Telegram BotAPI]
    cli -.->|optional| twilio[Twilio]
    tg -.->|optional| twilio
```

Both entrypoints import the same package; behaviour parity is by construction.

## Telegram bot job lifecycle

```mermaid
stateDiagram-v2
    [*] --> queued: /monitor parsed and authorized
    queued --> running: thread.start()
    running --> done: monitor hit + loop=once
    running --> success: reserve clicked successfully
    running --> failed: reserve attempt without success
    running --> cancelled: stop_event set by /cancel
    running --> error: unexpected exception
    done --> [*]
    success --> [*]
    failed --> [*]
    cancelled --> [*]
    error --> [*]
```

### JobManager invariants

- `JobManager.create()` returns `None` if `len(active) >= max_concurrent` — the caller responds with "Server busy".
- Each job carries a `threading.Event` (`stop_event`). `JobManager.cancel_for_user()` sets it; `cancel_all_for_user()` cancels every active job for a user. The worker thread polls `stop_event` during sleeps and inside `reserve_*_mode` between Selenium steps.
- All access to `active` and `recent` is under `self._lock`. The deque caps `recent` at `recent_max=40`.
- Per-user filtering (`*_for_user`) is the gate that keeps users from seeing each other's jobs.

### JobState display fields

| Field | Source | Example |
|---|---|---|
| `stay_label` | `stay_window_label(url)` (cheap, at queue time) | `jun15-jun20` |
| `site_filter` | `--f` argument (at queue time) | `s51,s52` or `None` (shown as `all`) |
| `park_name` | `fetch_park_name(url)` (resolved by the worker thread on start) | `Kikomun Creek Provincial Park` |

`stay_label` and `site_filter` are filled synchronously so the queue acknowledgement is instant; `park_name` is resolved by the worker (a network call) and then appears in `/status`, menu listings, and the log prefix.

## Telegram commands (menu-first UX)

`/menu` (and `/start`) is the single hub: `_jobs_overview_text()` + `_jobs_overview_keyboard()` list active **and** recent jobs as buttons. The bot registers only `/menu` via `set_my_commands()` on `post_init`; other commands still work and are reachable from buttons. `/help` is a concise reference (`telegram_help_text()`), no longer a dashboard.

| Command / callback | Handler | Purpose |
|---|---|---|
| `/menu`, `/start`, `/jobs` | `menu_cmd`, `start_cmd`, `jobs_cmd` | Active + recent overview with buttons |
| `/help`, `m:h` | `help_cmd` | Concise command reference |
| `/cancelall`, `j:ca` | `cancelall_cmd` | `cancel_all_for_user()` |
| `/exportall`, `j:xa` | `exportall_cmd` | `monitor_args_to_command()` per active job |
| `j:xr` / `j:rr` | export / restart recent | Recent finished jobs (last 5), respecting capacity |
| `j:d:<id>` | detail view | Status text + `_job_detail_keyboard()` |
| `j:x:<id>` | export one job | Single `/monitor …` line, no Twilio secrets |
| `j:e:<id>` | edit | Prefill wizard; Run cancels original if still active |
| `j:r:<id>` | restart | Re-queue from stored args via `_args_for_restart_shlex()` |

A finished job posts an inline action card (`_job_end_keyboard`: Restart / Export / Menu). The wizard's `r:dd` discards a resumed draft.

## Notification throttling

`availability_digest(labels)` produces a stable frozenset of matched site labels. Both entrypoints track the last hit; in continuous mode a Telegram availability ping (`telegram_digest=("hit", digest)`) and any **paid SMS** are emitted only when the digest changes, re-arming when availability drops to none. `--loop once` always notifies once and stops.

## Wizard draft persistence

When `CAMPSLINGER_WIZARD_PERSIST=1`, `_persist_wizard()` saves the in-progress wizard (via `wizard_draft.save_draft`) at each meaningful step and pending-input request; `_drop_wizard_draft()` clears it on Run / cancel / discard. Tapping 📡 Monitor calls `wizard_draft.load_draft()` and offers Go / More / 🆕 New URL. Secrets are stripped before writing and drafts expire after 7 days (`CAMPSLINGER_WIZARD_DRAFT_DIR` overrides the location).

## Twilio env defaults

When all four `CAMPSLINGER_TWILIO_*` env vars are set, `_apply_twilio_env_to_args()` fills empty fields on job start. The wizard SMS submenu shows `[env]` for env-supplied fields. Export/restart paths never embed secrets in Telegram messages — they emit `--sms` only and rely on env at runtime.

## Logging under systemd

```mermaid
flowchart LR
    job[Job thread] -->|set_job_log_context| ctx["threading.local: job_id, park, stay, filter"]
    ctx --> pp["pp() -> _format_log_line()"]
    pp -->|JOURNAL_STREAM unset| shell["stdout with script timestamp"]
    pp -->|JOURNAL_STREAM set| journald["stdout without script timestamp"]
    journald --> journalctl["journalctl adds system timestamp"]
```

Configure via `configure_log_timestamps()` at process start. CLI and bot both accept `--log-timestamp` / `--no-log-timestamp`.

## Logging digest deduplication

`pp(message, telegram_digest=...)` accepts an optional tuple identifying the *kind* of log line. Examples used in the code:

| Digest tuple | Meaning |
|---|---|
| `("zero",)` | "no availability" — same shape every poll. |
| `("filter_wait", frozenset(all_avail), tuple(args.filter or ()))` | "available, but none of your preferred sites" — collapses while the available set is unchanged. |
| `("api_err", type_name, err_snippet)` | API exception — collapses bursts of identical errors. Includes exception class name, and gets the cancel footer. |
| `("hit", availability_digest)` | Availability ping — collapses while the matched set is unchanged (continuous mode). |
| `("wm_countdown", what, bucket)` | Warmode countdown reassurance during long waits. |
| `None` | Always send to Telegram. |

If the new digest equals the previously recorded one (per-thread), the Telegram callback is skipped. Terminal output is always printed.

## Booking URL hardening

`util.validate_booking_url()` enforces, in order:

1. `scheme == "https"` (not `http`).
2. `hostname in SUPPORTED_PARK_HOSTS` — SSRF defence; the bot will only fetch known park hosts.
3. `path` starts with `/create-booking/`.
4. No embedded credentials (`user:pass@host` rejected).
5. Default port (`443`).

Adding a new park is a one-line change to `SUPPORTED_PARK_HOSTS`.

## Warmode timing

`reserve_war_mode` computes the next 07:00 boundary in `--timezone` (default `US/Pacific`, configurable per job via the CLI flag, the `/monitor --timezone`/`--tz` flag, or the 🌐 TZ wizard button) using `pytz` and `datetime`. It:

1. Sleeps until 06:59 (with `stop_event.wait()` so cancellations are responsive), logging periodic countdowns.
2. Loads the map and collects available icons (the prefetch step).
3. Sleeps until the 07:00 boundary.
4. Sleeps an additional `warmode_click_delay_ms` (default 0).
5. Clicks Reserve.

Prefetch/open logs name the actual timezone. Missing `pytz` raises `ImportError` (caught by the CLI/worker and reported) rather than terminating the process. Local TZ on the host is irrelevant — only clock accuracy matters. NTP is recommended.

## Remote Chrome concurrency

With `--rip`/`--rp`, all reserve jobs share one Chrome session. The worker (`_run_reserve_job`) serializes reserve work behind `_remote_chrome_lock` and never calls `driver.quit()` on the shared session (which would disrupt other jobs). The bot warns at startup if a shared remote Chrome is combined with `--max-concurrent > 1`.

## Adding a new park platform

1. Append the hostname to `SUPPORTED_PARK_HOSTS` in [campslinger/util.py](../campslinger/util.py).
2. Update the table in [README.md](../README.md) → "Supported parks".
3. Smoke test with a real booking URL: `python3 -c "from campslinger.util import validate_booking_url; print(validate_booking_url('https://newhost.example/create-booking/results?...'))"`.
4. Run a monitor-only job to confirm the API answers (the JSON shape is shared across all Aspira / GoingToCamp parks).
