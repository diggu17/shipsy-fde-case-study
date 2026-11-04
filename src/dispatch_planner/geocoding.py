"""Geocoding providers. Each turns a place into one common result, so providers can be swapped."""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

_TIMEOUT_S = 5
_MAX_ERRORS_IN_A_ROW = 3
_USER_AGENT = "dispatch-planner-prototype/0.1"
_GOOGLE_BUILDING_TYPES = {"premise", "subpremise", "establishment", "point_of_interest"}


class GeocodingError(Exception):
    """The service couldn't answer, which is different from finding nothing."""


class NoMapService:
    """Used when a client has no geocoding provider."""

    def locate(self, query):
        return None


def _fetch_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
            return json.load(response)
    except (OSError, ValueError) as error:
        raise GeocodingError(str(error)) from None


def _words(text):
    return re.sub(r"[^\w ]", " ", text.casefold()).split()


class NominatimGeocoder:
    """OpenStreetMap's public service: no key, at most one request a second."""

    min_interval_s = 1.1

    def __init__(self, fetch=_fetch_json):
        self._fetch = fetch

    def locate(self, query):
        text = ", ".join(query[key] for key in ("place", "area", "region"))
        params = {"format": "jsonv2", "limit": 1, "accept-language": "en", "q": text, "countrycodes": query["country_code"].lower()}
        results = self._fetch(f"https://nominatim.openstreetmap.org/search?{urllib.parse.urlencode(params)}")
        if not results:
            return None
        result = results[0]
        name = set(_words(result.get("display_name", "")))
        return {
            "lat": float(result["lat"]),
            "lng": float(result["lon"]),
            "specific": result.get("place_rank", 0) >= 30,
            "full_match": all(word in name for word in _words(query["place"])),
        }


class GoogleGeocoder:
    min_interval_s = 0

    def __init__(self, api_key, fetch=_fetch_json):
        self._api_key = api_key
        self._fetch = fetch

    def locate(self, query):
        text = ", ".join(query[key] for key in ("place", "area", "region"))
        params = {"address": text, "components": f"country:{query['country_code']}", "key": self._api_key}
        body = self._fetch(f"https://maps.googleapis.com/maps/api/geocode/json?{urllib.parse.urlencode(params)}")
        status = body.get("status")
        if status == "ZERO_RESULTS":
            return None
        if status != "OK":
            raise GeocodingError(f"Google answered {status}")
        result = body["results"][0]
        kind = result["geometry"]["location_type"]
        on_building = kind == "GEOMETRIC_CENTER" and _GOOGLE_BUILDING_TYPES & set(result.get("types", []))
        return {
            "lat": result["geometry"]["location"]["lat"],
            "lng": result["geometry"]["location"]["lng"],
            "specific": kind in ("ROOFTOP", "RANGE_INTERPOLATED") or bool(on_building),
            "full_match": not result.get("partial_match", False),
        }


class CachedGeocoder:
    """Saves answers so a repeat run makes no new calls, and stops calling during an outage."""

    def __init__(self, provider, name, collection, sleep=time.sleep, clock=time.monotonic):
        self._provider = provider
        self._name = name
        self._collection = collection
        self._sleep = sleep
        self._clock = clock
        self._errors_in_a_row = 0
        self._last_call = None

    def locate(self, query):
        key = {"provider": self._name, "query": query}
        saved = self._collection.find_one(key)
        if saved:
            return saved["result"]
        if self._errors_in_a_row >= _MAX_ERRORS_IN_A_ROW:
            raise GeocodingError("Stopped calling the service after repeated errors")
        self._wait_for_turn()
        try:
            result = self._provider.locate(query)
        except GeocodingError:
            self._errors_in_a_row += 1
            raise
        self._errors_in_a_row = 0
        self._collection.insert_one({**key, "result": result})
        return result

    def _wait_for_turn(self):
        interval = self._provider.min_interval_s
        if self._last_call is not None and interval:
            self._sleep(max(0, interval - (self._clock() - self._last_call)))
        self._last_call = self._clock()


def geocoder_for(settings, collection):
    """The provider a client's settings select. A missing key means no provider, not a failure."""
    name = settings["address"]["verification"]["geocoder"]
    if os.environ.get("DISPATCH_GEOCODER") == "none" or name == "none":
        return NoMapService()
    if name == "google":
        api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
        if not api_key:
            return NoMapService()
        return CachedGeocoder(GoogleGeocoder(api_key), name, collection)
    return CachedGeocoder(NominatimGeocoder(), name, collection)
