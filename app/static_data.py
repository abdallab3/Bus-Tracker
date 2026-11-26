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

from .geo import distance_m, is_valid_position

CHUNK = 65536
MAX_STOP_FILE = 40_000_000  # bytes, after unzipping
MAX_ROUTE_FILE = 5_000_000
MAX_STOPS = 150_000
MAX_ROUTES = 20_000
MAX_TRIP_FILE = 250_000_000
MAX_SHAPE_FILE = 400_000_000
MAX_STOP_TIMES_FILE = 900_000_000
THIN_METERS = 12  # route lines drop points closer together than this
MAX_LINE_POINTS = 6000  # most points sent for one route


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
        if short.isdigit() and short.strip("0"):
            short = short.lstrip("0")  # some agencies pad numbers (YRT's 004): riders say 4
        long = " ".join((row.get("route_long_name") or "").split())[:80]
        routes[route_id] = {"short": short, "long": long}
        if len(routes) >= MAX_ROUTES:
            break
    return routes


def parse_trip_shapes(rows):
    """For each route, the shape most trips follow in each direction: {route_id: [shape_id, ...]}."""
    counts = {}
    for row in rows:
        route_id = (row.get("route_id") or "").strip()
        shape_id = (row.get("shape_id") or "").strip()
        if not route_id or not shape_id:
            continue
        group = counts.setdefault((route_id, (row.get("direction_id") or "").strip()), {})
        group[shape_id] = group.get(shape_id, 0) + 1
    chosen = {}
    for (route_id, direction), group in counts.items():
        ranked = sorted(group, key=lambda sid: (-group[sid], sid))
        chosen.setdefault(route_id, []).extend(ranked[: 1 if direction else 2])
    return {route_id: list(dict.fromkeys(ids)) for route_id, ids in chosen.items()}


def parse_route_stops(trip_rows, time_rows):
    """Which stops each route serves: {route_id: set of stop ids}, from trips and stop_times."""
    trip_route = {}
    for row in trip_rows:
        trip_id = (row.get("trip_id") or "").strip()
        route_id = (row.get("route_id") or "").strip()
        if trip_id and route_id:
            trip_route[trip_id] = route_id
    served = {}
    last_trip, last_set = None, None
    for row in time_rows:
        trip_id = row.get("trip_id")
        if trip_id != last_trip:  # stop_times lists one trip's rows together
            last_trip = trip_id
            route_id = trip_route.get((trip_id or "").strip())
            last_set = served.setdefault(route_id, set()) if route_id else None
        if last_set is not None:
            stop_id = (row.get("stop_id") or "").strip()
            if stop_id:
                last_set.add(stop_id[:40])
    return served


def _thin(points):
    kept = [points[0]]
    for point in points[1:-1]:
        if distance_m(kept[-1][0], kept[-1][1], point[0], point[1]) >= THIN_METERS:
            kept.append(point)
    kept.append(points[-1])
    return kept


def parse_shapes(rows, wanted):
    """The paths of the wanted shapes, in order, with points that are too close together dropped."""
    found = {}
    for row in rows:
        shape_id = (row.get("shape_id") or "").strip()
        if shape_id not in wanted:
            continue
        try:
            seq = float(row.get("shape_pt_sequence", ""))
            lat, lon = float(row.get("shape_pt_lat", "")), float(row.get("shape_pt_lon", ""))
        except (TypeError, ValueError):
            continue
        if is_valid_position(lat, lon):
            found.setdefault(shape_id, []).append((seq, lat, lon))
    lines = {}
    for shape_id, points in found.items():
        points.sort()
        if len(points) >= 2:
            lines[shape_id] = [[round(lat, 5), round(lon, 5)] for lat, lon in _thin([(la, lo) for _, la, lo in points])]
    return lines


def _fit(lines, limit=MAX_LINE_POINTS):
    """Keep every line but thin them evenly if together they have too many points."""
    total = sum(len(line) for line in lines)
    if total <= limit:
        return lines
    step = -(-total // limit)
    return [line[::step] + ([line[-1]] if (len(line) - 1) % step else []) for line in lines]


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
        self._route_lines = {}
        self._lines_loaded = None
        self._route_stops = {}
        self._route_stops_loaded = None
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

    @property
    def lines_status(self):
        """Route lines load after the stops: off, loading or ready (ready can still have no lines)."""
        if not self.url:
            return "off"
        return "ready" if self._lines_loaded is not None else "loading"

    @property
    def route_stops_status(self):
        """Which stops each route serves load last: off, loading or ready."""
        if not self.url:
            return "off"
        return "ready" if self._route_stops_loaded is not None else "loading"

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

    def _load_lines(self, archive):
        """Route paths are a nice extra: if they cannot be read, the stops and names still work."""
        lines = {}
        try:
            by_route = parse_trip_shapes(_rows(archive, "trips.txt", MAX_TRIP_FILE))
            wanted = {sid for ids in by_route.values() for sid in ids}
            shapes = parse_shapes(_rows(archive, "shapes.txt", MAX_SHAPE_FILE), wanted)
            for route_id, ids in by_route.items():
                got = [shapes[sid] for sid in ids if sid in shapes]
                if got:
                    lines[route_id] = got
        except Exception:
            lines = {}
        with self._lock:
            self._route_lines = lines
            self._lines_loaded = self.clock()

    def _load_route_stops(self, archive):
        """Which stops a route serves is an extra too: without it the stops button shows every stop."""
        served = {}
        try:
            served = parse_route_stops(
                _rows(archive, "trips.txt", MAX_TRIP_FILE), _rows(archive, "stop_times.txt", MAX_STOP_TIMES_FILE)
            )
        except Exception:
            served = {}
        with self._lock:
            self._route_stops = served
            self._route_stops_loaded = self.clock()

    def _run(self):
        error = None
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
                    with self._lock:  # stops are usable now; route paths follow
                        self._stops, self._routes, self._loaded = stops, routes, self.clock()
                        self._failed, self.error = None, None
                    self._load_lines(archive)
                    self._load_route_stops(archive)
        except StaticError as e:
            error = str(e)
        except zipfile.BadZipFile:
            error = "The agency's schedule file is not a zip file."
        except requests.RequestException:
            error = "The agency's schedule file could not be reached."
        except Exception:
            error = "The agency's schedule file could not be read."
        with self._lock:
            self._loading = False
            if error is not None:
                self._failed, self.error = self.clock(), error  # keep older data, if there is any

    def routes(self):
        with self._lock:
            return self._routes

    def route_lines(self, route):
        """The paths of a route, named the way riders know it (504) or by its code: lists of [lat, lon]."""
        with self._lock:
            routes, lines = self._routes, self._route_lines
        ids = [rid for rid, info in routes.items() if rid == route or info["short"] == route]
        if route in lines and route not in ids:
            ids.append(route)
        found = []
        for rid in ids:
            found.extend(lines.get(rid, []))
        return _fit(found)

    def stop_ids_for(self, routes):
        """The ids of stops served by any of the routes (named like 504 or by code), or None if unknown."""
        with self._lock:
            names, served = self._routes, self._route_stops
        if not served:
            return None
        ids = set()
        for route in routes:
            for rid in {r for r, info in names.items() if r == route or info["short"] == route} | {route}:
                ids |= served.get(rid, set())
        return ids

    def stops_in(self, south, west, north, east, limit, only=None):
        """Stops inside the box, nearest to its middle first when there are too many.

        `only` is an optional set of stop ids: nothing else is returned."""
        with self._lock:
            stops = self._stops
        inside = [
            s for s in stops
            if south <= s["lat"] <= north and west <= s["lon"] <= east and (only is None or s["id"] in only)
        ]
        truncated = len(inside) > limit
        if truncated:
            mid_lat, mid_lon = (south + north) / 2, (west + east) / 2
            inside.sort(key=lambda s: (s["lat"] - mid_lat) ** 2 + (s["lon"] - mid_lon) ** 2)
            inside = inside[:limit]
        return inside, truncated


class DemoStops:
    """Made-up stops along the demo route, so the stops button has something to show."""

    status = "ready"
    lines_status = "ready"
    error = None

    def __init__(self, feed, spacing_m=600):
        self._loop = [[round(a, 5), round(b, 5)] for a, b in feed.loop]
        self._line = [[round(a, 5), round(b, 5)] for a, b in feed.line]
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

    def route_lines(self, route):
        return {"A": [self._loop], "B": [self._line]}.get(route, [])

    route_stops_status = "ready"

    def stop_ids_for(self, routes):
        # The demo's two routes share one loop; route B serves every other stop.
        ids = set()
        for route in routes:
            if route == "A":
                ids |= {s["id"] for s in self._stops}
            elif route == "B":
                ids |= {s["id"] for i, s in enumerate(self._stops) if i % 2 == 0}
        return ids

    def stops_in(self, south, west, north, east, limit, only=None):
        inside = [
            s for s in self._stops
            if south <= s["lat"] <= north and west <= s["lon"] <= east and (only is None or s["id"] in only)
        ]
        return inside[:limit], len(inside) > limit
