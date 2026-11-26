"""The Bus Tracker web app: a map page, bus positions from the agency's feed, and rider sharing."""

import gzip
import ipaddress
import math
import os
import time
from collections import Counter
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, send_from_directory

from .config import Settings
from .geo import distance_m
from .geocode import GeocodeError, Geocoder, clean_query
from .limiter import RateLimiter
from .riders import RiderError, RiderStore
from .systems import SYSTEM_ID, build_registry

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
MAX_STOP_SPAN = 1.0  # degrees: the widest area stops are looked up for
SAME_BUS_METERS = 120  # a rider this close to an agency bus on the same route is on that bus


def client_ip(req, hops, header=""):
    """The visitor's address. Use the header the host sets just for it, if one is configured (and
    holds a real address). Otherwise it is the entry the nearest trusted proxy added."""
    if header:
        try:
            return str(ipaddress.ip_address(req.headers.get(header, "").strip()))
        except ValueError:
            pass  # missing or not an address: fall back to the forwarded list
    forwarded = [p.strip() for p in req.headers.get("X-Forwarded-For", "").split(",") if p.strip()]
    if hops > 0 and len(forwarded) >= hops:
        return forwarded[-hops]
    return req.remote_addr or "unknown"


def content_security_policy(tile_url):
    tile_host = urlsplit(tile_url.replace("{s}", "a")).netloc
    images = "img-src 'self' data:" + (f" https://{tile_host}" if tile_host else "")
    return (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        f"{images}; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )


def merge_buses(agency_buses, rider_buses):
    """Add rider-reported buses, unless an agency bus on the same route is already there."""
    merged = [dict(b) for b in agency_buses]
    for rb in rider_buses:
        route = rb["route"].strip().lower()
        twin = None
        for ab in merged:
            if ab["source"] != "agency":
                continue
            names = {ab["route"].strip().lower(), ab["label"].strip().lower()} - {""}
            if route in names and distance_m(ab["lat"], ab["lon"], rb["lat"], rb["lon"]) <= SAME_BUS_METERS:
                twin = ab
                break
        if twin:
            twin["riders"] += rb["riders"]
        else:
            merged.append(rb)
    return merged


def apply_route_names(buses, names):
    """Show routes the way riders know them (504, not an internal code), with the full name."""
    out = []
    for b in buses:
        b = dict(b)
        info = names.get(b["route"])
        if info:
            b["route"] = info["short"] or b["route"]
            if info["long"]:
                b["routeName"] = info["long"]
        out.append(b)
    return out


def number_arg(name):
    try:
        value = float(request.args.get(name, ""))
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def filter_buses(buses, routes=None, box=None):
    """Only the buses on the given routes and/or inside the box (south, west, north, east)."""
    out = buses
    if routes:
        wanted = set(routes)
        out = [b for b in out if b["route"] in wanted]
    if box:
        south, west, north, east = box
        out = [b for b in out if south <= b["lat"] <= north and west <= b["lon"] <= east]
    return out


MAX_ROUTE_FILTERS = 20


def route_label(names, route_id):
    """(the number riders know, the full name) for a route code, when the schedule has it."""
    info = names.get(route_id)
    return (info["short"] or route_id, info["long"]) if info else (route_id, "")


MAX_ARRIVALS = 8
ARRIVAL_WINDOW_SECONDS = 6 * 3600
MAX_ALERTS_SENT = 50


def create_app(settings=None, feed=None, riders=None, clock=time.time, environ=None, registry=None, geocoder=None):
    settings = settings or Settings.from_env()
    registry = registry or build_registry(settings, feed=feed, clock=clock, environ=environ)
    riders = riders or RiderStore(settings, clock=clock)
    csp = content_security_policy(settings.tile_url)

    reads = RateLimiter(settings.rate_limit_reads, settings.rate_limit_read_window)
    reports = RateLimiter(settings.rate_limit_reports, settings.rate_limit_report_window)
    starts = RateLimiter(settings.rate_limit_starts, settings.rate_limit_start_window)
    stop_reads = RateLimiter(settings.rate_limit_stops, settings.rate_limit_stops_window)
    line_reads = RateLimiter(settings.rate_limit_stops, settings.rate_limit_stops_window)
    geocode_reads = RateLimiter(settings.rate_limit_geocode, settings.rate_limit_geocode_window)
    geocoder = geocoder or Geocoder(settings.geocoder_url, settings.geocoder_agent)

    app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = 4 * 1024  # every request from the page is tiny

    @app.after_request
    def add_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = csp
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "geolocation=(self), camera=(), microphone=()"
        # Bus lists are big and repeat a lot, so compress them for browsers that accept it.
        if (
            response.mimetype == "application/json"
            and response.status_code == 200
            and not response.direct_passthrough
            and "Content-Encoding" not in response.headers
            and "gzip" in request.headers.get("Accept-Encoding", "").lower()
        ):
            body = response.get_data()
            if len(body) > 1024:
                packed = gzip.compress(body, compresslevel=5)
                response.set_data(packed)
                response.headers["Content-Encoding"] = "gzip"
                response.headers["Content-Length"] = str(len(packed))
                response.headers["Vary"] = "Accept-Encoding"
        return response

    def limited(limiter, message):
        allowed, wait = limiter.allow(client_ip(request, settings.trusted_proxy_hops, settings.client_ip_header))
        if allowed:
            return None
        resp = jsonify(error=message)
        resp.status_code = 429
        resp.headers["Retry-After"] = str(wait)
        return resp

    def body_dict():
        body = request.get_json(silent=True)
        return body if isinstance(body, dict) else None

    def pick_system(raw):
        """(system, None) for a usable system id, or (None, error response). No id means the default."""
        sid = raw if raw else registry.default_id
        system = registry.get(sid) if isinstance(sid, str) and SYSTEM_ID.match(sid) else None
        if system is None:
            return None, (jsonify(error="That transit system is not known."), 404)
        if not system.available:
            return None, (jsonify(error=system.name + " is not connected yet. " + system.note), 404)
        return system, None

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    @app.get("/api/config")
    def config():
        default = registry.get(registry.default_id)
        resp = jsonify(
            mode=registry.feed(default.id).mode,
            agency=default.name,
            center=list(default.center),
            zoom=default.zoom,
            tileUrl=settings.tile_url,
            attribution=settings.tile_attribution,
            refreshSeconds=int(settings.feed_refresh_seconds),
        )
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/api/systems")
    def systems():
        blocked = limited(reads, "The page is asking too often. Please wait a moment.")
        if blocked:
            return blocked
        items = []
        for sid in registry.order:
            s = registry.get(sid)
            items.append(
                {
                    "id": s.id,
                    "name": s.name,
                    "city": s.city,
                    "available": s.available,
                    "note": s.note,
                    "center": list(s.center),
                    "zoom": s.zoom,
                }
            )
        resp = jsonify(systems=items, default=registry.default_id)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/api/buses")
    def buses():
        blocked = limited(reads, "The map is refreshing too often. Please wait a moment.")
        if blocked:
            return blocked
        system, error = pick_system(request.args.get("system"))
        if error:
            return error
        system_feed = registry.feed(system.id)
        agency_buses, status = system_feed.read()
        static = registry.static(system.id)
        static.ensure()
        names = static.routes() if static.status == "ready" else {}
        agency_buses = apply_route_names(agency_buses, names)
        merged = merge_buses(agency_buses, riders.buses(system=system.id))
        wanted_routes = [r.strip() for r in request.args.get("routes", "").split(",") if r.strip()]
        if len(wanted_routes) > MAX_ROUTE_FILTERS or any(len(r) > 40 for r in wanted_routes):
            return jsonify(error="Too many routes to filter by."), 400
        box = None
        if any(n in request.args for n in ("south", "west", "north", "east")):
            values = [number_arg(n) for n in ("south", "west", "north", "east")]
            if any(v is None for v in values) or values[0] > values[2] or values[1] > values[3]:
                return jsonify(error="Send the whole area: south, west, north and east."), 400
            box = tuple(values)
        notes = []
        if system_feed.mode == "demo":
            notes.append("Demo mode: these buses are simulated. No transit agency is connected yet.")
        elif not status["ok"]:
            notes.append(status["error"] + " Showing the last positions it sent, if they are recent.")
        resp = jsonify(
            mode=system_feed.mode,
            system=system.id,
            agency=system.name,
            serverTime=round(time.time()),
            updated=None if status["updated"] is None else round(status["updated"]),
            feedOk=status["ok"],
            buses=filter_buses(merged, wanted_routes, box),
            total=len(merged),
            routeCounts={r: n for r, n in Counter(b["route"] for b in merged if b["route"]).items()},
            routes=sorted({b["route"] for b in merged if b["route"]}, key=str.lower),
            routeNames={b["route"]: b["routeName"] for b in merged if b.get("routeName")},
            notes=notes,
        )
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/api/stops")
    def stops():
        blocked = limited(stop_reads, "Stops are being asked for too often. Please wait a moment.")
        if blocked:
            return blocked
        system, error = pick_system(request.args.get("system"))
        if error:
            return error
        box = [number_arg(n) for n in ("south", "west", "north", "east")]
        if any(v is None for v in box):
            return jsonify(error="Send the area to look in: south, west, north and east."), 400
        south, west, north, east = box
        if not (-90 <= south <= north <= 90 and -180 <= west <= east <= 180):
            return jsonify(error="That area is not valid."), 400
        if north - south > MAX_STOP_SPAN or east - west > MAX_STOP_SPAN:
            return jsonify(error="Zoom in to see stops."), 400
        static = registry.static(system.id)
        static.ensure()
        state, only = static.status, None
        wanted = [r.strip() for r in (request.args.get("routes") or "").split(",") if r.strip()][:MAX_ROUTE_FILTERS]
        if wanted and state == "ready":
            if static.route_stops_status != "ready":
                state = "loading"  # which stops a route serves is still being read
            else:
                only = static.stop_ids_for(wanted)  # None when the schedule cannot say
        found, truncated = static.stops_in(south, west, north, east, settings.max_stops, only) if state == "ready" else ([], False)
        resp = jsonify(state=state, stops=found, truncated=truncated, filtered=only is not None)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/api/geocode")
    def geocode():
        blocked = limited(geocode_reads, "Address searches are happening too often. Please wait a moment.")
        if blocked:
            return blocked
        system, error = pick_system(request.args.get("system"))
        if error:
            return error
        query = clean_query(request.args.get("q"))
        if not query:
            return jsonify(error="Type at least 3 letters of an address."), 400
        try:
            places = geocoder.search(query, system.center)
        except GeocodeError as err:
            return jsonify(error=str(err)), 503
        resp = jsonify(places=places)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/api/route-lines")
    def route_lines():
        blocked = limited(line_reads, "Route lines are being asked for too often. Please wait a moment.")
        if blocked:
            return blocked
        system, error = pick_system(request.args.get("system"))
        if error:
            return error
        route = (request.args.get("route") or "").strip()
        if not route or len(route) > 40:
            return jsonify(error="Send the route to draw."), 400
        static = registry.static(system.id)
        static.ensure()
        state = static.status if static.status != "ready" else static.lines_status
        lines = static.route_lines(route) if state == "ready" else []
        resp = jsonify(state=state, lines=lines)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    arrival_index = {}  # system id -> (feed time, {stop id: [(time, route, delay), ...]})

    def arrivals_by_stop(system_id, updates, stamp):
        cached = arrival_index.get(system_id)
        if cached and cached[0] == stamp:
            return cached[1]
        index = {}
        for update in updates:
            for stop in update["stops"]:
                if stop["time"] is not None:
                    index.setdefault(stop["stop"], []).append((stop["time"], update["route"], stop["delay"]))
        arrival_index[system_id] = (stamp, index)
        return index

    def route_names(system_id):
        static = registry.static(system_id)
        static.ensure()
        return static.routes() if static.status == "ready" else {}

    @app.get("/api/arrivals")
    def arrivals():
        blocked = limited(stop_reads, "Arrival times are being asked for too often. Please wait a moment.")
        if blocked:
            return blocked
        system, error = pick_system(request.args.get("system"))
        if error:
            return error
        stop = (request.args.get("stop") or "").strip()
        if not stop or len(stop) > 40:
            return jsonify(error="Send the stop to look up."), 400
        feed = registry.trips(system.id)
        if feed is None:
            return jsonify(state="off", arrivals=[])
        updates, status = feed.read()
        now = clock()
        names = route_names(system.id)
        found = []
        for when, route, delay in arrivals_by_stop(system.id, updates, status["updated"]).get(stop, []):
            if now - 30 <= when <= now + ARRIVAL_WINDOW_SECONDS:
                short, long = route_label(names, route)
                found.append(
                    {
                        "route": short,
                        "routeName": long,
                        "time": when,
                        "minutes": max(0, round((when - now) / 60)),
                        "delayMinutes": None if delay is None else round(delay / 60),
                    }
                )
        found.sort(key=lambda a: a["time"])
        state = "ok" if status["updated"] is not None else "error"
        resp = jsonify(state=state, arrivals=found[:MAX_ARRIVALS], updated=None if status["updated"] is None else round(status["updated"]))
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/api/alerts")
    def alerts():
        blocked = limited(reads, "Alerts are being asked for too often. Please wait a moment.")
        if blocked:
            return blocked
        system, error = pick_system(request.args.get("system"))
        if error:
            return error
        feed = registry.alerts(system.id)
        if feed is None:
            return jsonify(state="off", alerts=[])
        items, status = feed.read()
        now = clock()
        names = route_names(system.id)
        merged = {}
        for a in items:
            periods = a["periods"]
            if periods and not any(
                (p["start"] is None or p["start"] <= now) and (p["end"] is None or now <= p["end"]) for p in periods
            ):
                continue  # not in effect right now
            key = (a["header"], a["description"])
            entry = merged.setdefault(
                key, {"id": a["id"], "header": a["header"], "description": a["description"], "routes": set(), "effect": a["effect"]}
            )
            entry["routes"].update(route_label(names, r)[0] for r in a["routes"])
        out = []
        for entry in merged.values():
            entry["routes"] = sorted(entry["routes"], key=str.lower)[:20]
            out.append(entry)
            if len(out) >= MAX_ALERTS_SENT:
                break
        state = "ok" if status["updated"] is not None else "error"
        resp = jsonify(state=state, alerts=out)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.post("/api/riders/start")
    def rider_start():
        body = body_dict()
        if body is None:
            return jsonify(error="Send the route you are on."), 400
        system, error = pick_system(body.get("system") if isinstance(body.get("system"), str) else "")
        if error:
            return error
        blocked = limited(starts, "You have started sharing a lot in a short time. Please wait a little.")
        if blocked:
            return blocked
        try:
            sid = riders.start(body.get("route"), client_ip(request, settings.trusted_proxy_hops, settings.client_ip_header), system.id)
        except RiderError as e:
            return jsonify(error=e.message), e.status
        return jsonify(session=sid, minSeconds=settings.rider_min_interval)

    @app.post("/api/riders/report")
    def rider_report():
        body = body_dict()
        if body is None:
            return jsonify(error="Send your position."), 400
        blocked = limited(reports, "Too many position reports. Please slow down.")
        if blocked:
            return blocked
        try:
            result = riders.report(body.get("session"), body.get("lat"), body.get("lon"), body.get("accuracy"))
        except RiderError as e:
            return jsonify(error=e.message), e.status
        return jsonify(result=result)

    @app.post("/api/riders/stop")
    def rider_stop():
        body = body_dict()
        if body is not None and isinstance(body.get("session"), str):
            riders.stop(body["session"])
        return jsonify(result="stopped")

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8000")), debug=False)
