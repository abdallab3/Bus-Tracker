import unittest

from app.config import Settings
from app.gtfsrt import FeedError, parse_alerts, parse_trip_updates
from app.main import create_app
from app.realtime import SnapshotFeed, alert_feed, trip_update_feed
from app.static_data import StaticStore
from app.systems import Registry, System
from tests import pbhelp as pb
from tests.test_feed import Clock, FakeResponse, FakeSession
from tests.test_systems import ZipSession, make_zip

NOW = 1_700_000_000


class ReadingTripUpdates(unittest.TestCase):
    def test_arrival_times_delays_and_skipped_stops(self):
        data = pb.feed(
            [
                pb.trip_update_entity(
                    "t1",
                    "504",
                    "trip-1",
                    [
                        pb.stop_time_update("S1", time=NOW + 300, delay=-60, seq=1),
                        pb.stop_time_update("S2", time=NOW + 600, seq=2, skipped=True),
                        pb.stop_time_update("S3", time=NOW + 900, seq=3, departure=True),
                        pb.stop_time_update("S4", delay=120, seq=4),  # a delay but no time
                    ],
                ),
                pb.trip_update_entity("t2", "7", "trip-2", [pb.stop_time_update("S1", time=NOW + 60)], deleted=True),
            ],
            timestamp=NOW,
        )
        feed_time, updates = parse_trip_updates(data)
        self.assertEqual(feed_time, NOW)
        self.assertEqual(len(updates), 1)
        stops = {s["stop"]: s for s in updates[0]["stops"]}
        self.assertEqual(updates[0]["route"], "504")
        self.assertEqual(set(stops), {"S1", "S3", "S4"})
        self.assertEqual((stops["S1"]["time"], stops["S1"]["delay"]), (NOW + 300, -60))  # a negative delay survives
        self.assertEqual(stops["S3"]["time"], NOW + 900)
        self.assertIsNone(stops["S4"]["time"])

    def test_something_that_is_not_a_feed_is_refused(self):
        with self.assertRaises(FeedError):
            parse_trip_updates(b"\x08")
        with self.assertRaises(FeedError):
            parse_alerts(b"<html>nope</html>")


class ReadingAlerts(unittest.TestCase):
    def test_text_routes_stops_and_time_ranges(self):
        data = pb.feed(
            [
                pb.alert_entity(
                    "a1",
                    header="Detour on King",
                    description="Buses are using Queen.",
                    routes=["504", "514", "504"],
                    stops=["S9"],
                    start=NOW - 100,
                    end=NOW + 100,
                    effect=4,
                ),
                pb.alert_entity("a2", header=""),  # no text at all: left out
            ]
        )
        _, alerts = parse_alerts(data)
        self.assertEqual(len(alerts), 1)
        a = alerts[0]
        self.assertEqual((a["header"], a["description"]), ("Detour on King", "Buses are using Queen."))
        self.assertEqual((a["routes"], a["stops"], a["effect"]), (["504", "514"], ["S9"], 4))
        self.assertEqual(a["periods"], [{"start": NOW - 100, "end": NOW + 100}])

    def test_english_is_preferred_over_the_first_language(self):
        both = pb.message(10, pb.translated("Détour", "fr") + pb.translated("Detour", "en-CA"))
        _, alerts = parse_alerts(pb.feed([pb.alert_entity("a", extra_header=both)]))
        self.assertEqual(alerts[0]["header"], "Detour")


def snapshot(parse_feed, content, status=200, clock=None):
    clock = clock or Clock(NOW)
    session = FakeSession(FakeResponse(status, content))
    return parse_feed("https://x.example/rt", (), Settings(), session=session, clock=clock), session, clock


class SnapshotFeedTests(unittest.TestCase):
    def test_it_reads_once_per_refresh_period(self):
        feed, session, clock = snapshot(alert_feed, pb.feed([pb.alert_entity("a", header="Hi")]))
        feed.read()
        feed.read()
        self.assertEqual(len(session.calls), 1)
        clock.now += 11
        feed.read()
        self.assertEqual(len(session.calls), 2)

    def test_the_last_good_copy_is_kept_when_a_refresh_fails(self):
        feed, session, clock = snapshot(alert_feed, pb.feed([pb.alert_entity("a", header="Hi")]))
        items, status = feed.read()
        self.assertTrue(status["ok"])
        session.responses = [FakeResponse(503, b"")]
        clock.now += 11
        items, status = feed.read()
        self.assertFalse(status["ok"])
        self.assertEqual(len(items), 1)
        self.assertIn("error", status["error"])

    def test_a_feed_that_never_worked_has_no_update_time(self):
        feed, _, _ = snapshot(trip_update_feed, b"", status=500)
        items, status = feed.read()
        self.assertEqual((items, status["updated"], status["ok"]), ([], None, False))


def app_with(trips=None, alerts=None):
    clock = Clock(NOW)
    settings = Settings()
    systems = [
        System("ttc", "TTC", "Toronto", (43.65, -79.38), 11, feed_url="https://x/vp", available=True),
        System("miway", "MiWay", "Mississauga", (43.59, -79.64), 12, feed_url="https://y/vp", available=True),
    ]
    registry = Registry(settings, systems, clock=clock)
    from tests.test_app import StubFeed

    registry._feeds = {"ttc": StubFeed([]), "miway": StubFeed([])}
    store = StaticStore("https://agency.example/gtfs.zip", settings, session=ZipSession(FakeResponse(200, make_zip())), clock=clock, background=False)
    registry._statics = {"ttc": store, "miway": StaticStore("", settings, background=False)}
    registry._extras[("ttc", "trips")] = trip_update_feed("https://x/t", (), settings, FakeSession(FakeResponse(200, trips)), clock) if trips is not None else None
    registry._extras[("ttc", "alerts")] = alert_feed("https://x/a", (), settings, FakeSession(FakeResponse(200, alerts)), clock) if alerts is not None else None
    registry._extras[("miway", "trips")] = None
    registry._extras[("miway", "alerts")] = None
    store.ensure()
    return create_app(settings, registry=registry, clock=clock).test_client()


class ArrivalsApi(unittest.TestCase):
    def setUp(self):
        stops = [
            pb.stop_time_update("1", time=NOW + 600, delay=120),
            pb.stop_time_update("2", time=NOW + 1200),
        ]
        data = pb.feed(
            [
                pb.trip_update_entity("a", "504", "x", [pb.stop_time_update("1", time=NOW + 300)]),
                pb.trip_update_entity("b", "7", "y", stops),
                pb.trip_update_entity("c", "504", "z", [pb.stop_time_update("1", time=NOW - 600)]),  # already gone
                pb.trip_update_entity("d", "504", "w", [pb.stop_time_update("1", time=NOW + 30 * 3600)]),  # far away
            ],
            timestamp=NOW,
        )
        self.client = app_with(trips=data)

    def test_the_next_buses_come_soonest_first_with_names_and_delays(self):
        data = self.client.get("/api/arrivals?system=ttc&stop=1").get_json()
        self.assertEqual(data["state"], "ok")
        self.assertEqual([(a["route"], a["minutes"]) for a in data["arrivals"]], [("504", 5), ("7", 10)])
        self.assertEqual(data["arrivals"][0]["routeName"], "King")
        self.assertEqual(data["arrivals"][1]["delayMinutes"], 2)
        self.assertIsNone(data["arrivals"][0]["delayMinutes"])

    def test_a_stop_with_no_buses_coming_gives_an_empty_list(self):
        data = self.client.get("/api/arrivals?system=ttc&stop=999").get_json()
        self.assertEqual((data["state"], data["arrivals"]), ("ok", []))

    def test_a_system_without_a_trip_feed_says_so(self):
        self.assertEqual(self.client.get("/api/arrivals?system=miway&stop=1").get_json(), {"state": "off", "arrivals": []})

    def test_bad_requests(self):
        self.assertEqual(self.client.get("/api/arrivals?system=ttc").status_code, 400)
        self.assertEqual(self.client.get("/api/arrivals?system=ttc&stop=" + "9" * 41).status_code, 400)
        self.assertEqual(self.client.get("/api/arrivals?system=zzz&stop=1").status_code, 404)

    def test_a_feed_that_is_down_is_reported(self):
        client = app_with(trips=b"")  # an empty answer is not a feed
        data = client.get("/api/arrivals?system=ttc&stop=1").get_json()
        self.assertEqual((data["state"], data["arrivals"]), ("error", []))

    def test_only_eight_are_sent(self):
        many = pb.feed([pb.trip_update_entity(f"e{i}", "7", f"t{i}", [pb.stop_time_update("1", time=NOW + 60 * (i + 1))]) for i in range(12)])
        data = app_with(trips=many).get("/api/arrivals?system=ttc&stop=1").get_json()
        self.assertEqual(len(data["arrivals"]), 8)


class AlertsApi(unittest.TestCase):
    def test_current_alerts_are_listed_with_route_names_and_repeats_merged(self):
        data = pb.feed(
            [
                pb.alert_entity("1", header="Detour", description="Use Queen.", routes=["504"], start=NOW - 5, end=NOW + 5),
                pb.alert_entity("2", header="Detour", description="Use Queen.", routes=["7"]),  # same text, another route
                pb.alert_entity("3", header="Old news", end=NOW - 10),
                pb.alert_entity("4", header="Later", start=NOW + 500),
                pb.alert_entity("5", header="System wide", description="Fare change."),
            ]
        )
        out = app_with(alerts=data).get("/api/alerts?system=ttc").get_json()
        self.assertEqual(out["state"], "ok")
        by_header = {a["header"]: a for a in out["alerts"]}
        self.assertEqual(set(by_header), {"Detour", "System wide"})
        self.assertEqual(by_header["Detour"]["routes"], ["504", "7"])
        self.assertEqual(by_header["System wide"]["routes"], [])

    def test_a_system_without_alerts_says_so(self):
        self.assertEqual(app_with().get("/api/alerts?system=miway").get_json(), {"state": "off", "alerts": []})

    def test_the_alert_text_is_plain_text(self):
        data = pb.feed([pb.alert_entity("1", header="<b>Hi</b>  there\n now")])
        out = app_with(alerts=data).get("/api/alerts?system=ttc").get_json()
        self.assertEqual(out["alerts"][0]["header"], "<b>Hi</b> there now")  # extra spaces are tidied, the page shows it as text


if __name__ == "__main__":
    unittest.main()
