import gzip
import json
import unittest

from app.config import Settings
from app.main import client_ip, create_app, filter_buses
from tests.test_app import StubFeed, agency_bus


class Req:
    def __init__(self, headers=None, remote="10.0.0.9"):
        self.headers = headers or {}
        self.remote_addr = remote


def many(n):
    return [agency_bus(str(i % 7), lat=43.0 + i * 0.001, lon=-79.0, label=f"B{i:04d}") for i in range(n)]


def make(buses, **kw):
    settings = Settings(**kw)
    return create_app(settings, feed=StubFeed(buses)).test_client()


class VisitorAddress(unittest.TestCase):
    def test_a_configured_header_wins_when_it_holds_an_address(self):
        req = Req({"True-Client-IP": "203.0.113.7", "X-Forwarded-For": "1.1.1.1, 2.2.2.2"})
        self.assertEqual(client_ip(req, 1, "True-Client-IP"), "203.0.113.7")

    def test_a_bad_or_missing_header_falls_back_to_the_forwarded_list(self):
        self.assertEqual(client_ip(Req({"True-Client-IP": "not an ip", "X-Forwarded-For": "1.1.1.1, 2.2.2.2"}), 1, "True-Client-IP"), "2.2.2.2")
        self.assertEqual(client_ip(Req({"X-Forwarded-For": "1.1.1.1, 2.2.2.2"}), 2, "True-Client-IP"), "1.1.1.1")

    def test_without_a_header_setting_nothing_changes(self):
        self.assertEqual(client_ip(Req({"True-Client-IP": "203.0.113.7", "X-Forwarded-For": "1.1.1.1"}), 1), "1.1.1.1")
        self.assertEqual(client_ip(Req(), 1), "10.0.0.9")

    def test_ipv6_addresses_are_accepted(self):
        self.assertEqual(client_ip(Req({"CF-Connecting-IP": "2001:db8::1"}), 1, "CF-Connecting-IP"), "2001:db8::1")


class Filtering(unittest.TestCase):
    def test_by_route_and_by_box(self):
        buses = [agency_bus("1", lat=43.0, lon=-79.0), agency_bus("2", lat=43.5, lon=-79.5), agency_bus("1", lat=44.0, lon=-79.0)]
        self.assertEqual(len(filter_buses(buses, ["1"], None)), 2)
        self.assertEqual(len(filter_buses(buses, None, (42.9, -79.1, 43.6, -78.9))), 1)
        self.assertEqual(len(filter_buses(buses, ["1"], (42.9, -79.1, 43.6, -78.9))), 1)
        self.assertEqual(len(filter_buses(buses, None, None)), 3)

    def test_the_api_filters_but_still_lists_every_route(self):
        client = make(many(40))
        full = client.get("/api/buses").get_json()
        part = client.get("/api/buses?routes=1,2").get_json()
        self.assertTrue(all(b["route"] in ("1", "2") for b in part["buses"]))
        self.assertLess(len(part["buses"]), len(full["buses"]))
        self.assertEqual(part["routes"], full["routes"])
        self.assertEqual(part["total"], 40)

    def test_the_api_filters_by_area(self):
        client = make(many(40))
        data = client.get("/api/buses?south=43.0&west=-79.1&north=43.0095&east=-78.9").get_json()
        self.assertEqual(len(data["buses"]), 10)
        self.assertEqual(data["total"], 40)

    def test_bad_filters_are_refused(self):
        client = make(many(3))
        self.assertEqual(client.get("/api/buses?south=1&west=2").status_code, 400)
        self.assertEqual(client.get("/api/buses?south=a&west=2&north=3&east=4").status_code, 400)
        self.assertEqual(client.get("/api/buses?south=5&west=2&north=3&east=4").status_code, 400)
        self.assertEqual(client.get("/api/buses?routes=" + ",".join(str(i) for i in range(21))).status_code, 400)
        self.assertEqual(client.get("/api/buses?routes=" + "x" * 41).status_code, 400)


class Compression(unittest.TestCase):
    def test_big_answers_are_compressed_for_browsers_that_accept_it(self):
        client = make(many(200))
        plain = client.get("/api/buses").data
        packed = client.get("/api/buses", headers={"Accept-Encoding": "gzip, deflate"})
        self.assertEqual(packed.headers["Content-Encoding"], "gzip")
        self.assertEqual(packed.headers["Vary"], "Accept-Encoding")
        self.assertEqual(int(packed.headers["Content-Length"]), len(packed.data))
        self.assertLess(len(packed.data), len(plain) / 3)
        self.assertEqual(json.loads(gzip.decompress(packed.data)), json.loads(plain))

    def test_small_answers_and_other_browsers_get_plain_text(self):
        client = make(many(2))
        self.assertNotIn("Content-Encoding", client.get("/api/buses", headers={"Accept-Encoding": "gzip"}).headers)
        self.assertNotIn("Content-Encoding", make(many(200)).get("/api/buses").headers)
        self.assertNotIn("Content-Encoding", client.get("/", headers={"Accept-Encoding": "gzip"}).headers)


if __name__ == "__main__":
    unittest.main()
