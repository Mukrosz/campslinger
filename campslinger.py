#!/usr/bin/env python3
"""
campslinger.py -- BC Parks monitoring and optional reservation (CLI).

Default: API-only monitoring with terminal output and optional SMS.
With --reserve: also drives Chrome to click Reserve when a site is available.

Examples:
  python3 campslinger.py --url '...'                         # monitor only
  python3 campslinger.py --url '...' --reserve               # monitor + reserve
  python3 campslinger.py --url '...' --loop once             # stop after first hit
  python3 campslinger.py --url '...' --reserve --warmode     # 07:00 reserve window
  python3 campslinger.py --url '...' --reserve --headed      # visible Chrome
  python3 campslinger.py --url '...' --reserve --rip HOST --rp 9222
"""

import argparse
import sys
import time

from campslinger.core import (
    api_available_labels,
    bcparks_fetch_park_name,
    bcparks_fetch_sites_map,
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
    validate_bcparks_booking_url,
)


class _HelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawTextHelpFormatter):
    pass


def build_arg_parser():
    description = (
        "BC Parks monitoring and optional reservation.\n\n"
        "Default: API-only monitoring (no Chrome).\n"
        "With --reserve: Selenium clicks Reserve on hit.\n\n"
        "Examples:\n"
        "  Monitor any availability:\n"
        "    ./campslinger.py --url 'https://camping.bcparks.ca/create-booking/...'\n\n"
        "  Monitor specific sites:\n"
        "    ./campslinger.py --url '...' --f S51,S52 --i 30\n\n"
        "  Stop after first hit:\n"
        "    ./campslinger.py --url '...' --f S51 --loop once\n\n"
        "  Reserve on hit (Selenium):\n"
        "    ./campslinger.py --url '...' --f S51 --reserve\n\n"
        "  Warmode reserve (07:00 US/Pacific):\n"
        "    ./campslinger.py --url '...' --f S51 --reserve --warmode\n\n"
        "  Reserve with remote Chrome:\n"
        "    ./campslinger.py --url '...' --reserve --rip 192.168.1.50 --rp 9222\n\n"
        "  SMS on availability (monitor or reserve):\n"
        "    ./campslinger.py --url '...' --sms --tsid X --tat X --tn X --mpn X"
    )
    p = argparse.ArgumentParser(description=description, formatter_class=_HelpFormatter)
    p.add_argument("--url", "--u", dest="url", required=True, metavar="URL",
                    help="Full BC Parks create-booking results URL.")
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
    p.add_argument("--debug", "--d", action="store_true", default=False,
                    help="Extra diagnostics and screenshots on map failures.")
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
    validate_bcparks_booking_url(args.url)
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
            sites = bcparks_fetch_sites_map(args.url)
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

    park = bcparks_fetch_park_name(args.url)
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
                timezone=args.timezone, debug=args.debug)
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
