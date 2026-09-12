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

import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from ..media.metadata import PhotoMeta

_log = logging.getLogger(__name__)

SCHEMA_VERSION = 2

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
    -- The shuffle round in which this picture was last shown.  A round covers
    -- every picture exactly once, so "has it been shown yet this round?" is a
    -- single indexed comparison, and it survives reboots, rescans and new
    -- files arriving over Samba in the middle of a round.
    play_round    INTEGER NOT NULL DEFAULT 0,
    hidden        INTEGER NOT NULL DEFAULT 0,
    indexed_at    REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS files_folder  ON files(folder);
CREATE INDEX IF NOT EXISTS files_taken   ON files(taken_at);
CREATE INDEX IF NOT EXISTS files_played  ON files(last_played);

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

def like_escape(text: str) -> str:
    """Literal text as a LIKE pattern fragment.

    ``%`` and ``_`` are wildcards to LIKE, and a folder called ``2024_Italien``
    therefore matched ``2024xItalien`` as well -- rarely, confusingly, and only
    for the people whose folders are named that way.  The backslash has to be
    escaped first, and every query using this must say ``ESCAPE '\\'``.
    """
    return (str(text or "").replace("\\", "\\\\")
            .replace("%", "\\%").replace("_", "\\_"))


def fts_query(text: str) -> str:
    """An FTS5 MATCH expression for whatever somebody typed into a search box.

    Every word becomes a quoted prefix term, so "lis" still finds Lisbon.  The
    quoting is the part that matters: a double quote in the typed text would
    otherwise close the string literal FTS5 is parsing and leave it with an
    unterminated one.  That is an ``OperationalError``, not an empty result --
    and because the frame persists its filters, the error came back on every
    single start afterwards.  Doubling the quote inside the term is the escape
    FTS5 understands, so ``dog "`` is simply a search for a dog and a quote.
    """
    terms = [t.replace('"', '""') for t in str(text or "").split()]
    return " ".join(f'"{t}"*' for t in terms if t.strip('"'))


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
        #: Every connection handed out, so that close() can close them all and
        #: not just the one belonging to whichever thread happens to call it.
        self._conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        #: What the last refused prune looked like, so the same refusal twice
        #: in a row is taken as a real deletion rather than a mount that keeps
        #: failing.  See prune_missing().
        self._prune_refused: tuple[int, int] | None = None
        # Not wrapped in transaction(): the schema script sets journal_mode,
        # which SQLite refuses inside a transaction, and executescript commits
        # whatever is open before it runs anyway.
        conn = self.connect()
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
            # check_same_thread=False is safe here *because* of the one
            # connection per thread rule above -- no two threads ever touch the
            # same object.  It is switched off only so that close() may close a
            # connection that belongs to a thread which has already finished.
            conn = sqlite3.connect(self.path, timeout=15.0, isolation_level=None,
                                   check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=15000")
            self._local.conn = conn
            with self._conns_lock:
                self._conns.append(conn)
        return conn

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """A real transaction -- which ``with conn:`` is not on this connection.

        The connection is opened in autocommit mode (``isolation_level=None``),
        and sqlite3's own connection context manager commits the implicit
        transaction it manages, of which there is none in that mode.  Every
        statement was therefore its own transaction: a crash between writing the
        row and writing its tags and search entry left a file indexed with
        neither, and ``needs_reindex()`` compares mtime and size only, so that
        half-written row was never repaired.

        ``BEGIN IMMEDIATE`` takes the write lock up front instead of upgrading
        halfway through, so a scan and the render loop queue rather than one of
        them failing with SQLITE_BUSY after having written half its work.

        Nested uses join the transaction already open rather than starting a
        second one -- that is what lets the scanner wrap a whole batch of
        upserts, each of which opens one itself, into a single commit.
        """
        conn = self.connect()
        depth = getattr(self._local, "depth", 0)
        if depth:
            self._local.depth = depth + 1
            try:
                yield conn
            finally:
                self._local.depth = depth
            return
        conn.execute("BEGIN IMMEDIATE")
        self._local.depth = 1
        try:
            yield conn
        except BaseException:
            self._local.depth = 0
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
        else:
            self._local.depth = 0
            conn.execute("COMMIT")

    def _migrate(self) -> None:
        conn = self.connect()
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        version = int(row["value"]) if row else 0
        if version > SCHEMA_VERSION:
            _log.warning("index was written by a newer picframe3 (v%d); proceeding read-only-ish",
                         version)
        # v2 added files.play_round.  An index written by v1 has every picture
        # at round 0, which means "not yet shown in the round we are in now" --
        # exactly the right starting state, so there is nothing to backfill.
        # The column is added here rather than in the schema script because
        # CREATE TABLE IF NOT EXISTS will not alter a table that already
        # exists, and the index on it would then fail on every old library.
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(files)")}
        if "play_round" not in columns:
            with self.transaction():
                conn.execute("ALTER TABLE files ADD COLUMN "
                             "play_round INTEGER NOT NULL DEFAULT 0")
            _log.info("index upgraded to schema v2 (added play_round)")
        with self.transaction():
            conn.execute("CREATE INDEX IF NOT EXISTS files_round ON files(play_round)")
            # The indices below are the ones the playlist's ORDER BY clauses
            # actually use.  files_hidden and files_portrait never were: both
            # columns hold two values over the whole library, so SQLite reads
            # the table anyway and the index is pure write cost on every scan.
            # Every statement here is idempotent, so this runs on each start
            # and repairs an index that was built by an older version.
            conn.execute("DROP INDEX IF EXISTS files_hidden")
            conn.execute("DROP INDEX IF EXISTS files_portrait")
            conn.execute("CREATE INDEX IF NOT EXISTS files_play_count "
                         "ON files(play_count, last_played)")
            conn.execute("CREATE INDEX IF NOT EXISTS files_basename "
                         "ON files(basename COLLATE NOCASE)")
            conn.execute("CREATE INDEX IF NOT EXISTS files_recent "
                         "ON files(COALESCE(taken_at, mtime))")
        if version < SCHEMA_VERSION:
            with self.transaction():
                conn.execute("INSERT OR REPLACE INTO meta(key, value) "
                             "VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))

    # -- writing -----------------------------------------------------------
    def file_state(self, path: str) -> tuple[float, int] | None:
        """``(mtime, size)`` of an indexed file, or None if it is not indexed.

        The whole of what a scan needs to know about a file it has seen before.
        Asking ``by_path()`` instead costs a full row plus a join over the tags
        only to throw both away, and a scan asks this once per file -- on a
        library of twenty thousand photographs that difference is minutes.
        """
        row = self.connect().execute(
            "SELECT mtime, size FROM files WHERE path=?", (path,)
        ).fetchone()
        return (row["mtime"], row["size"]) if row is not None else None

    @staticmethod
    def is_current(state: tuple[float, int] | None, mtime: float, size: int) -> bool:
        """Whether an indexed file still matches what is on disk."""
        if state is None:
            return False
        return abs(state[0] - mtime) <= 0.5 and state[1] == size

    def needs_reindex(self, path: str, mtime: float, size: int) -> bool:
        return not self.is_current(self.file_state(path), mtime, size)

    def upsert(self, meta: PhotoMeta, *, mtime: float, size: int,
               location: str | None = None) -> int:
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
        # One transaction for the row, its tags and its search entry: a file
        # that is in the index but in neither of the other two is invisible to
        # every tag filter and every search, and nothing would ever notice.
        with self.transaction() as conn:
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
        with self.transaction() as conn:
            conn.execute("UPDATE files SET location=? WHERE id=?", (location, file_id))
            conn.execute("UPDATE search SET location=? WHERE rowid=?", (location or "", file_id))

    def mark_played(self, file_id: int, play_round: int | None = None) -> None:
        """Record that a picture has just been shown.

        ``play_round`` is the shuffle round it was shown in; passing it is what
        lets the playlist answer "which pictures are still owed a turn?" from
        the index rather than from an in-memory cursor that a reboot or a
        rescan would throw away.
        """
        with self.transaction() as conn:
            if play_round is None:
                conn.execute(
                    "UPDATE files SET play_count=play_count+1, last_played=? WHERE id=?",
                    (time.time(), file_id),
                )
            else:
                conn.execute(
                    "UPDATE files SET play_count=play_count+1, last_played=?, "
                    "play_round=? WHERE id=?",
                    (time.time(), play_round, file_id),
                )

    def mark_round(self, file_ids: Iterable[int], play_round: int) -> None:
        """Stamp the pictures the playlist has just handed out for this round.

        Separate from :meth:`mark_played` on purpose: this is bookkeeping for
        the shuffle and must happen exactly when the playlist advances, while
        play_count is about what a viewer actually saw.
        """
        ids = list(file_ids)
        if not ids:
            return
        with self.transaction() as conn:
            conn.executemany("UPDATE files SET play_round=? WHERE id=?",
                             [(play_round, i) for i in ids])

    def play_stats(self) -> dict[str, float]:
        """How evenly the library is actually being shown."""
        row = self.connect().execute(
            "SELECT COUNT(*) AS n, MIN(play_count) AS lo, MAX(play_count) AS hi, "
            "AVG(play_count) AS avg, SUM(play_count = 0) AS never "
            "FROM files WHERE hidden = 0"
        ).fetchone()
        return {
            "files": row["n"] or 0,
            "min": row["lo"] or 0,
            "max": row["hi"] or 0,
            "average": round(row["avg"] or 0.0, 2),
            "never_shown": row["never"] or 0,
        }

    def set_hidden(self, file_id: int, hidden: bool = True) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE files SET hidden=? WHERE id=?", (int(hidden), file_id))

    def forget(self, paths: Iterable[str]) -> int:
        removed = 0
        with self.transaction() as conn:
            for path in paths:
                row = conn.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
                if row is None:
                    continue
                conn.execute("DELETE FROM search WHERE rowid=?", (row["id"],))
                conn.execute("DELETE FROM files WHERE id=?", (row["id"],))
                removed += 1
        return removed

    def forget_under(self, folder: str) -> int:
        """Forget everything below a directory that has gone.

        A deleted directory arrives as one event about the directory itself,
        and forgetting only that path leaves every photograph that was inside
        it in the index -- shown as a missing file until the next full scan.
        The pattern is escaped because a real folder may well contain ``_``.
        """
        prefix = folder.rstrip("/") + "/"
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT id FROM files WHERE path LIKE ? ESCAPE '\\'",
                (like_escape(prefix) + "%",),
            ).fetchall()
            for row in rows:
                conn.execute("DELETE FROM search WHERE rowid=?", (row["id"],))
                conn.execute("DELETE FROM files WHERE id=?", (row["id"],))
        return len(rows)

    def prune_missing(self, roots: Sequence[str], *,
                      max_fraction: float = 0.2) -> int:
        """Drop rows whose file has gone, restricted to the scanned roots.

        ``max_fraction`` is the safety catch.  A Samba or NFS share that failed
        to mount looks exactly like somebody having deleted every photograph on
        it, and pruning takes the play counts, the rounds, the hidden flags and
        the geocoded place names with the rows -- work of months, gone in one
        pass over an empty mount point.  Nothing sane deletes a fifth of a
        library between two scans, so above that share the first scan refuses
        and says so.

        The *second* consecutive scan that sees the same thing goes ahead.  A
        mount problem is over by then -- the share is back and the files are
        there again -- whereas somebody who really did delete a year of
        photographs would otherwise be left with an index that refuses to
        forget them for ever, and a slideshow walking into the gaps one
        missing file at a time.
        """
        conn = self.connect()
        gone: list[str] = []
        indexed = 0
        for row in conn.execute("SELECT path FROM files"):
            path = row["path"]
            if roots and not any(path.startswith(r) for r in roots):
                continue
            indexed += 1
            if not os.path.exists(path):
                gone.append(path)
        if not gone:
            return 0
        share = len(gone) / indexed if indexed else 1.0
        signature = (len(gone), indexed)
        if 0 < max_fraction < share and self._prune_refused != signature:
            self._prune_refused = signature
            _log.warning(
                "refusing to prune: %d of %d indexed files under the scanned "
                "folders (%.0f%%) have vanished at once -- that looks like a "
                "mount problem, not a deletion; the index is left untouched. "
                "If they really are gone, the next scan will remove them",
                len(gone), indexed, share * 100,
            )
            return 0
        self._prune_refused = None
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

    def locations(self) -> list[tuple[str, int]]:
        """Every place name in the index, commonest first.

        What the location filter offers as suggestions.  Grouped by the whole
        name rather than by town, because the filter matches any part of it:
        picking "Carteret, Normandy, France" and then shortening it by hand to
        "France" is how somebody widens a filter without knowing the schema.
        """
        return [
            (r["location"], r["n"])
            for r in self.connect().execute(
                "SELECT location, COUNT(*) AS n FROM files "
                "WHERE hidden=0 AND location IS NOT NULL AND location != '' "
                "GROUP BY location ORDER BY n DESC, location"
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

    def with_position(self, limit: int = 5000) -> list[tuple[int, float, float]]:
        """Every row that has coordinates, place name or not.

        Used when the wording of place names changes -- the raw Nominatim
        replies are already in the geocache, so every caption can be rewritten
        from disk without a single new request.
        """
        rows = self.connect().execute(
            "SELECT id, latitude, longitude FROM files "
            "WHERE latitude IS NOT NULL AND longitude IS NOT NULL LIMIT ?",
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
        with self.transaction() as conn:
            cur = conn.execute("UPDATE files SET location = NULL WHERE location IS NOT NULL")
            count = cur.rowcount
            conn.execute("UPDATE search SET location = ''")
        return count

    def search(self, text: str, limit: int = 200) -> list[Record]:
        if not text.strip():
            return []
        query = fts_query(text)
        if not query:
            return []
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
            "SUM(size) AS bytes, "
            "MIN(CASE WHEN hidden=0 THEN play_count END) AS shown_min, "
            "MAX(CASE WHEN hidden=0 THEN play_count END) AS shown_max, "
            "SUM(CASE WHEN hidden=0 AND play_count=0 THEN 1 ELSE 0 END) AS never_shown "
            "FROM files"
        ).fetchone()
        return {
            "files": row["files"] or 0,
            "videos": row["videos"] or 0,
            "hidden": row["hidden"] or 0,
            "shown_min": row["shown_min"] or 0,
            "shown_max": row["shown_max"] or 0,
            "never_shown": row["never_shown"] or 0,
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
        with self.transaction() as conn:
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
        """Close every connection this library ever handed out.

        Closing only the calling thread's connection -- which is what this did
        -- leaves the scanner's and the web server's open, so the WAL file
        stays behind and a test's temporary directory cannot be removed on
        Windows.  The connections are closed from whichever thread calls this,
        which is why they are opened with check_same_thread=False.
        """
        with self._conns_lock:
            conns, self._conns = self._conns, []
        for conn in conns:
            try:
                conn.close()
            except sqlite3.Error as exc:  # pragma: no cover - defensive
                _log.debug("could not close a library connection: %s", exc)
        self._local.conn = None
        self._local.depth = 0
