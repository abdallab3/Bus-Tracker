"""An agency's schedule data (GTFS): where the stops are, and what the routes are called.

The schedule comes as one zip file. It is downloaded in the background the first time someone
asks for it, only two small files are read from it, and nothing is written anywhere permanent.
"""

import csv
import io
import tempfile
import threading
import time
import zipfile

import requests

from .geo import is_valid_position

CHUNK = 65536
MAX_STOP_FILE = 40_000_000  # bytes, after unzipping
MAX_ROUTE_FILE = 5_000_000
MAX_STOPS = 150_000
MAX_ROUTES = 20_000


class StaticError(Exception):
    pass


def _rows(archive, name, limit):
    try:
        info = archive.getinfo(name)
    except KeyError:
        raise StaticError(f"The schedule file has no {name}.")
    if info.file_size > limit:
        raise StaticError(f"The schedule's {name} is larger than this app accepts.")
    text = io.TextIOWrapper(archive.open(info), encoding="utf-8-sig", errors="replace", newline="")
    return csv.DictReader(text)


def parse_stops(rows):
    stops = []
    for row in rows:
        if (row.get("location_type") or "0").strip() not in ("", "0"):
            continue  # stations, entrances and the like are not places to board
        try:
            lat, lon = float(row.get("stop_lat", "")), float(row.get("stop_lon", ""))
        except (TypeError, ValueError):
            continue
        if not is_valid_position(lat, lon) or (lat == 0 and lon == 0):
            continue
        stop_id = (row.get("stop_id") or "").strip()
        if not stop_id:
            continue
        stops.append(
            {
                "id": stop_id[:40],
                "code": (row.get("stop_code") or "").strip()[:20],
                "name": " ".join((row.get("stop_name") or "").split())[:120] or "Bus stop",
                "lat": round(lat, 6),
                "lon": round(lon, 6),
            }
        )
        if len(stops) >= MAX_STOPS:
            raise StaticError("The schedule has more stops than this app accepts.")
    return stops


def parse_routes(rows):
    routes = {}
    for row in rows:
        route_id = (row.get("route_id") or "").strip()
        if not route_id:
            continue
        short = " ".join((row.get("route_short_name") or "").split())[:20]
        long = " ".join((row.get("route_long_name") or "").split())[:80]
        routes[route_id] = {"short": short, "long": long}
        if len(routes) >= MAX_ROUTES:
            break
    return routes


class StaticStore:
    """Stops and route names for one agency, loaded from its GTFS zip."""

    def __init__(self, url, settings, session=None, clock=time.time, background=True):
        self.url = url
        self.s = settings
        self.session = session or requests.Session()
        self.clock = clock
        self.background = background
        self._lock = threading.Lock()
        self._stops = []
        self._routes = {}
        self._loaded = None
        self._failed = None
        self._loading = False
        self.error = None

    @property
    def status(self):
        """off (no schedule address), loading, failed, or ready."""
        if not self.url:
            return "off"
        if self._loaded is not None:
            return "ready"
        if self._failed is not None and not self._loading:
            return "failed"
        return "loading"

    def ensure(self):
        """Start loading if the data is missing or old. Returns at once."""
        if not self.url:
            return
        now = self.clock()
        with self._lock:
            if self._loading:
                return
            if self._loaded is not None and now - self._loaded < self.s.static_refresh_seconds:
                return
            if self._failed is not None and now - self._failed < self.s.static_retry_seconds:
                return
            self._loading = True
        if self.background:
            threading.Thread(target=self._run, daemon=True).start()
        else:
            self._run()

    def _download(self, target):
        resp = self.session.get(self.url, timeout=(8, self.s.static_timeout), stream=True)
        try:
            if resp.status_code != 200:
                raise StaticError(f"The agency's schedule file answered with an error (code {resp.status_code}).")
            total = 0
            for chunk in resp.iter_content(CHUNK):
                total += len(chunk)
                if total > self.s.static_max_bytes:
                    raise StaticError("The agency's schedule file is larger than this app accepts.")
                target.write(chunk)
        finally:
            resp.close()

    def _run(self):
        error = None
        stops, routes = None, None
        try:
            with tempfile.TemporaryFile() as tmp:
                self._download(tmp)
                tmp.seek(0)
                with zipfile.ZipFile(tmp) as archive:
                    stops = parse_stops(_rows(archive, "stops.txt", MAX_STOP_FILE))
                    try:
                        routes = parse_routes(_rows(archive, "routes.txt", MAX_ROUTE_FILE))
                    except StaticError:
                        routes = {}  # route names are a nice extra, the stops are what matters
        except StaticError as e:
            error = str(e)
        except zipfile.BadZipFile:
            error = "The agency's schedule file is not a zip file."
        except requests.RequestException:
            error = "The agency's schedule file could not be reached."
        except Exception:
            error = "The agency's schedule file could not be read."
        now = self.clock()
        with self._lock:
            self._loading = False
            if error is None:
                self._stops, self._routes, self._loaded, self._failed, self.error = stops, routes, now, None, None
            else:
                self._failed, self.error = now, error  # keep older data, if there is any

    def routes(self):
        with self._lock:
            return self._routes

    def stops_in(self, south, west, north, east, limit):
        """Stops inside the box, nearest to its middle first when there are too many."""
        with self._lock:
            stops = self._stops
        inside = [s for s in stops if south <= s["lat"] <= north and west <= s["lon"] <= east]
        truncated = len(inside) > limit
        if truncated:
            mid_lat, mid_lon = (south + north) / 2, (west + east) / 2
            inside.sort(key=lambda s: (s["lat"] - mid_lat) ** 2 + (s["lon"] - mid_lon) ** 2)
            inside = inside[:limit]
        return inside, truncated


class DemoStops:
    """Made-up stops along the demo route, so the stops button has something to show."""

    status = "ready"
    error = None

    def __init__(self, feed, spacing_m=600):
        total = sum(feed._lengths(feed.loop))
        count = max(4, int(total // spacing_m))
        self._stops = []
        for i in range(count):
            lat, lon, _ = feed._along(feed.loop, i * total / count)
            self._stops.append(
                {"id": f"demo-{i + 1}", "code": str(100 + i), "name": f"Demo Stop {i + 1}", "lat": round(lat, 6), "lon": round(lon, 6)}
            )

    def ensure(self):
        pass

    def routes(self):
        return {}

    def stops_in(self, south, west, north, east, limit):
        inside = [s for s in self._stops if south <= s["lat"] <= north and west <= s["lon"] <= east]
        return inside[:limit], len(inside) > limit
