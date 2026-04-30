# Troubleshooting

Extended troubleshooting recipes. For the short-form FAQ see the [README → Troubleshooting](../README.md#-troubleshooting--faq) section.

## Warmode

### "Cannot Reserve — these dates cannot be reserved until 07:00"

The script clicked Reserve a few milliseconds before the park's server crossed 07:00 in its own zone. Two-step fix:

1. **Verify the host clock.** Run `timedatectl`. `System clock synchronized: yes` and `NTP service: active` are both required. Local timezone does not matter — `--timezone` controls the warmode target.
2. **Add a small post-open delay.** `--warmode-click-delay 200` is a safe default; bump to `400` or `500` ms for slow VPS hosts. Same option in the Telegram wizard: tap **Warmode** to enable, then **WM delay** to set milliseconds.

> [!TIP]
> Don't go above ~1500 ms. The window where Reserve succeeds is short — too much delay risks losing the site to other reservers.

### Warmode never fires

- Check the bot/CLI is running with `--reserve` set. Warmode is a sub-mode of reserve; it's a no-op without it.
- Check the timezone string is a valid IANA zone (e.g. `US/Pacific`, `America/Toronto`). A typo silently falls through to UTC.
- Check the system clock is correct.

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

### "Unauthorized"

Your numeric Telegram user ID isn't in `TELEGRAM_ALLOWED_USER_IDS`. Use `@userinfobot` to find it. Set the env var (comma-separated, no spaces) and restart the bot.

### "Server busy: max N concurrent jobs reached"

The JobManager cap is full. Either:

- `/cancel <id>` an existing job.
- Restart the bot with a higher `--max-concurrent`.

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
