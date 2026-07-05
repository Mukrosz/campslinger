# Troubleshooting

Extended troubleshooting recipes. For the short-form FAQ see the [README → Troubleshooting](../README.md#-troubleshooting--faq) section.

## Warmode

### "Cannot Reserve — these dates cannot be reserved until 07:00"

The script clicked Reserve a few milliseconds before the park's server crossed 07:00 in its own zone. Two-step fix:

1. **Verify the host clock.** Run `timedatectl`. `System clock synchronized: yes` and `NTP service: active` are both required. Local timezone does not matter — `--timezone` controls the warmode target.
2. **Add a small post-open delay.** `--warmode-click-delay 200` is a safe default; bump to `400` or `500` ms for slow VPS hosts. Same option in the Telegram wizard: tap **Warmode** to enable, then **WM delay** to set milliseconds.

> [!TIP]
> Don't go above ~1500 ms. The window where Reserve succeeds is short — too much delay risks losing the site to other reservers.

### Setting the warmode timezone

The target zone is configurable per job: `--timezone`/`--tz` on the CLI and `/monitor`, or the **🌐 TZ** button in the wizard (shown when Warmode is on). Default is `US/Pacific`. Invalid zones are now rejected up front with a friendly message (e.g. *"Unknown timezone …"*) instead of silently falling through. Prefetch/open log lines name the actual zone, and long waits emit periodic countdowns.

### Warmode never fires

- Check the bot/CLI is running with `--reserve` set. Warmode is a sub-mode of reserve; it's a no-op without it.
- Check the timezone string is a valid IANA zone (e.g. `US/Pacific`, `America/Toronto`). Invalid zones are rejected at parse time.
- Check the system clock is correct.

### `pytz` not installed

Warmode requires `pytz`. If it's missing the job/CLI now reports a clear *"missing dependency"* error (it no longer terminates the whole bot process). Fix with `pip install -r requirements.txt`.

## Selenium / Chrome

### `webdriver-manager` fails to download ChromeDriver

Symptoms: `ConnectionError`, `HTTPError 4xx/5xx` from the `webdriver-manager` cache step.

Fixes:

- The host is behind a proxy with no internet egress. Install ChromeDriver manually (see [README → ChromeDriver](../README.md#chromedriver)) and place it on `PATH`. The script will use it without `webdriver-manager`.
- ChromeDriver doesn't yet exist for your Chrome major. Pin Chrome to the previous stable major: `apt install google-chrome-stable=xxx`.
- Air-gapped host: copy a working `chromedriver` binary into `/usr/local/bin/` and restart.

### "session not created: This version of ChromeDriver only supports Chrome version N"

The Chrome and ChromeDriver majors mismatch. Either upgrade ChromeDriver to match Chrome, or downgrade Chrome to match ChromeDriver. With remote attach (`--rip`/`--rp`), the relevant Chrome version is on the **remote** machine, not the script host.

### Map fails to load (timeouts, blank canvas)

1. Re-run with `--debug`. The script saves a paired `.html` + `.png` (`mapfail` tag) when the map never reports site icons. Open the HTML — most often the site shows a "queue" page or maintenance banner.
2. Try `--headed` on a machine with a display. Some sites detect headless chromium and degrade.
3. Try remote Chrome with `--rip`/`--rp`, attaching to a real, logged-in browser.
4. Increase the wait. Slow/cold starts are common; `selenium_ops.collect_available_icons_from_map` retries up to 5 times.

### "Cannot Reserve" modal even outside warmode

The site has been *placed in a hold* (likely yours, from a previous run on the same anonymous session). Holds last ~10–15 minutes. Wait it out, then retry — or use `--rip`/`--rp` so holds attach to your own logged-in browser.

## Telegram bot

### The menu (`/menu`, `/start`)

`/menu` is the single hub: it lists your **active (running) jobs** with inline buttons. Each job line shows:

`job_id · kind · park_name · stay_dates · site_filter · status`

Tap a job for **Status**, **Cancel**, **Export**, or **Edit**. Use **Cancel all** / **Export all** for active jobs. Tap **📂 History** to browse finished jobs from disk — paginated, newest first, with Re-run and Edit (requires `CAMPSLINGER_JOB_HISTORY=1`; otherwise History shows a setup hint). A finished job also posts a Restart / Export / Menu card. The bot advertises only `/menu` in Telegram's `/` list; `/help` is a concise reference and `/jobs` still works.

### Reboot recovery

**Automatic (with `CAMPSLINGER_JOB_PERSIST=1`):**

Jobs are persisted to disk on every start/finish and on SIGTERM. After a reboot, the bot restores them automatically and sends a summary per chat. No manual action needed.

**Stagger (recommended for multiple jobs):** Set `CAMPSLINGER_JOB_RESTORE_STAGGER=30` to add a random delay (0–30 s) between each restored job start. This prevents all jobs from hitting the API simultaneously and risking rate-limits or bans.

- Active jobs file: `CAMPSLINGER_JOB_STORE_PATH` (default `./campslinger_active_jobs.json`)
- Archive file: `CAMPSLINGER_JOB_ARCHIVE_PATH` (default `./campslinger_job_archive.jsonl`)

**Manual (with `/exportall`):**

1. Before shutdown, run `/exportall` in Telegram (or tap **Export all** in `/menu`).
2. Save the code block (one `/monitor …` line per running job).
3. After the bot restarts, paste each line back into the chat.
4. If any job used `--sms`, confirm the four `CAMPSLINGER_TWILIO_*` env vars are loaded in systemd — exported lines contain `--sms` only.

> [!NOTE]
> Twilio secrets are **never** written to disk by either the active store or the archive. On restore, credentials are loaded from `CAMPSLINGER_TWILIO_*` env vars.

### Job history

Finished jobs are archived automatically when `CAMPSLINGER_JOB_HISTORY=1`. Browse via the **📂 History** button in `/menu` — paginated, newest first. Each entry has **Re-run** (start immediately with same config) and **Edit** (load into wizard to tweak before running).

> [!TIP]
> The main `/menu` lists **running jobs only**. All finished jobs (done, cancelled, failed, error) are browsed via **📂 History** when `CAMPSLINGER_JOB_HISTORY=1`.

### Jobs not restored after reboot

1. Confirm `CAMPSLINGER_JOB_PERSIST=1` is in the `.env` loaded by systemd (`EnvironmentFile=`).
2. Check the bot startup log for `job_persist=on`.
3. Verify the store file exists and is writable by the service user (default `./campslinger_active_jobs.json`, or your `CAMPSLINGER_JOB_STORE_PATH`). Write failures are logged to stderr.
4. If the bot was killed without SIGTERM and before the first sync, a job started milliseconds before crash may not be in the file — use `/exportall` before planned maintenance as a backup.

### Jobs hitting API simultaneously after reboot

If you see rate-limit errors or suspect the target site is banning you immediately after a reboot, set `CAMPSLINGER_JOB_RESTORE_STAGGER` to a reasonable number of seconds (e.g. `30`). Each restored job will then wait a random delay in [0, N] seconds before starting. The console log shows "Stagger: waiting X.Xs before next restore" between each job.

### "Unauthorized"

Your numeric Telegram user ID isn't in `TELEGRAM_ALLOWED_USER_IDS`. Use `@userinfobot` to find it. Set the env var (comma-separated, no spaces) and restart the bot.

### "Server busy: max N concurrent jobs reached"

The JobManager cap is full. Either:

- `/cancel <id>` or `/cancelall` to stop running jobs.
- Restart the bot with a higher `--max-concurrent` or set `CAMPSLINGER_MAX_CONCURRENT` in `.env`.

> [!IMPORTANT]
> If the bot is started with `--rip`/`--rp`, every reserve job shares one Chrome session. Run with `--max-concurrent 1` to avoid two jobs fighting over the same browser.

### Telegram bot stops sending messages

Telegram rate-limits bots: roughly 30 messages/second globally and 1 message/second per chat. `python-telegram-bot >= 21` retries with backoff but very chatty configurations (low `--interval` + low `--jitter` + many concurrent jobs + `--debug`) can saturate the limit.

Mitigations:

- Increase `--interval` and `--jitter`.
- Disable `--debug` for routine monitoring.
- Reduce `--max-concurrent`.

### Bot prints `Telegram handler error: …` then continues

These are caught by the global error handler. The full traceback prints to the server terminal *only when terminal logging is enabled* (it isn't with `--no-terminal-log`). The audit log records `handler_error` with truncated error text. Persistent identical errors usually indicate a Telegram BotAPI change or an outdated `python-telegram-bot` install.

### `--no-terminal-log` doesn't suppress the traceback

Fixed in the docs-overhaul release. Earlier versions imported `_TERMINAL_LOG_ENABLED` by name and never observed `set_terminal_log_enabled()`. If you're on an older revision, upgrade to head.

## SMS / Twilio

### SMS preflight blocks Run

If you toggle SMS on in the wizard but the four Twilio fields aren't all available (from the wizard or `CAMPSLINGER_TWILIO_*` env), tapping **Run** is blocked with a message naming the missing fields. Add them in **More → SMS**, set them in server env, or turn SMS off. The CLI fails fast too: `--sms` without all of `--tsid/--tat/--tn/--mpn` exits with the missing flag names.

Note: in continuous mode SMS is sent only when availability **changes**, so you won't get a text on every poll.

### SMS enabled but job aborts immediately

A job that started with `--sms` but no complete Twilio credential set aborts on launch. Fix one of:

1. **Server env defaults** — set all four in `.env` / systemd `EnvironmentFile=`:
   - `CAMPSLINGER_TWILIO_SID`
   - `CAMPSLINGER_TWILIO_AUTH_TOKEN`
   - `CAMPSLINGER_TWILIO_NUMBER`
   - `CAMPSLINGER_MY_PHONE_NUMBER`
   Then toggle SMS on in the wizard (submenu shows `[env]` for env-supplied fields).

2. **Per-job wizard** — enter all four fields manually in the SMS submenu under More.

Audit log reason: `missing_twilio_creds`.

### `❌ Twilio module not installed`

```bash
source venv/bin/activate
pip install -r requirements.txt
```

### SMS never arrives

- Twilio sandbox numbers require the destination number to be verified in your Twilio console.
- Sandbox accounts have a daily message cap.
- Check the bot's audit log for `❌ SMS failed: …` lines (they're logged via `pp` to the terminal, not the audit log).

## URL validation

### `Unsupported park host: <host>`

Your URL's hostname isn't in `SUPPORTED_PARK_HOSTS` ([campslinger/util.py](../campslinger/util.py)). If the park genuinely uses the same `/create-booking/` Aspira / GoingToCamp UI, add the host to the tuple, restart, and re-test. Update the [README](../README.md) "Supported parks" table to keep documentation in sync.

### `Booking URL must use https`

Use `https://`, not `http://`. The allowlist is HTTPS-only by design.

### `Booking URL path must start with /create-booking/`

Make sure you copied the full URL from the **map / results** page, not from a campaign or marketing page on the same domain.

## Process / environment

### Bot starts then exits with `TELEGRAM_BOT_TOKEN env var is required`

You forgot to load `.env`. Either source it:

```bash
set -a; source .env; set +a; python3 campslinger_tg.py
```

…or use a systemd unit with `EnvironmentFile=`. See the [README → systemd unit](../README.md#-telegram-bot-campslinger_tgpy).

### Audit log file isn't created

Check write permissions on the directory. The bot logs a stderr warning (`Warning: audit log write failed: …`) the first time the write fails, then stays silent to avoid spamming.

### Log entries print twice (or with weird timestamps)

- Two campslinger processes are running at the same time (e.g. forgotten background instance). Run `pgrep -a campslinger`.
- Mixed shells with stale `--no-terminal-log` env / flag. Restart cleanly.

### journalctl shows duplicate timestamps

Under systemd, journald adds its own timestamp. The bot auto-detects `JOURNAL_STREAM` and suppresses the script's `YYYY-MM-DD HH:MM:SS -` prefix. If you still see both, add `--no-log-timestamp` to your systemd unit's `ExecStart=`.

Example with auto-suppression:

```text
Jun 15 02:31:55 campoor python3[139657]: [a1b2c3d4 | Kikomun Creek Provincial Park | jun15-jun20 | s51] No availability. Checking again in ~64s
```

### Multiple jobs — can't tell log lines apart

Each poll line now starts with the job id: `[job_id | Park | dates | sites]`. Even if two jobs share the same park, dates, and filter, the leading 8-char job id (matching `/menu` and debug screenshot filenames) disambiguates them.
