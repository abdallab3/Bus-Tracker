"""Trip updates (next arrivals) and service alerts: other live feeds an agency may publish."""

import threading
import time

import requests

from .gtfsrt import FeedError, parse_alerts, parse_trip_updates


class SnapshotFeed:
    """Reads one GTFS-Realtime feed, never more often than the refresh setting, and keeps the last good copy."""

    def __init__(self, url, headers, parse, settings, session=None, clock=time.time):
        self.url = url
        self.headers = dict(headers)
        self.parse = parse
        self.s = settings
        self.session = session or requests.Session()
        self.clock = clock
        self._lock = threading.Lock()
        self._last_try = None
        self._last_ok = None
        self._error = None
        self._items = []

    def _fetch(self):
        headers = {"Accept": "application/x-protobuf, application/octet-stream, */*;q=0.5"}
        headers.update(self.headers)
        resp = self.session.get(self.url, headers=headers, timeout=self.s.feed_timeout, stream=True)
        try:
            if resp.status_code != 200:
                raise FeedError(f"The feed answered with an error (code {resp.status_code}).")
            chunks, total = [], 0
            for chunk in resp.iter_content(65536):
                total += len(chunk)
                if total > self.s.feed_max_bytes:
                    raise FeedError("The feed is larger than this app accepts.")
                chunks.append(chunk)
            return self.parse(b"".join(chunks))
        finally:
            resp.close()

    def read(self):
        """Return (items, status) where status has ok, updated and error."""
        now = self.clock()
        with self._lock:
            if self._last_try is None or now - self._last_try >= self.s.feed_refresh_seconds:
                self._last_try = now
                try:
                    _, items = self._fetch()
                except FeedError as e:
                    self._error = str(e)
                except requests.RequestException:
                    self._error = "The feed could not be reached."
                except Exception:
                    self._error = "The feed could not be read."
                else:
                    self._items, self._last_ok, self._error = items, now, None
            return self._items, {"ok": self._error is None, "updated": self._last_ok, "error": self._error}


def trip_update_feed(url, headers, settings, session=None, clock=time.time):
    return SnapshotFeed(url, headers, parse_trip_updates, settings, session, clock)


def alert_feed(url, headers, settings, session=None, clock=time.time):
    return SnapshotFeed(url, headers, parse_alerts, settings, session, clock)
