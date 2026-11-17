import io
import unittest
import zipfile

import requests

from app.config import Settings
from app.feed import DemoFeed
from app.main import apply_route_names, create_app
from app.static_data import DemoStops, StaticStore, parse_routes, parse_stops
from app.systems import Registry, System, build_registry, build_systems
from tests.test_app import StubFeed, agency_bus
from tests.test_feed import Clock, FakeResponse, FakeSession

STOPS = (
    "stop_id,stop_code,stop_name,stop_lat,stop_lon,location_type\n"
    "1,101,King at Bay,43.6490,-79.3800,0\n"
    "2,102,  Queen   at Yonge ,43.6520,-79.3790,\n"
    "3,,Union Station,43.6453,-79.3806,1\n"  # a station, not a stop
    "4,104,Broken,north,-79.38,0\n"
    "5,105,Far away,44.50,-79.38,0\n"
)
ROUTES = "route_id,route_short_name,route_long_name\n504,504,King\n7,,Bathurst Local\n"


def make_zip(stops=STOPS, routes=ROUTES):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        if stops is not None:
            z.writestr("stops.txt", stops)
        if routes is not None:
            z.writestr("routes.txt", routes)
    return out.getvalue()


class ZipSession(FakeSession):
    def get(self, url, headers=None, timeout=None, stream=False):
        self.calls.append({"url": url, "timeout": timeout})
        item = self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


def store(content=None, status=200, **kw):
    clock = kw.pop("clock", Clock())
    session = ZipSession(FakeResponse(status, make_zip() if content is None else content))
    s = StaticStore("https://agency.example/gtfs.zip", Settings(**kw), session=session, clock=clock, background=False)
    return s, session, clock


class ParsingSchedules(unittest.TestCase):
    def test_stops_keep_only_places_to_board_with_good_positions(self):
        stops = parse_stops(__import__("csv").DictReader(io.StringIO(STOPS)))
        self.assertEqual([s["id"] for s in stops], ["1", "2", "5"])
        self.assertEqual(stops[1]["name"], "Queen at Yonge")

    def test_route_names_are_read(self):
        routes = parse_routes(__import__("csv").DictReader(io.StringIO(ROUTES)))
        self.assertEqual(routes["504"], {"short": "504", "long": "King"})
        self.assertEqual(routes["7"], {"short": "", "long": "Bathurst Local"})


class StaticStoreTests(unittest.TestCase):
    def test_a_system_with_no_schedule_address_has_no_stops(self):
        s = StaticStore("", Settings(), background=False)
        s.ensure()
        self.assertEqual(s.status, "off")
        self.assertEqual(s.stops_in(-90, -180, 90, 180, 10), ([], False))

    def test_loading_stops_and_routes(self):
        s, session, _ = store()
        self.assertEqual(s.status, "loading")
        s.ensure()
        self.assertEqual(s.status, "ready")
        found, truncated = s.stops_in(43.60, -79.40, 43.70, -79.30, 10)
        self.assertEqual([x["id"] for x in found], ["1", "2"])
        self.assertFalse(truncated)
        self.assertEqual(s.routes()["504"]["long"], "King")

    def test_too_many_stops_in_view_keeps_the_ones_nearest_the_middle(self):
        s, _, _ = store()
        s.ensure()
        found, truncated = s.stops_in(43.60, -79.40, 43.70, -79.30, 1)
        self.assertTrue(truncated)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["id"], "2")  # closest to 43.65, -79.35

    def test_it_is_not_downloaded_again_until_it_is_old(self):
        s, session, clock = store()
        s.ensure()
        s.ensure()
        self.assertEqual(len(session.calls), 1)
        clock.now += s.s.static_refresh_seconds + 1
        s.ensure()
        self.assertEqual(len(session.calls), 2)

    def test_a_failed_download_is_retried_later_not_every_request(self):
        s, session, clock = store(status=503)
        s.ensure()
        self.assertEqual(s.status, "failed")
        self.assertIn("error", s.error)
        s.ensure()
        self.assertEqual(len(session.calls), 1)
        clock.now += s.s.static_retry_seconds + 1
        s.ensure()
        self.assertEqual(len(session.calls), 2)

    def test_old_data_is_kept_when_a_refresh_fails(self):
        s, session, clock = store()
        s.ensure()
        session.responses = [FakeResponse(500, b"")]
        clock.now += s.s.static_refresh_seconds + 1
        s.ensure()
        self.assertEqual(s.status, "ready")
        self.assertEqual(len(s.stops_in(43.60, -79.40, 43.70, -79.30, 10)[0]), 2)

    def test_bad_files_are_explained(self):
        s, _, _ = store(content=b"this is not a zip")
        s.ensure()
        self.assertEqual((s.status, s.error), ("failed", "The agency's schedule file is not a zip file."))
        s, _, _ = store(content=make_zip(stops=None))
        s.ensure()
        self.assertIn("no stops.txt", s.error)

    def test_a_missing_routes_file_still_gives_stops(self):
        s, _, _ = store(content=make_zip(routes=None))
        s.ensure()
        self.assertEqual(s.status, "ready")
        self.assertEqual(s.routes(), {})

    def test_an_oversized_download_is_refused(self):
        s, _, _ = store(static_max_bytes=100)
        s.ensure()
        self.assertEqual(s.status, "failed")
        self.assertIn("larger than this app accepts", s.error)

    def test_an_unreachable_agency_is_explained(self):
        clock = Clock()
        session = ZipSession(requests.ConnectionError("down"))
        s = StaticStore("https://x.example/a.zip", Settings(), session=session, clock=clock, background=False)
        s.ensure()
        self.assertEqual(s.error, "The agency's schedule file could not be reached.")


class DemoStopsTests(unittest.TestCase):
    def test_the_demo_has_stops_along_its_route(self):
        stops = DemoStops(DemoFeed(Settings()))
        found, _ = stops.stops_in(-90, -180, 90, 180, 500)
        self.assertGreaterEqual(len(found), 4)
        self.assertEqual(stops.status, "ready")


class TheSystemList(unittest.TestCase):
    def test_built_in_systems_are_listed_with_reasons_when_not_available(self):
        systems = {s.id: s for s in build_systems(Settings(use_builtin_systems=True), environ={})}
        self.assertTrue(systems["ttc"].available and systems["miway"].available)
        for sid in ("yrt", "brampton", "go"):
            self.assertFalse(systems[sid].available)
            self.assertTrue(systems[sid].note)

    def test_an_address_in_the_environment_makes_a_system_available(self):
        env = {"SYSTEM_YRT_FEED_URL": "https://yrt.example/vp", "SYSTEM_YRT_FEED_HEADERS": '{"x-api-key": "k"}'}
        systems = {s.id: s for s in build_systems(Settings(use_builtin_systems=True), environ=env)}
        self.assertTrue(systems["yrt"].available)
        self.assertEqual(systems["yrt"].headers, (("x-api-key", "k"),))
        self.assertEqual(systems["yrt"].note, "")

    def test_only_web_addresses_are_accepted(self):
        env = {"SYSTEM_YRT_FEED_URL": "file:///etc/passwd", "SYSTEM_TTC_STOPS_URL": "ftp://x"}
        systems = {s.id: s for s in build_systems(Settings(use_builtin_systems=True), environ=env)}
        self.assertFalse(systems["yrt"].available)
        self.assertTrue(systems["ttc"].stops_url.startswith("http"))

    def test_the_old_feed_url_setting_is_kept_as_its_own_system_unless_it_is_a_built_in_one(self):
        settings = Settings(use_builtin_systems=True, feed_url="https://other.example/rt", agency_name="Other")
        ids = [s.id for s in build_systems(settings, environ={})]
        self.assertIn("custom", ids)
        same = Settings(use_builtin_systems=True, feed_url="https://bustime.ttc.ca/gtfsrt/vehicles")
        self.assertNotIn("custom", [s.id for s in build_systems(same, environ={})])

    def test_with_nothing_connected_the_app_runs_a_demo(self):
        registry = build_registry(Settings(), environ={})
        self.assertEqual(registry.order, ["demo"])
        self.assertEqual(registry.feed("demo").mode, "demo")
        self.assertEqual(registry.static("demo").status, "ready")


def two_system_app(**kw):
    clock = Clock(1_700_000_000.0)
    settings = Settings(**kw)
    systems = [
        System("ttc", "TTC", "Toronto", (43.65, -79.38), 11, feed_url="https://x/ttc", available=True),
        System("miway", "MiWay", "Mississauga", (43.59, -79.64), 12, feed_url="https://x/miway", available=True),
        System("yrt", "YRT", "York", (43.85, -79.33), 11, note="Needs a form.", available=False),
    ]
    feeds = {
        "ttc": StubFeed([agency_bus("504", lat=43.65, lon=-79.38, label="4001"), agency_bus("7", lat=43.70, lon=-79.40, label="4002")]),
        "miway": StubFeed([agency_bus("101", lat=43.59, lon=-79.64, label="9001")]),
    }
    registry = Registry(settings, systems, feeds=feeds, clock=clock)
    s, _, _ = store()
    registry._statics["ttc"] = s
    registry._statics["miway"] = StaticStore("", settings, background=False)
    app = create_app(settings, registry=registry, clock=clock)
    return app.test_client(), registry, clock


class SystemsApi(unittest.TestCase):
    def test_the_page_can_list_the_systems(self):
        client, _, _ = two_system_app()
        data = client.get("/api/systems").get_json()
        self.assertEqual([s["id"] for s in data["systems"]], ["ttc", "miway", "yrt"])
        self.assertEqual(data["default"], "ttc")
        self.assertFalse(data["systems"][2]["available"])
        self.assertEqual(data["systems"][2]["note"], "Needs a form.")

    def test_each_system_shows_only_its_own_buses(self):
        client, _, _ = two_system_app()
        ttc = client.get("/api/buses?system=ttc").get_json()
        miway = client.get("/api/buses?system=miway").get_json()
        self.assertEqual({b["label"] for b in ttc["buses"]}, {"4001", "4002"})
        self.assertEqual({b["label"] for b in miway["buses"]}, {"9001"})
        self.assertEqual((ttc["system"], ttc["agency"]), ("ttc", "TTC"))

    def test_no_system_means_the_first_available_one(self):
        client, _, _ = two_system_app()
        self.assertEqual(client.get("/api/buses").get_json()["system"], "ttc")

    def test_unknown_and_unavailable_systems_get_clear_answers(self):
        client, _, _ = two_system_app()
        self.assertEqual(client.get("/api/buses?system=nope").status_code, 404)
        self.assertEqual(client.get("/api/buses?system=../etc").status_code, 404)
        r = client.get("/api/buses?system=yrt")
        self.assertEqual(r.status_code, 404)
        self.assertIn("not connected yet", r.get_json()["error"])
        self.assertIn("Needs a form.", r.get_json()["error"])

    def test_route_names_come_from_the_schedule(self):
        client, registry, _ = two_system_app()
        registry.static("ttc").ensure()
        data = client.get("/api/buses?system=ttc").get_json()
        self.assertEqual(data["routeNames"], {"504": "King", "7": "Bathurst Local"})
        names = {b["label"]: b["route"] for b in data["buses"]}
        self.assertEqual(names, {"4001": "504", "4002": "7"})  # route 7 has no short name, so its code stays

    def test_route_names_are_applied_without_changing_the_original_list(self):
        original = [agency_bus("504")]
        out = apply_route_names(original, {"504": {"short": "504A", "long": "King"}})
        self.assertEqual((out[0]["route"], out[0]["routeName"]), ("504A", "King"))
        self.assertEqual(original[0]["route"], "504")

    def test_riders_belong_to_the_system_they_chose(self):
        client, _, clock = two_system_app()
        sid = client.post("/api/riders/start", json={"route": "99", "system": "miway"}).get_json()["session"]
        for i in range(3):
            client.post("/api/riders/report", json={"session": sid, "lat": 43.59 + i * 0.001, "lon": -79.64, "accuracy": 8})
            clock.now += 4
        self.assertEqual([b["route"] for b in client.get("/api/buses?system=miway").get_json()["buses"]], ["101", "99"])
        self.assertEqual({b["route"] for b in client.get("/api/buses?system=ttc").get_json()["buses"]}, {"504", "7"})

    def test_a_rider_cannot_start_on_an_unavailable_system(self):
        client, _, _ = two_system_app()
        self.assertEqual(client.post("/api/riders/start", json={"route": "9", "system": "yrt"}).status_code, 404)
        self.assertEqual(client.post("/api/riders/start", json={"route": "9", "system": "zzz"}).status_code, 404)


class StopsApi(unittest.TestCase):
    AREA = "south=43.60&west=-79.40&north=43.70&east=-79.30"

    def test_stops_inside_the_area_are_returned(self):
        client, registry, _ = two_system_app()
        registry.static("ttc").ensure()
        data = client.get("/api/stops?system=ttc&" + self.AREA).get_json()
        self.assertEqual(data["state"], "ready")
        self.assertEqual([s["name"] for s in data["stops"]], ["King at Bay", "Queen at Yonge"])
        self.assertFalse(data["truncated"])

    def test_a_system_with_no_schedule_says_so(self):
        client, _, _ = two_system_app()
        data = client.get("/api/stops?system=miway&" + self.AREA).get_json()
        self.assertEqual((data["state"], data["stops"]), ("off", []))

    def test_bad_areas_are_refused(self):
        client, _, _ = two_system_app()
        self.assertEqual(client.get("/api/stops?system=ttc").status_code, 400)
        self.assertEqual(client.get("/api/stops?system=ttc&south=a&west=1&north=2&east=3").status_code, 400)
        self.assertEqual(client.get("/api/stops?system=ttc&south=50&west=-79&north=40&east=-78").status_code, 400)
        wide = client.get("/api/stops?system=ttc&south=40&west=-80&north=45&east=-70")
        self.assertEqual(wide.status_code, 400)
        self.assertIn("Zoom in", wide.get_json()["error"])
        nan = client.get("/api/stops?system=ttc&south=nan&west=-79&north=44&east=-78")
        self.assertEqual(nan.status_code, 400)

    def test_stop_lookups_are_rate_limited(self):
        client, _, _ = two_system_app(rate_limit_stops=2)
        codes = [client.get("/api/stops?system=ttc&" + self.AREA).status_code for _ in range(4)]
        self.assertEqual(codes, [200, 200, 429, 429])


if __name__ == "__main__":
    unittest.main()
