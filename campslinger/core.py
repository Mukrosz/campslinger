"""Park platform API layer: site availability, park name, URL parsing."""

from urllib.parse import urlencode

import requests

from campslinger.util import api_base_from_url, sort_key, validate_booking_url

API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}


def parse_booking_url(url, params):
    from urllib.parse import parse_qs, urlparse
    try:
        url_params = parse_qs(urlparse(url).query)
        url_params = {key: url_params[key][0] for key in params if key in url_params}
        if len(url_params) != len(params):
            missing = set(params) - set(url_params.keys())
            raise ValueError("Missing params: {}".format(missing))
    except Exception as e:
        raise ValueError("Invalid URL: {}".format(e)) from e
    return url_params


def normalize_sites(n_dict, a_dict):
    merged = {}
    for key in a_dict.get("resourceAvailabilities", {}):
        name = n_dict[key].get("localizedValues", {})[0].get("name", "")
        status = (
            a_dict.get("resourceAvailabilities", {})
            .get(key, {})[0]
            .get("availability", "")
        )
        label = name.strip()
        merged[label.lower()] = {"status": status, "id": key, "label": label}
    return {k: merged[k] for k in sorted(merged, key=sort_key)}


def fetch_sites_map(booking_url):
    validate_booking_url(booking_url)
    api_base = api_base_from_url(booking_url)
    site_name_params = parse_booking_url(booking_url, ["resourceLocationId", "mapId"])
    site_status_params = parse_booking_url(booking_url, ["mapId", "startDate", "endDate"])
    names_url = "{}resourcelocation/resources?{}".format(api_base, urlencode(site_name_params))
    status_url = "{}availability/map?{}".format(api_base, urlencode(site_status_params))
    r1 = requests.get(names_url, headers=API_HEADERS, timeout=30)
    r1.raise_for_status()
    r2 = requests.get(status_url, headers=API_HEADERS, timeout=30)
    r2.raise_for_status()
    return normalize_sites(r1.json(), r2.json())


def fetch_park_name(booking_url):
    try:
        validate_booking_url(booking_url)
        api_base = api_base_from_url(booking_url)
        p = parse_booking_url(booking_url, ["resourceLocationId"])
        rid_str = p["resourceLocationId"]
        rid_int = int(rid_str)
        loc_url = "{}resourcelocation?resourceLocationId={}".format(api_base, rid_str)
        r = requests.get(loc_url, headers=API_HEADERS, timeout=30)
        r.raise_for_status()
        data = r.json()
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


def api_available_labels(sites):
    labels = []
    for key in sorted(sites.keys(), key=sort_key):
        if sites[key].get("status") == 0:
            labels.append(sites[key].get("label", key))
    return labels


def pick_api_target(sites, requested_sites):
    if not sites:
        return None
    pool = requested_sites if requested_sites else list(sites.keys())
    for key in pool:
        if key in sites and sites[key].get("status") == 0:
            return key
    return None


def labels_available_matching_filter(sites, requested_sites):
    out = []
    for key in sorted(sites.keys(), key=sort_key):
        if sites[key].get("status") != 0:
            continue
        if requested_sites and key not in requested_sites:
            continue
        out.append(sites[key].get("label", key))
    return out
