import io
import unittest
import zipfile

from app.config import Settings
from app.main import create_app
from app.static_data import DemoStops, StaticStore, parse_route_stops, parse_routes
from app.feed import DemoFeed
from app.systems import Registry, System
from tests.test_feed import Clock, FakeResponse
from tests.test_systems import ZipSession

STOPS = (
    "stop_id,stop_code,stop_name,stop_lat,stop_lon,location_type\n"
    "1,101,One,43.6490,-79.3800,0\n2,102,Two,43.6500,-79.3800,0\n"
    "3,103,Three,43.6510,-79.3800,0\n4,104,Four,43.6520,-79.3800,0\n"
)
ROUTES = "route_id,route_short_name,route_long_name\n504,504,King\n004,004,Zero padded\n7,7,Bathurst\n"
TRIPS = "route_id,trip_id,shape_id,direction_id\n504,t1,,0\n504,t2,,1\n7,t3,,0\n004,t4,,0\n"
TIMES = (
    "trip_id,stop_id,stop_sequence\n"
    "t1,1,1\nt1,2,2\nt2,2,1\nt2,1,2\n"
    "t3,3,1\nt3,4,2\n"
    "t4,4,1\n"
    "nope,1,1\n"
)


def zipped(times=TIMES):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for n, t in (("stops.txt", STOPS), ("routes.txt", ROUTES), ("trips.txt", TRIPS), ("stop_times.txt", times)):
            if t is not None:
                z.writestr(n, t)
    return out.getvalue()


def make_store(times=TIMES):
    session = ZipSession(FakeResponse(200, zipped(times)))
    s = StaticStore("https://a.example/g.zip", Settings(), session=session, clock=Clock(), background=False)
    s.ensure()
    return s


class ReadingRouteStops(unittest.TestCase):
    def test_stops_per_route(self):
        import csv
        trips = csv.DictReader(io.StringIO(TRIPS))
        times = csv.DictReader(io.StringIO(TIMES))
        self.assertEqual(parse_route_stops(trips, times), {"504": {"1", "2"}, "7": {"3", "4"}, "004": {"4"}})

    def test_padded_route_numbers_lose_their_zeros(self):
        routes = parse_routes(csv_rows(ROUTES))
        self.assertEqual(routes["004"]["short"], "4")
        self.assertEqual(routes["504"]["short"], "504")

    def test_store_filters_by_name_or_code(self):
        s = make_store()
        self.assertEqual(s.route_stops_status, "ready")
        self.assertEqual(s.stop_ids_for(["504"]), {"1", "2"})
        self.assertEqual(s.stop_ids_for(["4"]), {"4"})  # the rider's name for route 004
        self.assertEqual(s.stop_ids_for(["504", "7"]), {"1", "2", "3", "4"})
        found, _ = s.stops_in(43, -80, 44, -79, 100, only={"1", "2"})
        self.assertEqual({x["id"] for x in found}, {"1", "2"})

    def test_missing_stop_times_means_no_filter_not_no_stops(self):
        s = make_store(times=None)
        self.assertIsNone(s.stop_ids_for(["504"]))

    def test_demo_routes(self):
        d = DemoStops(DemoFeed(Settings()))
        a, b = d.stop_ids_for(["A"]), d.stop_ids_for(["B"])
        self.assertTrue(b < a)


def csv_rows(text):
    import csv
    return csv.DictReader(io.StringIO(text))


class StopsEndpoint(unittest.TestCase):
    def client(self, store):
        settings = Settings()
        reg = Registry(settings, [System("t", "T", "", (43.65, -79.38), 12, feed_url="https://f.example/x", available=True)], feeds={})
        reg._statics["t"] = store
        return create_app(settings, registry=reg).test_client()

    URL = "/api/stops?system=t&south=43.6&west=-79.4&north=43.7&east=-79.3"

    def ids(self, r):
        return sorted(s["id"] for s in r.get_json()["stops"])

    def test_all_stops_without_routes(self):
        c = self.client(make_store())
        self.assertEqual(self.ids(c.get(self.URL)), ["1", "2", "3", "4"])

    def test_only_the_chosen_routes(self):
        c = self.client(make_store())
        r = c.get(self.URL + "&routes=504")
        self.assertEqual(self.ids(r), ["1", "2"])
        self.assertTrue(r.get_json()["filtered"])
        self.assertEqual(self.ids(c.get(self.URL + "&routes=504,7")), ["1", "2", "3", "4"])

    def test_unknown_route_has_no_stops(self):
        c = self.client(make_store())
        self.assertEqual(self.ids(c.get(self.URL + "&routes=999")), [])

    def test_loading_while_route_stops_unread(self):
        s = make_store()
        s._route_stops_loaded = None
        r = self.client(s).get(self.URL + "&routes=504")
        self.assertEqual(r.get_json()["state"], "loading")

    def test_no_route_data_shows_all_stops(self):
        c = self.client(make_store(times=None))
        r = c.get(self.URL + "&routes=504")
        self.assertEqual(self.ids(r), ["1", "2", "3", "4"])
        self.assertFalse(r.get_json()["filtered"])


if __name__ == "__main__":
    unittest.main()
