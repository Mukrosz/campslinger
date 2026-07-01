#!/usr/bin/env python3
"""
campslinger.py -- campsite monitoring and optional reservation (CLI).

Works with any park on the Aspira / GoingToCamp platform.  The booking-URL
hostname must be on the allowlist in campslinger.util.SUPPORTED_PARK_HOSTS
(BC Parks, Ontario Parks, Parks Canada, Manitoba, Nova Scotia, New Brunswick,
NL, Yukon, Michigan, Maryland, Mississippi, Nebraska, ...).

Modes:
  - Monitor (default): API-only polling with terminal output and optional SMS.
                       No Chrome required.
  - Reserve (--reserve): same polling, plus Selenium clicks Reserve on hit.
  - Warmode (--reserve --warmode): no polling.  Prefetches the map at 06:59
    in --timezone (default US/Pacific) and clicks Reserve at 07:00.  Optional
    --warmode-click-delay (ms) lets you wait briefly after the open time to
    avoid "Cannot Reserve" rejections from a server that hasn't crossed the
    boundary yet.

Notes:
  - --debug only takes effect with --reserve (monitor-only mode has no
    Selenium and no screenshots; a stderr note is emitted if you try).
  - --headed and --rip/--rp also require --reserve.
  - Twilio credentials, if used, are passed as flags; this script does not
    read environment variables.

Examples:
  python3 campslinger.py --url '...'                              # monitor only
  python3 campslinger.py --url '...' --f S51,S52 --i 30           # filtered, fast
  python3 campslinger.py --url '...' --f S51 --loop once          # stop on hit
  python3 campslinger.py --url '...' --f S51 --reserve            # monitor + reserve
  python3 campslinger.py --url '...' --reserve --warmode --wcd 400
  python3 campslinger.py --url '...' --reserve --headed
  python3 campslinger.py --url '...' --reserve --rip HOST --rp 9222
"""

import argparse
import signal
import sys
import threading
import time
import uuid

from campslinger.core import (
    api_available_labels,
    fetch_park_name,
    fetch_sites_map,
    labels_available_matching_filter,
)
from campslinger.log import (
    configure_log_timestamps,
    pp,
    set_job_log_context,
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


class _HelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawTextHelpFormatter):
    pass


def build_arg_parser():
    description = (
        "Campsite monitoring and optional reservation.\n"
        "Works with any park on the Aspira / GoingToCamp platform\n"
        "(BC Parks, Ontario Parks, Parks Canada, and 9 more -- see\n"
        "campslinger.util.SUPPORTED_PARK_HOSTS).\n\n"
        "Default: API-only monitoring (no Chrome).\n"
        "With --reserve: Selenium clicks Reserve on hit.\n"
        "Warmode targets 07:00 in --timezone (default US/Pacific).\n"
        "--debug requires --reserve to produce screenshots.\n\n"
        "Examples:\n"
        "  Monitor only (no Chrome):\n"
        "    ./campslinger.py --url 'https://camping.bcparks.ca/create-booking/...'\n"
        "    ./campslinger.py --url 'https://reservations.ontarioparks.ca/create-booking/...'\n\n"
        "  Filtered, fast polling:\n"
        "    ./campslinger.py --url '...' --f S51,S52 --i 30\n\n"
        "  Stop after first hit:\n"
        "    ./campslinger.py --url '...' --f S51 --loop once\n\n"
        "  Reserve on hit (Selenium):\n"
        "    ./campslinger.py --url '...' --f S51 --reserve\n\n"
        "  Warmode (07:00 US/Pacific) with 400 ms safety delay:\n"
        "    ./campslinger.py --url '...' --f S51 --reserve --warmode --wcd 400\n\n"
        "  Reserve with remote Chrome on the LAN:\n"
        "    ./campslinger.py --url '...' --reserve --rip 192.168.1.50 --rp 9222\n\n"
        "  SMS on availability (monitor or reserve):\n"
        "    ./campslinger.py --url '...' --sms --tsid X --tat X --tn X --mpn X"
    )
    p = argparse.ArgumentParser(description=description, formatter_class=_HelpFormatter)
    p.add_argument("--url", "--u", dest="url", required=True, metavar="URL",
                    help="Full park create-booking results URL (any supported platform).")
    p.add_argument("--interval", "--i", type=int, default=60, metavar="SECONDS",
                    help="Seconds between API polls (ignored in warmode).")
    p.add_argument("--jitter", "--ij", type=int, default=10, metavar="SECONDS",
                    help="Random variance in seconds around --interval.")
    p.add_argument("--filter", "--f", type=comma_separated_list, metavar="SITES",
                    help="Comma-separated preferred site labels (order = priority).")
    p.add_argument("--reserve", "--r", action="store_true", default=False,
                    help="Enable Selenium reservation on hit (requires Chrome).")
    p.add_argument("--loop", choices=["continuous", "once"], default="continuous",
                    help="continuous: keep polling. once: stop after first hit.")
    p.add_argument("--warmode", "--w", action="store_true", default=False,
                    help="Warmode: prefetch at 06:59, click Reserve at 07:00 US/Pacific. Requires --reserve.")
    p.add_argument(
        "--warmode-click-delay", "--wcd", type=int, default=0, metavar="MS",
        help="Milliseconds to wait after warmode open time before clicking Reserve (0=immediate). "
             "Only applies with --warmode.",
    )
    p.add_argument("--debug", "--d", action="store_true", default=False,
                    help="Extra diagnostics; screenshots named ss_<time>_<park>_<stay>_bcr|acr|acs|mapfail.png.")
    p.add_argument("--headed", action="store_true", default=False,
                    help="Show Chrome window (requires --reserve).")
    p.add_argument("--timezone", default="US/Pacific", metavar="TZ",
                    help="IANA timezone for warmode.")
    p.add_argument("--remote_ip", "--rip", metavar="HOST",
                    help="Chrome remote debugging host (requires --reserve and --rp).")
    p.add_argument("--remote_port", "--rp", type=int, metavar="PORT",
                    help="Chrome remote debugging port (requires --reserve and --rip).")
    p.add_argument("--sms", "--s", action="store_true", default=False,
                    help="Send SMS on availability (requires Twilio flags).")
    p.add_argument("--twilio_sid", "--tsid", default="", metavar="SID",
                    help="Twilio Account SID.")
    p.add_argument("--twilio_auth_token", "--tat", default="", metavar="TOKEN",
                    help="Twilio auth token.")
    p.add_argument("--twilio_number", "--tn", default="", metavar="FROM",
                    help="Twilio sending phone number.")
    p.add_argument("--my_phone_number", "--mpn", default="", metavar="TO",
                    help="Your phone number to receive SMS.")
    ts_group = p.add_mutually_exclusive_group()
    ts_group.add_argument("--log-timestamp", action="store_true", dest="log_timestamp",
                          default=None, help="Prefix log lines with a timestamp (default: auto).")
    ts_group.add_argument("--no-log-timestamp", action="store_false", dest="log_timestamp",
                          help="Omit script timestamps (auto-off under systemd journald).")
    return p


def _validate_args(args):
    try:
        validate_booking_url(args.url)
    except ValueError as e:
        sys.exit("Error: {}".format(e))
    if args.warmode and not args.reserve:
        sys.exit("Error: --warmode requires --reserve")
    if args.headed and not args.reserve:
        sys.exit("Error: --headed requires --reserve")
    rip = (args.remote_ip or "").strip()
    rp = args.remote_port
    if bool(rip) != (rp is not None):
        sys.exit("Error: --rip and --rp must be used together")
    if rip and not args.reserve:
        sys.exit("Error: --rip/--rp require --reserve")
    wcd = int(getattr(args, "warmode_click_delay", 0) or 0)
    if wcd < 0:
        sys.exit("Error: --warmode-click-delay must be >= 0")
    if wcd and not args.warmode:
        sys.exit("Error: --warmode-click-delay is only valid with --warmode")
    if args.debug and not args.reserve:
        print(
            "Note: --debug only takes effect with --reserve "
            "(monitor-only mode has no Selenium/screenshots).",
            file=sys.stderr, flush=True,
        )


def _setup_twilio(args):
    if not args.sms:
        return None
    missing = [
        name for name, val in (
            ("--tsid", args.twilio_sid),
            ("--tat", args.twilio_auth_token),
            ("--tn", args.twilio_number),
            ("--mpn", args.my_phone_number),
        ) if not (val or "").strip()
    ]
    if missing:
        sys.exit("Error: --sms requires Twilio flags; missing: {}".format(", ".join(missing)))
    try:
        from twilio.rest import Client
        return Client(args.twilio_sid, args.twilio_auth_token)
    except ImportError:
        sys.exit("Error: pip install twilio")


def _startup_banner(args, park, stay, job_id):
    if args.reserve and args.warmode:
        mode = "warmode"
    elif args.reserve:
        mode = "reserve"
    else:
        mode = "monitor"
    bits = [
        "campslinger {}".format(job_id),
        "mode={}".format(mode),
        "park={}".format(park or "?"),
        "stay={}".format(stay),
        "interval={}s±{}s".format(args.interval, args.jitter),
        "filter={}".format(",".join(args.filter) if args.filter else "all"),
        "loop={}".format(args.loop),
    ]
    if mode == "warmode":
        bits.append("tz={}".format(args.timezone))
        if int(args.warmode_click_delay or 0) > 0:
            bits.append("wcd={}ms".format(args.warmode_click_delay))
    if args.sms:
        bits.append("sms=on")
    pp("🏕️  " + " | ".join(bits))


def _monitor_loop(args, client, park_name=None, stop_event=None):
    """API-only polling loop (no Selenium).  SMS fires on availability
    transitions, not every poll, to avoid duplicate paid messages."""
    last_hit = None
    while True:
        if stop_event and stop_event.is_set():
            break
        wait_s = randomized_probe_wait_seconds(args.interval, args.jitter)
        try:
            sites = fetch_sites_map(args.url)
        except Exception as e:
            pp("❌ API poll failed ({}): {}. Retrying in ~{}s".format(
                type(e).__name__, e, wait_s))
            if stop_event and stop_event.wait(wait_s):
                break
            if not stop_event:
                time.sleep(wait_s)
            continue

        matching = labels_available_matching_filter(sites, args.filter)
        all_avail = api_available_labels(sites)

        if matching:
            once = args.loop == "once"
            eta = "" if once else " — checking again in ~{}s".format(wait_s)
            pp("✅ Available sites: {}{}".format(",".join(matching), eta))
            new_hit = availability_digest(matching)
            changed = new_hit != last_hit
            last_hit = new_hit
            if client and (once or changed):
                try:
                    prefix = "[{}] ".format(park_name) if park_name else ""
                    stay_prefix = "[{}] ".format(stay) if stay else ""
                    body = "{} - {}{}Available sites: {}\n{}".format(
                        current_time(), prefix, stay_prefix, ",".join(matching), shorten_url(args.url))
                    send_sms(body, client, args.my_phone_number, args.twilio_number)
                except Exception as e:
                    pp("❌ SMS failed: {}".format(e))
            elif client and not changed:
                pp("SMS skipped (same availability as last hit)", skip_telegram=True)
            if once:
                pp("✅ Done (--loop once).")
                return
        elif not all_avail:
            last_hit = None
            pp("No availability. Checking again in ~{}s".format(wait_s))
        else:
            last_hit = None
            labels_csv = ",".join(sorted(all_avail, key=sort_key))
            if args.filter:
                pp("✨ Available: {} | ❌ None of your preferred sites ({}) are free. Checking again in ~{}s".format(
                    labels_csv, ",".join(args.filter), wait_s))
            else:
                pp("✨ Available: {}. Checking again in ~{}s".format(labels_csv, wait_s))

        if stop_event and stop_event.wait(wait_s):
            break
        if not stop_event:
            time.sleep(wait_s)


def main():
    args = build_arg_parser().parse_args()
    configure_log_timestamps(getattr(args, "log_timestamp", None))
    _validate_args(args)

    job_id = uuid.uuid4().hex[:8]
    park = fetch_park_name(args.url)
    stay = stay_window_label(args.url)
    site_filter = ",".join(args.filter) if args.filter else None
    set_job_log_context(
        park_name=park, stay_label=stay, site_filter=site_filter,
        interval_seconds=args.interval, interval_jitter_seconds=args.jitter,
        job_id=job_id,
    )
    client = _setup_twilio(args)
    _startup_banner(args, park, stay, job_id)

    stop_event = threading.Event()

    def _handle_term(signum, frame):
        pp("🛑 Received signal {}; shutting down…".format(signum))
        stop_event.set()

    try:
        signal.signal(signal.SIGTERM, _handle_term)
    except (ValueError, OSError):
        pass

    if not args.reserve:
        try:
            _monitor_loop(args, client, park_name=park, stop_event=stop_event)
        except KeyboardInterrupt:
            pp("🛑 Interrupted")
        return

    rip = (args.remote_ip or "").strip()
    rp = args.remote_port
    if rip and rp:
        driver = setup_webdriver_remote(rip, rp)
    else:
        driver = setup_webdriver(headed=args.headed)
    if not driver:
        sys.exit("❌ WebDriver initialization failed.")
    use_remote = bool(rip and rp)

    try:
        if args.warmode:
            reserved, reason = reserve_war_mode(
                driver, args.url, args.filter,
                timezone=args.timezone, debug=args.debug, stop_event=stop_event,
                warmode_click_delay_ms=int(args.warmode_click_delay or 0),
            )
        else:
            reserved, reason = reserve_normal_mode(
                driver, args.url, args.filter,
                interval=args.interval, interval_jitter=args.jitter,
                debug=args.debug, stop_event=stop_event)
        if reserved:
            pp("🎯 Reserved: {}".format(reserved))
            if client:
                stay_prefix = "[{}] ".format(stay) if stay else ""
                park_prefix = "[{}] ".format(park) if park else ""
                send_sms("{} - {}{}🎯 Reserved: {}\n{}".format(
                    current_time(), park_prefix, stay_prefix, reserved, shorten_url(args.url)),
                    client, args.my_phone_number, args.twilio_number)
        else:
            pp("❌ No reservation was successful (reason: {})".format(reason or "unknown"))
    except ImportError as e:
        pp("❌ Missing dependency: {}. (warmode needs `pip install pytz`)".format(e))
    except KeyboardInterrupt:
        pp("🛑 Interrupted")
    except Exception as e:
        pp("❌ Unexpected error: {}".format(e))
    finally:
        if not use_remote:
            driver.quit()


if __name__ == "__main__":
    main()
