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
import sys
import time

from campslinger.core import (
    api_available_labels,
    fetch_park_name,
    fetch_sites_map,
    labels_available_matching_filter,
)
from campslinger.log import pp, set_park_name
from campslinger.reserve_modes import reserve_normal_mode, reserve_war_mode
from campslinger.selenium_ops import setup_webdriver, setup_webdriver_remote
from campslinger.util import (
    comma_separated_list,
    current_time,
    randomized_probe_wait_seconds,
    send_sms,
    shorten_url,
    sort_key,
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
    return p


def _validate_args(args):
    validate_booking_url(args.url)
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
    try:
        from twilio.rest import Client
        return Client(args.twilio_sid, args.twilio_auth_token)
    except ImportError:
        sys.exit("Error: pip install twilio")


def _monitor_loop(args, client):
    """API-only polling loop (no Selenium)."""
    while True:
        wait_s = randomized_probe_wait_seconds(args.interval, args.jitter)
        try:
            sites = fetch_sites_map(args.url)
        except Exception as e:
            pp("❌ API poll failed: {}".format(e))
            time.sleep(wait_s)
            continue

        matching = labels_available_matching_filter(sites, args.filter)
        all_avail = api_available_labels(sites)

        if matching:
            pp("✅ Available sites: {}".format(",".join(matching)))
            if client:
                try:
                    park = getattr(pp, "__self__", None)
                    body = "{} - Available sites: {}\n{}".format(
                        current_time(), ",".join(matching), shorten_url(args.url))
                    send_sms(body, client, args.my_phone_number, args.twilio_number)
                except Exception as e:
                    pp("❌ SMS failed: {}".format(e))
            if args.loop == "once":
                pp("✅ Done (--loop once).")
                return
        elif not all_avail:
            pp("No availability")
        else:
            labels_csv = ",".join(sorted(all_avail, key=sort_key))
            if args.filter:
                pp("✨ Available: {} | ❌ None of your preferred sites ({}) are free.".format(
                    labels_csv, ",".join(args.filter)))
            else:
                pp("✨ Available: {}".format(labels_csv))

        time.sleep(wait_s)


def main():
    args = build_arg_parser().parse_args()
    _validate_args(args)

    park = fetch_park_name(args.url)
    set_park_name(park)
    client = _setup_twilio(args)

    if not args.reserve:
        try:
            _monitor_loop(args, client)
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
            reserved = reserve_war_mode(
                driver, args.url, args.filter,
                timezone=args.timezone, debug=args.debug,
                warmode_click_delay_ms=int(args.warmode_click_delay or 0),
            )
        else:
            reserved = reserve_normal_mode(
                driver, args.url, args.filter,
                interval=args.interval, interval_jitter=args.jitter,
                debug=args.debug)
        if reserved:
            pp("🎯 Reserved: {}".format(reserved))
            if client:
                send_sms("{} - 🎯 Reserved: {}\n{}".format(
                    current_time(), reserved, shorten_url(args.url)),
                    client, args.my_phone_number, args.twilio_number)
        else:
            pp("❌ No reservation was successful")
    except KeyboardInterrupt:
        pp("🛑 Interrupted")
    except Exception as e:
        pp("❌ Unexpected error: {}".format(e))
    finally:
        if not use_remote:
            driver.quit()


if __name__ == "__main__":
    main()
