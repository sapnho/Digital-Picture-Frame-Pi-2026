"""The photo index: a single SQLite file in WAL mode.

``picframe`` also used SQLite, and that was one of its better decisions -- it
is the right database for a device that has to survive being unplugged.  What
is new here:

* **WAL journalling**, so a scan writing in the background never blocks the
  render loop's reads, and a power cut mid-scan cannot corrupt the index.
* **FTS5 full-text search** over titles, captions, tags and paths, which is
  what makes "show me photos from Lisbon" a query instead of a table scan.
* **Normalised tags**, so filtering by keyword is an index lookup rather than
  a ``LIKE '%...%'`` over a comma-joined string.
* Play counts and last-played timestamps, so the shuffle can actually avoid
  repeating itself across restarts.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from ..media.metadata import PhotoMeta

_log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS files (
    id            INTEGER PRIMARY KEY,
    path          TEXT    NOT NULL UNIQUE,
    folder        TEXT    NOT NULL,
    basename      TEXT    NOT NULL,
    ext           TEXT    NOT NULL,
    mtime         REAL    NOT NULL,
    size          INTEGER NOT NULL,
    is_video      INTEGER NOT NULL DEFAULT 0,
    width         INTEGER,
    height        INTEGER,
    orientation   INTEGER DEFAULT 1,
    is_portrait   INTEGER NOT NULL DEFAULT 0,
    taken_at      REAL,
    make          TEXT,
    model         TEXT,
    lens          TEXT,
    f_number      REAL,
    exposure_time TEXT,
    iso           INTEGER,
    focal_length  REAL,
    latitude      REAL,
    longitude     REAL,
    location      TEXT,
    title         TEXT,
    caption       TEXT,
    rating        INTEGER,
    duration      REAL,
    play_count    INTEGER NOT NULL DEFAULT 0,
    last_played   REAL,
    hidden        INTEGER NOT NULL DEFAULT 0,
    indexed_at    REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS files_folder  ON files(folder);
CREATE INDEX IF NOT EXISTS files_taken   ON files(taken_at);
CREATE INDEX IF NOT EXISTS files_played  ON files(last_played);
CREATE INDEX IF NOT EXISTS files_hidden  ON files(hidden);
CREATE INDEX IF NOT EXISTS files_portrait ON files(is_portrait);

CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);
CREATE TABLE IF NOT EXISTS file_tags (
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    tag_id  INTEGER NOT NULL REFERENCES tags(id)  ON DELETE CASCADE,
    PRIMARY KEY (file_id, tag_id)
);
CREATE INDEX IF NOT EXISTS file_tags_tag ON file_tags(tag_id);

CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
    path, title, caption, tags, location, tokenize='unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_COLUMNS = (
    "id", "path", "folder", "basename", "ext", "mtime", "size", "is_video",
    "width", "height", "orientation", "is_portrait", "taken_at", "make", "model",
    "lens", "f_number", "exposure_time", "iso", "focal_length", "latitude",
    "longitude", "location", "title", "caption", "rating", "duration",
    "play_count", "last_played", "hidden", "indexed_at",
)


@dataclass
class Record:
    """One row of ``files``, with the metadata view the renderer needs."""

    id: int
    path: str
    folder: str
    basename: str
    ext: str
    mtime: float
    size: int
    is_video: bool
    width: int | None
    height: int | None
    orientation: int
    is_portrait: bool
    taken_at: float | None
    make: str | None
    model: str | None
    lens: str | None
    f_number: float | None
    exposure_time: str | None
    iso: int | None
    focal_length: float | None
    latitude: float | None
    longitude: float | None
    location: str | None
    title: str | None
    caption: str | None
    rating: int | None
    duration: float | None
    play_count: int
    last_played: float | None
    hidden: bool
    indexed_at: float
    tags: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.tags is None:
            self.tags = []

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Record:
        return cls(**{k: row[k] for k in _COLUMNS})

    def as_meta(self) -> PhotoMeta:
        return PhotoMeta(
            path=self.path,
            width=self.width or 0,
            height=self.height or 0,
            orientation=self.orientation or 1,
            taken_at=self.taken_at,
            make=self.make,
            model=self.model,
            lens=self.lens,
            f_number=self.f_number,
            exposure_time=self.exposure_time,
            iso=self.iso,
            focal_length=self.focal_length,
            latitude=self.latitude,
            longitude=self.longitude,
            title=self.title,
            caption=self.caption,
            tags=list(self.tags or []),
            rating=self.rating,
            is_video=bool(self.is_video),
            duration=self.duration,
        )

    def as_dict(self) -> dict[str, Any]:
        d = {k: getattr(self, k) for k in _COLUMNS}
        d["tags"] = list(self.tags or [])
        return d


class Library:
    def __init__(self, path: str):
        self.path = os.path.expanduser(path)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._local = threading.local()
        with self.connect() as conn:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        self._migrate()

    # -- connections -------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        """One connection per thread; SQLite objects are not thread-safe."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=15.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=15000")
            self._local.conn = conn
        return conn

    def _migrate(self) -> None:
        conn = self.connect()
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        version = int(row["value"]) if row else 0
        if version > SCHEMA_VERSION:
            _log.warning("index was written by a newer picframe3 (v%d); proceeding read-only-ish",
                         version)
        # Future migrations chain here; v1 is the initial schema.

    # -- writing -----------------------------------------------------------
    def needs_reindex(self, path: str, mtime: float, size: int) -> bool:
        row = self.connect().execute(
            "SELECT mtime, size FROM files WHERE path=?", (path,)
        ).fetchone()
        if row is None:
            return True
        return abs(row["mtime"] - mtime) > 0.5 or row["size"] != size

    def upsert(self, meta: PhotoMeta, *, mtime: float, size: int,
               location: str | None = None) -> int:
        conn = self.connect()
        folder = os.path.dirname(meta.path)
        basename = os.path.basename(meta.path)
        ext = os.path.splitext(basename)[1].lower()
        dw, dh = meta.display_size
        values = (
            meta.path, folder, basename, ext, mtime, size, int(meta.is_video),
            meta.width, meta.height, meta.orientation, int(dh > dw),
            meta.taken_at, meta.make, meta.model, meta.lens, meta.f_number,
            meta.exposure_time, meta.iso, meta.focal_length, meta.latitude,
            meta.longitude, location, meta.title, meta.caption, meta.rating,
            meta.duration, time.time(),
        )
        with conn:
            conn.execute(
                """
                INSERT INTO files (
                    path, folder, basename, ext, mtime, size, is_video,
                    width, height, orientation, is_portrait, taken_at, make, model,
                    lens, f_number, exposure_time, iso, focal_length, latitude,
                    longitude, location, title, caption, rating, duration, indexed_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET
                    folder=excluded.folder, basename=excluded.basename, ext=excluded.ext,
                    mtime=excluded.mtime, size=excluded.size, is_video=excluded.is_video,
                    width=excluded.width, height=excluded.height,
                    orientation=excluded.orientation, is_portrait=excluded.is_portrait,
                    taken_at=excluded.taken_at, make=excluded.make, model=excluded.model,
                    lens=excluded.lens, f_number=excluded.f_number,
                    exposure_time=excluded.exposure_time, iso=excluded.iso,
                    focal_length=excluded.focal_length, latitude=excluded.latitude,
                    longitude=excluded.longitude,
                    location=COALESCE(excluded.location, files.location),
                    title=excluded.title, caption=excluded.caption,
                    rating=excluded.rating, duration=excluded.duration,
                    indexed_at=excluded.indexed_at
                """,
                values,
            )
            file_id = conn.execute("SELECT id FROM files WHERE path=?", (meta.path,)).fetchone()["id"]
            self._set_tags(conn, file_id, meta.tags)
            self._index_search(conn, file_id, meta, location)
        return file_id

    @staticmethod
    def _set_tags(conn: sqlite3.Connection, file_id: int, tags: Sequence[str]) -> None:
        conn.execute("DELETE FROM file_tags WHERE file_id=?", (file_id,))
        for tag in {t.strip() for t in tags if t and t.strip()}:
            conn.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (tag,))
            row = conn.execute("SELECT id FROM tags WHERE name=?", (tag,)).fetchone()
            conn.execute(
                "INSERT OR IGNORE INTO file_tags(file_id, tag_id) VALUES (?,?)",
                (file_id, row["id"]),
            )

    @staticmethod
    def _index_search(conn: sqlite3.Connection, file_id: int, meta: PhotoMeta,
                      location: str | None) -> None:
        conn.execute("DELETE FROM search WHERE rowid=?", (file_id,))
        conn.execute(
            "INSERT INTO search(rowid, path, title, caption, tags, location) VALUES (?,?,?,?,?,?)",
            (file_id, meta.path, meta.title or "", meta.caption or "",
             " ".join(meta.tags or ()), location or ""),
        )

    def set_location(self, file_id: int, location: str | None) -> None:
        conn = self.connect()
        with conn:
            conn.execute("UPDATE files SET location=? WHERE id=?", (location, file_id))
            conn.execute("UPDATE search SET location=? WHERE rowid=?", (location or "", file_id))

    def mark_played(self, file_id: int) -> None:
        conn = self.connect()
        with conn:
            conn.execute(
                "UPDATE files SET play_count=play_count+1, last_played=? WHERE id=?",
                (time.time(), file_id),
            )

    def set_hidden(self, file_id: int, hidden: bool = True) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE files SET hidden=? WHERE id=?", (int(hidden), file_id))

    def forget(self, paths: Iterable[str]) -> int:
        conn = self.connect()
        removed = 0
        with conn:
            for path in paths:
                row = conn.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
                if row is None:
                    continue
                conn.execute("DELETE FROM search WHERE rowid=?", (row["id"],))
                conn.execute("DELETE FROM files WHERE id=?", (row["id"],))
                removed += 1
        return removed

    def prune_missing(self, roots: Sequence[str]) -> int:
        """Drop rows whose file has gone, restricted to the scanned roots."""
        conn = self.connect()
        gone: list[str] = []
        for row in conn.execute("SELECT path FROM files"):
            path = row["path"]
            if roots and not any(path.startswith(r) for r in roots):
                continue
            if not os.path.exists(path):
                gone.append(path)
        if gone:
            _log.info("removing %d indexed files that no longer exist", len(gone))
        return self.forget(gone)

    # -- reading -----------------------------------------------------------
    def get(self, file_id: int) -> Record | None:
        row = self.connect().execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        return self._hydrate(row) if row else None

    def by_path(self, path: str) -> Record | None:
        row = self.connect().execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()
        return self._hydrate(row) if row else None

    def _hydrate(self, row: sqlite3.Row) -> Record:
        rec = Record.from_row(row)
        rec.tags = [
            r["name"] for r in self.connect().execute(
                "SELECT t.name FROM tags t JOIN file_tags ft ON ft.tag_id=t.id "
                "WHERE ft.file_id=? ORDER BY t.name",
                (rec.id,),
            )
        ]
        return rec

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[Record]:
        return [self._hydrate(r) for r in self.connect().execute(sql, params)]

    def count(self, where: str = "hidden=0", params: Sequence[Any] = ()) -> int:
        return self.connect().execute(
            f"SELECT COUNT(*) AS n FROM files WHERE {where}", params
        ).fetchone()["n"]

    def folders(self) -> list[tuple[str, int]]:
        return [
            (r["folder"], r["n"])
            for r in self.connect().execute(
                "SELECT folder, COUNT(*) AS n FROM files WHERE hidden=0 "
                "GROUP BY folder ORDER BY folder"
            )
        ]

    def all_tags(self) -> list[tuple[str, int]]:
        return [
            (r["name"], r["n"])
            for r in self.connect().execute(
                "SELECT t.name, COUNT(*) AS n FROM tags t "
                "JOIN file_tags ft ON ft.tag_id=t.id "
                "JOIN files f ON f.id=ft.file_id AND f.hidden=0 "
                "GROUP BY t.name ORDER BY n DESC, t.name"
            )
        ]

    def locations_missing(self, limit: int = 100) -> list[tuple[int, float, float]]:
        """Rows that have coordinates but no place name yet.

        Geocoding is deliberately decoupled from indexing: a scan must not be
        held up by a rate-limited network service, and turning geocoding on
        later has to be able to fill in everything already indexed.
        """
        rows = self.connect().execute(
            "SELECT id, latitude, longitude FROM files "
            "WHERE latitude IS NOT NULL AND longitude IS NOT NULL "
            "AND (location IS NULL OR location = '') "
            "ORDER BY COALESCE(taken_at, mtime) DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [(r["id"], r["latitude"], r["longitude"]) for r in rows]

    def count_locations_missing(self) -> int:
        return self.connect().execute(
            "SELECT COUNT(*) AS n FROM files WHERE latitude IS NOT NULL "
            "AND longitude IS NOT NULL AND (location IS NULL OR location = '')"
        ).fetchone()["n"]

    def clear_locations(self) -> int:
        """Forget every resolved place name, so they are looked up again."""
        conn = self.connect()
        with conn:
            cur = conn.execute("UPDATE files SET location = NULL WHERE location IS NOT NULL")
            conn.execute("UPDATE search SET location = ''")
        return cur.rowcount

    def search(self, text: str, limit: int = 200) -> list[Record]:
        if not text.strip():
            return []
        query = " ".join(f'"{t}"*' for t in text.split())
        try:
            rows = self.connect().execute(
                "SELECT f.* FROM search s JOIN files f ON f.id=s.rowid "
                "WHERE search MATCH ? AND f.hidden=0 ORDER BY rank LIMIT ?",
                (query, limit),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            _log.debug("search query rejected: %s", exc)
            return []
        return [self._hydrate(r) for r in rows]

    def stats(self) -> dict[str, Any]:
        conn = self.connect()
        row = conn.execute(
            "SELECT COUNT(*) AS files, "
            "SUM(is_video) AS videos, "
            "SUM(CASE WHEN hidden=1 THEN 1 ELSE 0 END) AS hidden, "
            "MIN(taken_at) AS oldest, MAX(taken_at) AS newest, "
            "SUM(size) AS bytes FROM files"
        ).fetchone()
        return {
            "files": row["files"] or 0,
            "videos": row["videos"] or 0,
            "hidden": row["hidden"] or 0,
            "oldest": row["oldest"],
            "newest": row["newest"],
            "bytes": row["bytes"] or 0,
            "folders": len(self.folders()),
            "tags": len(self.all_tags()),
            "db_path": self.path,
            "db_bytes": os.path.getsize(self.path) if os.path.exists(self.path) else 0,
        }

    # -- key/value ---------------------------------------------------------
    def set_state(self, key: str, value: Any) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)",
                (key, json.dumps(value)),
            )

    def get_state(self, key: str, default: Any = None) -> Any:
        row = self.connect().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
