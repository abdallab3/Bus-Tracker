"""The transit systems a visitor can choose from (TTC, MiWay, and so on).

A system needs a live vehicle feed address to be available. Systems that have no public feed
address yet are still listed, with the reason, so people can see what is planned. An address can
be added without changing the code, with environment variables named after the system:

    SYSTEM_YRT_FEED_URL, SYSTEM_YRT_STOPS_URL, SYSTEM_YRT_FEED_HEADERS
"""

import json
import os
import re
import time
from dataclasses import dataclass, replace

from .feed import AgencyFeed, DemoFeed
from .realtime import alert_feed, trip_update_feed
from .static_data import DemoStops, StaticStore

SYSTEM_ID = re.compile(r"^[a-z0-9_-]{1,20}$")


@dataclass(frozen=True)
class System:
    id: str
    name: str
    city: str
    center: tuple
    zoom: float
    feed_url: str = ""  # live vehicle positions (GTFS-Realtime)
    stops_url: str = ""  # the schedule zip (GTFS), for stops and route names
    trips_url: str = ""  # trip updates (GTFS-Realtime), for next arrivals
    alerts_url: str = ""  # service alerts (GTFS-Realtime)
    headers: tuple = ()
    available: bool = False
    note: str = ""  # why it is not available yet


BUILTIN = (
    System(
        id="ttc",
        name="TTC",
        city="Toronto",
        center=(43.6532, -79.3832),
        zoom=11,
        feed_url="https://bustime.ttc.ca/gtfsrt/vehicles",
        stops_url="http://opendata.toronto.ca/toronto.transit.commission/ttc-routes-and-schedules/OpenData_TTC_Schedules.zip",
        trips_url="https://bustime.ttc.ca/gtfsrt/trips",
        alerts_url="https://bustime.ttc.ca/gtfsrt/alerts",
    ),
    System(
        id="miway",
        name="MiWay",
        city="Mississauga",
        center=(43.5890, -79.6441),
        zoom=12,
        feed_url="https://www.miapp.ca/GTFS_RT/Vehicle/VehiclePositions.pb",
        stops_url="https://www.miapp.ca/GTFS/google_transit.zip",
    ),
    System(
        id="yrt",
        name="YRT",
        city="York Region (Vaughan, Markham, Richmond Hill)",
        center=(43.8561, -79.3370),
        zoom=11,
        note="York Region Transit shares its live data after a request form is submitted.",
    ),
    System(
        id="brampton",
        name="Brampton Transit",
        city="Brampton",
        center=(43.7315, -79.7624),
        zoom=12,
        note="No public live bus feed has been found for Brampton Transit yet.",
    ),
    System(
        id="go",
        name="GO Transit",
        city="Greater Toronto and Hamilton",
        center=(43.6532, -79.3832),
        zoom=9,
        note="GO Transit's live data needs a free Metrolinx access key.",
    ),
)


def _web_address(raw):
    raw = (raw or "").strip()
    return raw if raw.lower().startswith(("http://", "https://")) else ""


def _headers(raw):
    try:
        data = json.loads(raw or "")
    except ValueError:
        return ()
    if not isinstance(data, dict):
        return ()
    return tuple((str(k), str(v)) for k, v in data.items())


def build_systems(settings, environ=None):
    """The list of systems: the built-in ones (when switched on), plus a FEED_URL system if set."""
    environ = os.environ if environ is None else environ
    systems = []
    if settings.use_builtin_systems:
        for base in BUILTIN:
            key = "SYSTEM_" + base.id.upper() + "_"
            feed = _web_address(environ.get(key + "FEED_URL")) or base.feed_url
            stops = _web_address(environ.get(key + "STOPS_URL")) or base.stops_url
            headers = _headers(environ.get(key + "FEED_HEADERS")) or base.headers
            trips = _web_address(environ.get(key + "TRIPS_URL")) or base.trips_url
            alerts = _web_address(environ.get(key + "ALERTS_URL")) or base.alerts_url
            systems.append(
                replace(
                    base, feed_url=feed, stops_url=stops, trips_url=trips, alerts_url=alerts, headers=headers,
                    available=bool(feed), note="" if feed else base.note,
                )
            )
    legacy = _web_address(settings.feed_url)
    if legacy and all(s.feed_url != legacy for s in systems):
        systems.append(
            System(
                id="custom",
                name=settings.agency_name or "Transit agency",
                city="",
                center=(settings.center_lat, settings.center_lon),
                zoom=settings.zoom,
                feed_url=legacy,
                stops_url=_web_address(environ.get("STOPS_URL")),
                trips_url=_web_address(environ.get("TRIPS_URL")),
                alerts_url=_web_address(environ.get("ALERTS_URL")),
                headers=settings.feed_headers,
                available=True,
            )
        )
    return systems


class Registry:
    """Knows each system and creates its feed and schedule data when first needed."""

    def __init__(self, settings, systems, feeds=None, clock=time.time):
        self.settings = settings
        self.clock = clock
        self.systems = {s.id: s for s in systems}
        self.order = [s.id for s in systems]
        self._feeds = dict(feeds or {})
        self._statics = {}
        self._extras = {}

    @property
    def default_id(self):
        for sid in self.order:
            if self.systems[sid].available:
                return sid
        return self.order[0]

    def get(self, sid):
        return self.systems.get(sid)

    def feed(self, sid):
        if sid not in self._feeds:
            s = self.systems[sid]
            self._feeds[sid] = AgencyFeed(
                replace(self.settings, feed_url=s.feed_url, feed_headers=s.headers), clock=self.clock
            )
        return self._feeds[sid]

    def _extra(self, sid, kind):
        key = (sid, kind)
        if key not in self._extras:
            s = self.systems[sid]
            url = s.trips_url if kind == "trips" else s.alerts_url
            if not url:
                self._extras[key] = None
            else:
                make = trip_update_feed if kind == "trips" else alert_feed
                self._extras[key] = make(url, s.headers, self.settings, clock=self.clock)
        return self._extras[key]

    def trips(self, sid):
        """The trip updates feed for a system, or None if it has none."""
        return self._extra(sid, "trips")

    def alerts(self, sid):
        """The service alerts feed for a system, or None if it has none."""
        return self._extra(sid, "alerts")

    def static(self, sid):
        if sid not in self._statics:
            feed = self.feed(sid)
            if feed.mode == "demo" and hasattr(feed, "loop"):
                self._statics[sid] = DemoStops(feed)
            else:
                self._statics[sid] = StaticStore(self.systems[sid].stops_url, self.settings, clock=self.clock)
        return self._statics[sid]


def build_registry(settings, feed=None, clock=time.time, environ=None):
    if feed is not None:  # a feed handed in directly (used by the tests)
        system = System(
            id="default",
            name=settings.agency_name,
            city="",
            center=(settings.center_lat, settings.center_lon),
            zoom=14.0 if feed.mode == "demo" else settings.zoom,
            available=True,
        )
        return Registry(settings, [system], feeds={"default": feed}, clock=clock)
    systems = build_systems(settings, environ)
    if not any(s.available for s in systems):
        demo = System(
            id="demo",
            name=settings.agency_name or "Demo",
            city="Simulated buses",
            center=(settings.center_lat, settings.center_lon),
            zoom=14.0,
            available=True,
        )
        return Registry(settings, [demo], feeds={"demo": DemoFeed(settings, clock=clock)}, clock=clock)
    return Registry(settings, systems, clock=clock)
