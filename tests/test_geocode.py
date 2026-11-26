import unittest

import requests

from app.config import Settings
from app.geocode import DEFAULT_AGENT, GeocodeError, Geocoder, clean_query
from app.main import create_app
from app.systems import Registry, System


class FakeResponse:
    def __init__(self, json_data):
        self.status_code = 200
        self._json = json_data

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


class Session:
    def __init__(self, payload=None, fail=False):
        self.payload, self.fail, self.calls = payload, fail, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        if self.fail:
            raise requests.ConnectionError("down")
        return FakeResponse(self.payload)


class Ticker:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


PLACES = [{"lat": "43.65", "lon": "-79.38", "display_name": "100 Queen St W,  Toronto"}, {"lat": "bad", "lon": "1"}, {"lat": "99", "lon": "1"}]


def make(payload=PLACES, fail=False):
    t = Ticker()
    s = Session(payload, fail)
    return Geocoder(session=s, clock=t.now, sleep=t.sleep), s, t


class Geocoding(unittest.TestCase):
    def test_reads_places_and_drops_bad_ones(self):
        g, s, _ = make()
        self.assertEqual(g.search("100 Queen St W", (43.65, -79.38)), [{"name": "100 Queen St W, Toronto", "lat": 43.65, "lon": -79.38}])

    def test_user_agent_is_identifying_and_has_no_email(self):
        g, s, _ = make()
        g.search("city hall", (43.65, -79.38))
        agent = s.calls[0][2]["User-Agent"]
        self.assertEqual(agent, DEFAULT_AGENT)
        self.assertNotIn("@", agent)

    def test_search_is_limited_to_the_system_area(self):
        g, s, _ = make()
        g.search("city hall", (43.65, -79.38))
        self.assertEqual(s.calls[0][1]["bounded"], 1)
        self.assertIn("viewbox", s.calls[0][1])

    def test_same_search_is_cached(self):
        g, s, _ = make()
        g.search("city hall", (43.65, -79.38))
        g.search("  City   Hall ", (43.65, -79.38))
        self.assertEqual(len(s.calls), 1)

    def test_one_request_per_second(self):
        g, s, t = make()
        g.search("city hall", None)
        g.search("union station", None)
        self.assertEqual(len(s.calls), 2)
        self.assertTrue(t.slept and t.slept[0] > 0.9)

    def test_failure_raises(self):
        g, _, _ = make(fail=True)
        with self.assertRaises(GeocodeError):
            g.search("city hall", None)

    def test_short_queries_are_empty(self):
        self.assertEqual(clean_query(" a "), "")
        self.assertEqual(len(clean_query("x" * 500)), 120)


class GeocodeEndpoint(unittest.TestCase):
    def client(self, geocoder, **kw):
        settings = Settings(**kw)
        reg = Registry(settings, [System("t", "T", "", (43.65, -79.38), 12, available=True)], feeds={})
        return create_app(settings, registry=reg, geocoder=geocoder).test_client()

    def test_returns_places(self):
        g, _, _ = make()
        r = self.client(g).get("/api/geocode?system=t&q=city hall")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.get_json()["places"]), 1)

    def test_short_query_is_refused(self):
        g, _, _ = make()
        self.assertEqual(self.client(g).get("/api/geocode?system=t&q=ab").status_code, 400)

    def test_service_down_is_503(self):
        g, _, _ = make(fail=True)
        self.assertEqual(self.client(g).get("/api/geocode?system=t&q=city hall").status_code, 503)

    def test_rate_limited(self):
        g, _, _ = make()
        c = self.client(g, rate_limit_geocode=2)
        codes = [c.get("/api/geocode?system=t&q=place%d" % i).status_code for i in range(3)]
        self.assertEqual(codes, [200, 200, 429])


if __name__ == "__main__":
    unittest.main()
