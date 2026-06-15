# Changelog

All notable changes to this project. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) loosely.

## [Unreleased]

## 2026-06-14 — Multi-job UX, logging, Twilio env defaults

### Added
- **Multi-job UX:** `/help` shows a live job dashboard with per-job buttons (Status, Cancel, Export, Edit, Restart).
- **`/menu`** alias for `/jobs`; **`/cancelall`** and **`/exportall`** bulk commands (also available as inline buttons).
- **Env-based Twilio defaults:** `CAMPSLINGER_TWILIO_*` env vars so SMS can be toggled per job without re-entering credentials.
- **Richer job listings:** `/jobs`, `/status`, and logs show park name, stay dates (`jun15-jun20`), and site filter.
- **journald-friendly logging:** script timestamps auto-suppressed under systemd (`JOURNAL_STREAM`); override with `--log-timestamp` / `--no-log-timestamp`.
- **Log context prefix:** `[Park | dates | sites]` on every poll line so multiple jobs are distinguishable in `journalctl`.
- **API poll errors** include exception class name (e.g. `ReadTimeout`) for easier diagnosis.

### Documentation
- Updated README, `docs/architecture.md`, `docs/audit-log.md`, and `docs/troubleshooting.md` for multi-job dashboard, env vars, logging, and reboot recovery workflow.

## 2026-04-30 — Documentation overhaul
- Cross-compared scripts against `README.md` and rewrote it for clarity, scannability, and visual polish (badges, callouts, sample outputs, mermaid sequence diagrams, anchor-safe headings).
- Added `docs/architecture.md`, `docs/audit-log.md`, and `docs/troubleshooting.md`.
- Added `.env.example` covering exactly the three environment variables the bot actually reads, with an explicit "what is NOT an env variable" section.
- Expanded module docstrings, `argparse` descriptions, and `telegram_help_text()` so `--help` reads like a mini-README.
- Documented the Booking URL constraints enforced by `validate_booking_url()`, the audit-log JSON shape, the BotFather `/setcommands` snippet, the full systemd unit, and the `--rip`/`--rp` + `--max-concurrent 1` recommendation.

### Fixed
- **NB host typo:** `camping.nbbparks.ca` → `camping.nbparks.ca` in [campslinger/util.py](campslinger/util.py) (and the README supported-parks table). New Brunswick URLs were previously rejected as unsupported.
- **`--no-terminal-log` was partially ignored:** the Telegram error-handler imported `_TERMINAL_LOG_ENABLED` by name at module load and never observed `set_terminal_log_enabled()`. Replaced with a `terminal_log_enabled()` helper that reads the live value.

### Added
- **Soft warning** when `--debug` is passed without `--reserve` on the CLI (monitor-only mode produces no Selenium and no screenshots, so `--debug` was a silent no-op).

## 2026-04-13 — Warmode click delay

### Added
- `--warmode-click-delay` / `--wcd` (CLI and Telegram) — milliseconds to wait after the 07:00 open time before clicking Reserve. Avoids "Cannot Reserve — these dates cannot be reserved until …" rejections caused by the bot crossing the boundary a tick before the park's server.
- Telegram wizard: **WM delay** button appears next to **Warmode** when Warmode is on.

## 2026-04-08 — Descriptive screenshot filenames

### Changed
- Debug screenshots now embed timestamp, slugified park name, stay window, and a short tag, e.g. `ss_2026.04.08-22.03.11_kikomun-creek-provincial-park_jul08-jul14_bcr.png` (`bcr` = before click reserve, `acr` = after, `acs` = after click site, `mapfail` = map load failure dump).
- All screenshots from one reservation attempt share the same timestamp, so they sort and group naturally.

## 2026-04-04 — Multi-park support (Aspira / GoingToCamp)

### Changed
- Generalized the tool from BC-Parks-specific to the full Aspira / GoingToCamp platform. URL validation now uses `SUPPORTED_PARK_HOSTS` allowlist (12 hosts at launch).
- Renamed `bcparks_*` helpers to generic `fetch_sites_map`, `fetch_park_name`, etc. The API base is derived from the booking URL hostname.
- README, help text, audit fields, and Telegram bot prompts no longer hardcode "BC Parks".

## 2026-03-28 — Monitor-first redesign

### Added
- New unified entrypoints `campslinger.py` (CLI) and `campslinger_tg.py` (Telegram bot) replacing `monitor.py`, `reserve.py`, and `reserve_tg.py`.
- Shared logic moved into a `campslinger/` package (`core`, `log`, `reserve_modes`, `selenium_ops`, `util`).
- Telegram bot: monitor is the primary flow; Reserve, Warmode, Debug, Loop, and SMS are opt-in via the More menu.
- Telegram bot: per-user job ownership, `/jobs`, `/status`, `/cancel` quick-action keyboard.

### Deprecated
- `_archive/monitor.py`, `_archive/reserve.py`, `_archive/reserve_tg.py` kept for reference; will be removed once the new scripts are validated.
