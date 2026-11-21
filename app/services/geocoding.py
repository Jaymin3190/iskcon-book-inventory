import json
import threading
import time
import urllib.parse
import urllib.request

_REQUEST_INTERVAL = 1.0
_USER_AGENT = "iskcon-book-inventory/1.0"
_request_lock = threading.Lock()
_last_request_at = 0.0
_area_cache = {}
_area_cache_limit = 512


def _area_from_result(result):
    if not isinstance(result, dict):
        raise ValueError("OpenStreetMap returned an invalid response")
    address = result.get("address")
    if not isinstance(address, dict):
        address = {}

    locality = next(
        (
            address[key]
            for key in ("neighbourhood", "suburb", "quarter", "city_district", "residential", "village")
            if address.get(key)
        ),
        None,
    )
    city = next(
        (
            address[key]
            for key in ("city", "town", "municipality", "county")
            if address.get(key) and address[key] != locality
        ),
        None,
    )
    district = address.get("state_district")
    state = address.get("state")

    parts = []
    for part in (locality, city, district, state):
        if part and part.casefold() not in {existing.casefold() for existing in parts}:
            parts.append(part)

    area = ", ".join(parts) or result.get("display_name")
    if not isinstance(area, str) or not area.strip():
        raise ValueError("OpenStreetMap returned no area details")
    return area.strip()


def reverse_geocode(latitude, longitude):
    global _last_request_at

    cache_key = (round(latitude, 5), round(longitude, 5))
    with _request_lock:
        cached_area = _area_cache.get(cache_key)
        if cached_area:
            return cached_area

        delay = _REQUEST_INTERVAL - (time.monotonic() - _last_request_at)
        if delay > 0:
            time.sleep(delay)

        query = urllib.parse.urlencode({
            "format": "jsonv2",
            "lat": latitude,
            "lon": longitude,
            "zoom": 18,
            "addressdetails": 1,
        })
        request = urllib.request.Request(
            f"https://nominatim.openstreetmap.org/reverse?{query}",
            headers={
                "User-Agent": _USER_AGENT,
                "Accept-Language": "en",
            },
        )
        _last_request_at = time.monotonic()
        with urllib.request.urlopen(request, timeout=3) as response:
            result = json.loads(response.read().decode("utf-8"))
        area = _area_from_result(result)
        if len(_area_cache) >= _area_cache_limit:
            _area_cache.pop(next(iter(_area_cache)))
        _area_cache[cache_key] = area
        return area
