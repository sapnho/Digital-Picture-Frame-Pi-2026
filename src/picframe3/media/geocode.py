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

#: Ready-made answers to "how much of the address do you want?".
#:
#: Nominatim returns a dozen or more address keys and which ones exist varies
#: wildly by country -- a French hamlet has ``village``, a German one
#: ``isolated_dwelling``, a US address neither.  That is why each tier is a
#: *list* of keys and the first one present wins: it is what makes one setting
#: behave the same in Normandy and in Hessen.
DETAIL_PRESETS: dict[str, tuple[tuple[str, ...], ...]] = {
    "full": DEFAULT_KEY_ORDER,
    "town_region_country": (
        ("village", "town", "city", "municipality", "suburb", "neighbourhood"),
        ("state", "province", "region"),
        ("country",),
    ),
    "town_country": (
        ("village", "town", "city", "municipality", "suburb", "neighbourhood"),
        ("country",),
    ),
    "town": (
        ("village", "town", "city", "municipality", "suburb", "neighbourhood"),
    ),
    "region_country": (
        ("state", "province", "region"),
        ("country",),
    ),
    "country": (("country",),),
}

#: The address keys Nominatim actually returns, loosely finest-first.  Not
#: every key comes back for every place -- a French hamlet has ``village``, a
#: German one ``isolated_dwelling``, a US suburb neither -- which is exactly
#: why a tier is a *list* rather than a single key.
NOMINATIM_KEYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Named place", ("tourism", "attraction", "amenity", "leisure", "historic",
                     "building", "isolated_dwelling", "farm")),
    ("Street", ("house_number", "road", "pedestrian", "footway")),
    ("Neighbourhood", ("neighbourhood", "quarter", "suburb", "city_district",
                       "hamlet", "croft")),
    ("Town", ("village", "town", "city", "municipality", "borough")),
    ("Region", ("county", "state_district", "state", "province", "region")),
    ("Country", ("postcode", "country", "country_code", "continent")),
)

#: A real reply, kept so the settings page can show what a set of tiers would
#: produce before a single photograph is on screen.
EXAMPLE_ADDRESS = {
    "tourism": "Plage de Hattainville",
    "road": "Route de la Mer",
    "hamlet": "Hattainville",
    "village": "Baubigny",
    "county": "Cherbourg",
    "state": "Normandy",
    "postcode": "50270",
    "country": "France",
    "country_code": "fr",
}


def format_address(address: dict, key_order: Sequence[Sequence[str]],
                   suppress: Sequence[str] = ()) -> str | None:
    """One place name from one Nominatim reply.

    Per tier, the first key that is present wins; a value already used is not
    repeated.  A module function rather than a method so the settings page can
    preview a set of tiers without a cache, a network or a Geocoder.
    """
    parts: list[str] = []
    for group in key_order:
        for key in group:
            value = address.get(key)
            if value and value not in parts:
                parts.append(str(value))
                break
    for bad in suppress:
        parts = [p for p in parts if p != bad]
    return ", ".join(parts) or None


#: What the settings page calls them.
DETAIL_LABELS = {
    "full": "Everything — landmark, town, region, country",
    "town_region_country": "Town, region, country",
    "town_country": "Town, country",
    "town": "Town only",
    "region_country": "Region and country",
    "country": "Country only",
    "custom": "Custom — the geo.key_order list in the config file",
}


def key_order_for(detail: str, custom: Sequence[Sequence[str]] | None = None
                  ) -> tuple[tuple[str, ...], ...]:
    """The tiers to use, from a preset name or the hand-written list."""
    name = (detail or "full").strip().lower()
    if name == "custom":
        return tuple(tuple(tier) for tier in (custom or DEFAULT_KEY_ORDER))
    preset = DETAIL_PRESETS.get(name)
    if preset is None:
        _log.warning("unknown geo.detail %r; using 'full'", detail)
        return DEFAULT_KEY_ORDER
    return preset


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
        return format_address(address, self.key_order, self.suppress)

    def cached_address(self, lat: float | None, lon: float | None) -> dict | None:
        """The raw reply for a point, if it is already cached.  Never fetches.

        The settings page previews place names against a photograph the frame
        has actually shown, which is far more use than a canned example: you
        see your own caption change as you edit the tiers.
        """
        if lat is None or lon is None:
            return None
        with self._lock:
            row = self._db.execute("SELECT payload FROM geocache WHERE key=?",
                                   (self._key(lat, lon),)).fetchone()
        return json.loads(row[0]) if row else None

    def set_style(self, key_order: Sequence[Sequence[str]],
                  suppress: Sequence[str] = ()) -> None:
        """Change the wording of place names without touching the cache."""
        self.key_order = [tuple(k) for k in key_order]
        self.suppress = [s for s in suppress if s]

    def close(self) -> None:
        try:
            self._db.close()
        except Exception:  # pragma: no cover
            pass
