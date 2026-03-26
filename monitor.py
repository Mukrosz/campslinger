#!/usr/bin/env python3
import argparse
import random
import re
import requests
import time
import sys
from datetime import datetime
from urllib.parse import urlparse, parse_qs, urlencode
 
BCPARKS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

def shorten_url(url):
    """Convert long URLs to short """
    s = pyshorteners.Shortener()
    return s.tinyurl.short(url)

def comma_separated_list(value):
    """Converts a comma-separated string into a sorted list of numbers"""
    return sorted([item.strip() for item in value.split(',')], key = sort_key)

def sort_key(s):
    """
    Natural sorting function, sorts a list of alphanumeric values (excluding special characters)

    :param s : alphanumeric value (examples: 2, S15, 18B) 
    :return  : a tuple (example: ("", 2, "") or ("S", 15, "") or ("", 18, "B")
    """
    match = re.match(r'([A-Za-z]*)(\d+)([A-Za-z]*)', s.strip())
    if match:
        prefix, number, suffix = match.groups()
        return (prefix, int(number), suffix)
    return (s, 0, '')  # fallback if unmatched

def send_sms(message, client, to_number, from_number):
    """
    Send a text message to a phone number

    :param str message     : message to send as sms
    :param obj client      : Twilio obj instance
    :param str to_number   : the number to send the message to
    :param str from_number : Twilio number
    """

    message   = client.messages.create(
        to    = to_number,
        from_ = from_number,
        body  = message
    )

    print(f"SMS sent: {message.sid}")

def get_available_sites(sites, desired_sites):
    """
    Filters available campsite listings and returns only whatever is desired, avalable

    :param  dict sites         : all campsites in a given park 
    :param  list desired_sites : user specified campsites
    :return list               : sorted list of available site names
    """
    try:
        available_sites = [ site for site in sites if sites[site].get('status') == 0 and site in desired_sites ]
    except Exception as e:
        sys.exit('Error while determining available sites: {}'.format(e))

    return available_sites

def normalize_sites(n_dict, a_dict):
    """
    Matches and extract needed bits from two dicts to combine into one usable one

    :param   dict n_dict : dictionary of the all campsites and their names for given url 
    :param   dict a_dict : dictionary of campsites' availility for given url 

    :return: dict        : sorted dict of campsites' name, availability and id
    """
    merged = {}
    try:
        for key in a_dict.get('resourceAvailabilities', {}):
            name  = n_dict[key].get('localizedValues', {})[0].get('name', '')
            status = a_dict.get('resourceAvailabilities', {}).get(key, {})[0].get('availability', '')
            merged[name] = {'status': status, 'id': key}
    except Exception as e:
        sys.exit('Error nomalizing two dicts: {}'.format(e))

    return {k: merged[k] for k in sorted(merged, key=sort_key)}

def parse_url(url, params):
    """
    Extracts required parameters from the url

    :param  str  url        : url of the camping site 
    :param  list params     : parameters to extact from the url
    :return dict url_params : required url parameters 
    """

    try:
        url_params = parse_qs(urlparse(url).query)
        url_params = {key: url_params[key][0] for key in params if key in url_params}
        if len(url_params) != len(params):
            missing_params =  set(params) - set(url_params.keys())
            raise ValueError('Missing params: {}'.format(missing_params))
    except Exception as e:
        sys.exit('Invalid URL: {}'.format(e))
    
    return url_params 

def make_request(url, headers):
    try:
        response = requests.get(url, headers = headers)
        response = response.json()
    except Exception as e:
        sys.exit('Error fetching {}: {}'.format(url, e))

    return response


def fetch_park_name(url):
    """
    Best-effort park name lookup by resourceLocationId.
    Returns None when not found or on any request/shape failure.
    """
    try:
        p = parse_url(url, ["resourceLocationId"])
        rid_str = p["resourceLocationId"]
        rid_int = int(rid_str)
        loc_url = "https://camping.bcparks.ca/api/resourcelocation?resourceLocationId={}".format(
            rid_str
        )
        data = requests.get(loc_url, headers=BCPARKS_HEADERS, timeout=30).json()
        if not isinstance(data, list):
            return None
        for loc in data:
            if not isinstance(loc, dict) or loc.get("resourceLocationId") != rid_int:
                continue
            locs = loc.get("localizedValues")
            if not isinstance(locs, list) or not locs or not isinstance(locs[0], dict):
                return None
            first = locs[0]
            for key in ("fullName", "shortName", "name", "value"):
                v = first.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            return None
    except Exception:
        return None
    return None


def randomized_probe_wait_seconds(interval_seconds, jitter_seconds):
    base = max(1, int(interval_seconds))
    spread = max(0, int(jitter_seconds or 0))
    if spread == 0:
        return base
    low = max(1, base - spread)
    high = base + spread
    return random.randint(low, high)


if __name__ == '__main__':

    # PyShorter - TinyURL
    import pyshorteners

    # Twilio API
    from twilio.rest import Client

    description = ("This script monitors available campsites based on the provided URL \n"
                   "For full README, check https://github.com/Mukrosz/campslinger \n"
                   " ---< Examples >--- \n"
                   "Check for site availability : \n"
                   "  ./monitor.py --u 'https://camping.bcparks.ca/create-booking...'  \n\n"
                   "Check for site availability for specific sites : \n"
                   "  ./monitor.py --u 'https://camping.bcparks.ca/create-booking...' --f '10,92,S18,S32B'  \n\n"
                   "Check for site availability and get and sms notification (check twilio_* arguments): \n"
                   "  ./monitor.py --u 'https://camping.bcparks.ca/create-booking...' --s \n\n"
                   "Check for site availability every 30s insead the default 60s: \n"
                   "  ./monitor.py --u 'https://camping.bcparks.ca/create-booking...' --s --i 30 \n\n"
                   "Use jitter of 10s around interval (e.g. 50-70s when --i 60): \n"
                   "  ./monitor.py --u 'https://camping.bcparks.ca/create-booking...' --i 60 --jitter 10 \n\n"
                   "Get an SMS notification when a site becomes available (requires Twilio account): \n"
                   "  ./monitor.py --u 'https://camping.bcparks.ca/create-booking...' --s --i 30 \\\n"
                   "                  --twilio_sid X --twilio_auth_token X --twilio_number X \\\n"
                   "                  --my_phone_number X \n\n")
    parser = argparse.ArgumentParser(description     = description,
                                     formatter_class = argparse.RawTextHelpFormatter
    )
    parser.add_argument('--url',
                         help     = 'https://camping.bcparks.ca/create-booking...',
                         required = True 
    )
    parser.add_argument('--interval', '--i',
                         help     = 'Interval between checks in seconds',
                         type     = int,
                         default  = 60,
                         required = False
    )
    parser.add_argument('--jitter', '--ij',
                         help     = 'Random variance in seconds around --interval (default: 10)',
                         type     = int,
                         default  = 10,
                         required = False
    )
    parser.add_argument('--filter','--f',
                         help     = 'Filter specified sites',
                         type     = comma_separated_list,
                         required = False 
    )
    parser.add_argument('--sms', '--s',
                         help     = 'Enable SMS notification',
                         action   = 'store_true',
                         default  = False,
                         required = False
    )
    parser.add_argument('--twilio_sid', '--tsid',
                         help     = 'Twilio account sid',
                         default  = '',
                         required = False
    )
    parser.add_argument('--twilio_auth_token', '--tat',
                         help     = 'Twilio auth token',
                         default  = '',
                         required = False
    )
    parser.add_argument('--twilio_number', '--tn',
                         help     = 'Twilio phone number',
                         default  = '',
                         required = False
    )
    parser.add_argument('--my_phone_number', '--mpn',
                         help     = 'My phone number',
                         default  = '',
                         required = False
    )

    args = parser.parse_args()

    if args.sms:
        # Initialize Twilio client
        client = Client(args.twilio_sid, args.twilio_auth_token)

    site_name_params = parse_url(args.url, ["resourceLocationId", "mapId"])
    site_status_params = parse_url(args.url, ["mapId", "startDate", "endDate"])

    url_base = 'https://camping.bcparks.ca/api/'
    site_names_url  = '{}resourcelocation/resources?{}'.format(url_base, urlencode(site_name_params))
    site_status_url = '{}availability/map?{}'.format(url_base, urlencode(site_status_params))

    headers = BCPARKS_HEADERS
    park_name = fetch_park_name(args.url)

    try:
        while True:

            site_names_dict = make_request(site_names_url, headers)
            site_status_dict = make_request(site_status_url, headers)

            sites = normalize_sites(site_names_dict, site_status_dict)

            available_sites = get_available_sites(sites, args.filter if args.filter else list(sites.keys()))

            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            prefix = '[{}] '.format(park_name) if park_name else ''
            if available_sites:
                print('{} - {}Available sites: {}'.format(timestamp, prefix, ','.join(available_sites)))
                if args.sms:
                    send_sms('{} - {}Available sites: {}\n{}'.format(timestamp, prefix, ','.join(available_sites), shorten_url(args.url)),
                             client,
                             args.my_phone_number,
                             args.twilio_number
                    )
            else:
                print('{} - {}No availability'.format(timestamp, prefix))

            wait_s = randomized_probe_wait_seconds(args.interval, args.jitter)
            time.sleep(wait_s)

    except KeyboardInterrupt:
        print("Stopping the script.")

    except Exception as e:
        print('❌ Unexpected error: {}'.format(e))
