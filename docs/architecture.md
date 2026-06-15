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
│   └── util.py               # URL validation, screenshot naming, SMS, helpers
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
| `log.py` | `pp()` is the single log entry point. Builds `[Park | dates | sites]` context prefixes via `set_job_log_context()`. Auto-suppresses script timestamps under systemd journald (`JOURNAL_STREAM`); override with `--log-timestamp` / `--no-log-timestamp`. In bot mode, `set_log_callback()` mirrors lines to Telegram with digest-based deduplication. |
| `reserve_modes.py` | High-level reservation strategies: `reserve_normal_mode` polls until a match is available then drives Selenium; `reserve_war_mode` prefetches the map at 06:59 and clicks Reserve at 07:00 (+ optional click-delay). API errors log the exception class name. |
| `selenium_ops.py` | All WebDriver interaction: `setup_webdriver`, `setup_webdriver_remote`, `prepare_reservation`, `get_available_sites`, `_dump_map_load_failure`. |
| `util.py` | URL allowlist + validation, `stay_window_label()` for human-friendly date ranges, descriptive screenshot path builder, comma list / sort helpers, SMS helper. |

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

At queue time each job resolves and caches:

| Field | Source | Example |
|---|---|---|
| `park_name` | `fetch_park_name(url)` | `Kikomun Creek Provincial Park` |
| `stay_label` | `stay_window_label(url)` | `jun15-jun20` |
| `site_filter` | `--f` argument | `s51,s52` or `None` (shown as `all`) |

These appear in `/jobs`, `/status`, `/help` dashboard listings, and the terminal log prefix.

## Telegram commands (multi-job UX)

| Command / callback | Handler | Purpose |
|---|---|---|
| `/help` | `help_cmd` | Help text + live running-jobs dashboard with buttons |
| `/jobs`, `/menu` | `jobs_cmd`, `menu_cmd` | Same overview via `_jobs_overview_text()` |
| `/cancelall`, `j:ca` | `cancelall_cmd` | `cancel_all_for_user()` |
| `/exportall`, `j:xa` | `exportall_cmd` | `monitor_args_to_command()` per active job |
| `j:d:<id>` | detail view | Status text + `_job_detail_keyboard()` |
| `j:x:<id>` | export one job | Single `/monitor …` line, no Twilio secrets |
| `j:e:<id>` | edit | Prefill wizard; Run cancels original if still active |
| `j:r:<id>` | restart | Re-queue from stored args via `_args_for_restart_shlex()` |

## Twilio env defaults

When all four `CAMPSLINGER_TWILIO_*` env vars are set, `_apply_twilio_env_to_args()` fills empty fields on job start. The wizard SMS submenu shows `[env]` for env-supplied fields. Export/restart paths never embed secrets in Telegram messages — they emit `--sms` only and rely on env at runtime.

## Logging under systemd

```mermaid
flowchart LR
    job[Job thread] -->|set_job_log_context| ctx["threading.local: park, stay, filter"]
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
| `("api_err", type_name, err_snippet)` | API exception — collapses bursts of identical errors. Includes exception class name. |
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

`reserve_war_mode` computes the next 07:00 boundary in `--timezone` (default `US/Pacific`) using `pytz` and `datetime`. It:

1. Sleeps until 06:59 (with `stop_event.wait()` so cancellations are responsive).
2. Loads the map and collects available icons (the prefetch step).
3. Sleeps until the 07:00 boundary.
4. Sleeps an additional `warmode_click_delay_ms` (default 0).
5. Clicks Reserve.

Local TZ on the host is irrelevant — only clock accuracy matters. NTP is recommended.

## Adding a new park platform

1. Append the hostname to `SUPPORTED_PARK_HOSTS` in [campslinger/util.py](../campslinger/util.py).
2. Update the table in [README.md](../README.md) → "Supported parks".
3. Smoke test with a real booking URL: `python3 -c "from campslinger.util import validate_booking_url; print(validate_booking_url('https://newhost.example/create-booking/results?...'))"`.
4. Run a monitor-only job to confirm the API answers (the JSON shape is shared across all Aspira / GoingToCamp parks).
