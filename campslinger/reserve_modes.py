"""Reservation loop strategies: normal polling and warmode (timed 07:00 click)."""

import os
import sys
import time
from datetime import datetime

from campslinger.core import (
    api_available_labels,
    bcparks_fetch_sites_map,
    pick_api_target,
)
from campslinger.log import pp
from campslinger.selenium_ops import (
    collect_available_icons_from_map,
    prepare_reservation,
)
from campslinger.util import (
    debug_screenshot,
    randomized_probe_wait_seconds,
    sort_key,
)


def reserve_normal_mode(driver, url, requested_sites, interval, interval_jitter=10, debug=False, stop_event=None):
    while True:
        wait_s = randomized_probe_wait_seconds(interval, interval_jitter)
        if stop_event and stop_event.is_set():
            pp("🛑 Cancellation requested")
            return ""
        try:
            sites = bcparks_fetch_sites_map(url)
        except Exception as e:
            pp("❌ API poll failed: {}".format(e), telegram_digest=("api_err", str(e)[:220]))
            if stop_event and stop_event.wait(wait_s):
                pp("🛑 Cancellation requested")
                return ""
            time.sleep(0 if stop_event else wait_s)
            continue
        avail = api_available_labels(sites)
        if not avail:
            pp("❌ No availability", telegram_digest=("zero",))
            if stop_event and stop_event.wait(wait_s):
                pp("🛑 Cancellation requested")
                return ""
            time.sleep(0 if stop_event else wait_s)
            continue
        target = pick_api_target(sites, requested_sites)
        if not target:
            labels_csv = ",".join(sorted(avail, key=sort_key))
            if requested_sites:
                pp("✨ Available sites: {}\n❌ None of your preferred sites ({}) are free.".format(
                    labels_csv, ",".join(requested_sites)),
                    telegram_digest=("filter_wait", frozenset(avail), tuple(requested_sites)))
            else:
                pp("✨ Available sites: {}\n❌ Could not pick a target site; retrying…".format(labels_csv),
                    telegram_digest=("no_pick_wait", frozenset(avail)))
            if stop_event and stop_event.wait(wait_s):
                pp("🛑 Cancellation requested")
                return ""
            time.sleep(0 if stop_event else wait_s)
            continue
        pp("✨ Available sites: {}".format(",".join(sorted(avail, key=sort_key))), skip_telegram=True)
        label = sites[target].get("label", target)
        pp("🎯 Trying map + Reserve for: {} …".format(label))
        on_map = collect_available_icons_from_map(driver, url, debug=debug, stop_event=stop_event)
        if target not in on_map:
            pp("⚠️  API shows {} but map has no matching available icon yet; waiting {}s…".format(
                label, wait_s), telegram_digest=("map_wait", label))
            if stop_event and stop_event.wait(wait_s):
                pp("🛑 Cancellation requested")
                return ""
            time.sleep(0 if stop_event else wait_s)
            continue
        site, reserve_button = prepare_reservation(driver, on_map, [target], debug=debug)
        if site and reserve_button:
            try:
                driver.execute_script("arguments[0].scrollIntoView(true);", reserve_button)
                if debug:
                    debug_screenshot(driver, os.path.join(os.getcwd(), "ss-before_clicking_reserve.png"))
                driver.execute_script("arguments[0].click();", reserve_button)
                pp("✅ Clicked the Reserve button")
                time.sleep(5)
                if debug:
                    debug_screenshot(driver, os.path.join(os.getcwd(), "ss-after_clicking_reserve.png"))
                return site
            except Exception as e:
                pp("❌ Failed to click reserve button: {}".format(e))
        else:
            pp("❌ Could not prepare reservation", telegram_digest=("prep_fail", label))
        if stop_event and stop_event.wait(wait_s):
            pp("🛑 Cancellation requested")
            return ""
        time.sleep(0 if stop_event else wait_s)


def reserve_war_mode(driver, url, requested_sites, timezone="US/Pacific", debug=False, stop_event=None):
    try:
        import pytz
        from datetime import timedelta
    except ImportError:
        sys.exit("Error: pytz module not found. Install with `pip install pytz`")

    from campslinger.selenium_ops import get_available_sites

    def wait_until(target_time):
        while True:
            if stop_event and stop_event.is_set():
                return False
            now = datetime.now(tz=target_time.tzinfo)
            if now >= target_time:
                return True
            time.sleep(min(0.05, (target_time - now).total_seconds()))

    tz = pytz.timezone(timezone)
    now = datetime.now(tz)
    target_time = now.replace(hour=7, minute=0, second=0, microsecond=0)
    if now >= target_time:
        target_time += timedelta(days=1)

    def fmt_ampm(dt):
        try:
            return dt.strftime("%-I:%M%p")
        except ValueError:
            return dt.strftime("%I:%M%p").lstrip("0")

    pp("⚔️  Warmode: prefetch at {} US/Pacific (07:00 window)…".format(
        fmt_ampm(target_time - timedelta(minutes=1))))
    if not wait_until(target_time - timedelta(minutes=1)):
        pp("🛑 Cancellation requested")
        return ""
    available_sites = get_available_sites(driver, url, debug=debug, stop_event=stop_event)
    if not available_sites:
        pp("❌ No available sites on map at prefetch time")
        return ""
    site, reserve_button = prepare_reservation(driver, available_sites, requested_sites, debug=debug)
    if not site or not reserve_button:
        pp("❌ Could not prepare reservation (prefetch)")
        return ""
    pp("✅ Prefetch done. Waiting to click Reserve for {} at {}…".format(site, fmt_ampm(target_time)))
    if not wait_until(target_time):
        pp("🛑 Cancellation requested")
        return ""
    try:
        driver.execute_script("arguments[0].scrollIntoView(true);", reserve_button)
        if debug:
            debug_screenshot(driver, os.path.join(os.getcwd(), "ss-before_clicking_reserve.png"))
        driver.execute_script("arguments[0].click();", reserve_button)
        pp("✅ Clicked the Reserve button")
        time.sleep(5)
        if debug:
            debug_screenshot(driver, os.path.join(os.getcwd(), "ss-after_clicking_reserve.png"))
        return site
    except Exception as e:
        pp("❌ Failed to click reserve button at {}: {}".format(fmt_ampm(target_time), e))
        return ""
