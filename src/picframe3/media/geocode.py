"""Reverse geocoding with a persistent, rate-limited cache.

Nominatim's usage policy allows one request per second and requires a real
identifying User-Agent.  Both are enforced here rather than left to the
operator, and every answer is cached in SQLite keyed to a rounded coordinate,
so a frame that has been running for a year makes almost no requests at all.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence

_log = logging.getLogger(__name__)

DEFAULT_KEY_ORDER: tuple[tuple[str, ...], ...] = (
    ("tourism", "attraction", "amenity", "isolated_dwelling"),
    ("neighbourhood", "suburb", "village", "town"),
    ("city", "municipality", "county"),
    ("state", "province", "region"),
    ("country",),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS geocache (
    key      TEXT PRIMARY KEY,
    payload  TEXT NOT NULL,
    fetched  REAL NOT NULL
);
"""


class Geocoder:
    def __init__(
        self,
        cache_path: str,
        *,
        contact: str = "",
        enabled: bool = True,
        key_order: Sequence[Sequence[str]] = DEFAULT_KEY_ORDER,
        suppress: Sequence[str] = (),
        language: str = "en",
        precision: int = 4,
        min_interval: float = 1.2,
        endpoint: str = "https://nominatim.openstreetmap.org/reverse",
    ):
        self.enabled = enabled and bool(contact)
        if enabled and not contact:
            _log.warning(
                "reverse geocoding is on but no contact address is configured; "
                "Nominatim requires one, so lookups are disabled"
            )
        self.contact = contact
        self.key_order = [tuple(k) for k in key_order]
        self.suppress = [s for s in suppress if s]
        self.language = language
        self.precision = precision
        self.min_interval = min_interval
        self.endpoint = endpoint
        self._last_request = 0.0
        self._lock = threading.Lock()
        self._db = sqlite3.connect(cache_path, check_same_thread=False)
        self._db.execute(_SCHEMA)
        self._db.commit()

    def _key(self, lat: float, lon: float) -> str:
        return f"{round(lat, self.precision)},{round(lon, self.precision)}"

    def lookup(self, lat: float | None, lon: float | None, *,
               cached_only: bool = False) -> str | None:
        """Resolve a place name.

        ``cached_only`` answers from the on-disk cache or not at all, which is
        what the indexer wants: scanning ten thousand photographs must never
        turn into ten thousand rate-limited requests.  Anything not cached is
        left for the backfill pass, which trickles through them politely.
        """
        if lat is None or lon is None:
            return None
        key = self._key(lat, lon)
        with self._lock:
            row = self._db.execute("SELECT payload FROM geocache WHERE key=?", (key,)).fetchone()
        if row:
            return self._format(json.loads(row[0]))
        if cached_only or not self.enabled:
            return None
        payload = self._fetch(lat, lon)
        if payload is None:
            return None
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO geocache(key, payload, fetched) VALUES (?,?,?)",
                (key, json.dumps(payload), time.time()),
            )
            self._db.commit()
        return self._format(payload)

    def _fetch(self, lat: float, lon: float) -> dict | None:
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
        params = urllib.parse.urlencode({
            "lat": f"{lat:.6f}", "lon": f"{lon:.6f}", "format": "jsonv2",
            "zoom": "18", "addressdetails": "1", "accept-language": self.language,
        })
        req = urllib.request.Request(
            f"{self.endpoint}?{params}",
            headers={"User-Agent": f"picframe3 (+{self.contact})"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            return data.get("address") or {}
        except (urllib.error.URLError, OSError, ValueError) as exc:
            _log.info("reverse geocoding failed for %.4f,%.4f: %s", lat, lon, exc)
            return None

    def _format(self, address: dict) -> str | None:
        parts: list[str] = []
        for group in self.key_order:
            for key in group:
                value = address.get(key)
                if value and value not in parts:
                    parts.append(str(value))
                    break
        for bad in self.suppress:
            parts = [p for p in parts if p != bad]
        return ", ".join(parts) or None

    def close(self) -> None:
        try:
            self._db.close()
        except Exception:  # pragma: no cover
            pass
