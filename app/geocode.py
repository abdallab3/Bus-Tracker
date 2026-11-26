"""Turns a typed address into a map position, using a Nominatim-style search service.

The public OpenStreetMap service has a usage policy this follows: an identifying User-Agent,
no more than one request per second, results kept so the same search is not asked twice, and
a search only when the person submits it (never as they type). The User-Agent never holds a
personal email address. Set GEOCODER_URL to use another service.
"""

import threading
import time
from collections import OrderedDict

import requests

DEFAULT_URL = "https://nominatim.openstreetmap.org/search"
DEFAULT_AGENT = "BusTrackerWebApp/1.0 (+https://github.com/abdallab3/Bus-Tracker)"
MAX_QUERY = 120
BOX_DEGREES = 0.6  # how far around the chosen system's centre to look


class GeocodeError(Exception):
    pass


def clean_query(raw):
    text = " ".join((raw or "").split())
    return text[:MAX_QUERY] if len(text) >= 3 else ""


class Geocoder:
    def __init__(self, url="", agent="", session=None, clock=time.time, sleep=time.sleep,
                 timeout=6.0, cache_size=500, cache_seconds=86400.0, min_gap=1.0):
        self.url = url or DEFAULT_URL
        self.agent = agent or DEFAULT_AGENT
        self.session = session or requests.Session()
        self.clock = clock
        self.sleep = sleep
        self.timeout = timeout
        self.cache_size = cache_size
        self.cache_seconds = cache_seconds
        self.min_gap = min_gap
        self._cache = OrderedDict()
        self._lock = threading.Lock()
        self._last = 0.0

    def search(self, query, center=None):
        """Up to 5 places: [{name, lat, lon}]. Raises GeocodeError if the service cannot answer."""
        query = clean_query(query)
        if not query:
            return []
        key = (query.lower(), None if center is None else (round(center[0], 1), round(center[1], 1)))
        now = self.clock()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < self.cache_seconds:
                self._cache.move_to_end(key)
                return hit[1]
            wait = self.min_gap - (now - self._last)
            if wait > 0:
                self.sleep(wait)  # one request at a time, at most one per second
            self._last = self.clock()
            places = self._ask(query, center)
            self._cache[key] = (self.clock(), places)
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
            return places

    def _ask(self, query, center):
        params = {"q": query, "format": "jsonv2", "limit": 5, "addressdetails": 0}
        if center is not None:
            lat, lon = center
            params["viewbox"] = "%.4f,%.4f,%.4f,%.4f" % (lon - BOX_DEGREES, lat + BOX_DEGREES, lon + BOX_DEGREES, lat - BOX_DEGREES)
            params["bounded"] = 1
        try:
            resp = self.session.get(self.url, params=params, headers={"User-Agent": self.agent, "Accept": "application/json"}, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError):
            raise GeocodeError("The address search is not available right now.")
        if not isinstance(data, list):
            raise GeocodeError("The address search is not available right now.")
        places = []
        for item in data[:5]:
            try:
                lat, lon = float(item["lat"]), float(item["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue
            name = " ".join(str(item.get("display_name") or "").split())[:120]
            places.append({"name": name or "Place", "lat": round(lat, 6), "lon": round(lon, 6)})
        return places
