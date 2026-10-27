"""SQLite state, organised around runs rather than files.

Tables:
  runs      - the unit the lab cares about: one plate, one acquisition
  images    - every discovered TIFF, owned by a run, tagged with its well
  telemetry - one row per sampler tick, tagged with the run it belongs to,
              so a finished run's charts can be replayed months later
  events    - what was alerted on, and when (the webhook debounce lives here)
  kv        - small persisted flags, e.g. the global segmentation pause

Several method names (add_pending, bump_job, known_paths) match the ones the
dashboard's Worker calls, because station's worker subclasses it to inherit
the partial-file guard verbatim. Here `job_id` is a run id.
"""
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT NOT NULL,
    plate                TEXT NOT NULL DEFAULT '',
    expected_image_count INTEGER NOT NULL DEFAULT 0,
    status               TEXT NOT NULL,   -- queued|running|paused|done|failed|cancelled
    notes                TEXT NOT NULL DEFAULT '',
    created_at           REAL NOT NULL,
    started_at           REAL,
    finished_at          REAL,
    seg_status           TEXT NOT NULL DEFAULT 'active',  -- active|paused|cancelled
    baseline_count       INTEGER NOT NULL DEFAULT 0,
    error                TEXT
);
CREATE INDEX IF NOT EXISTS runs_status ON runs(status);

CREATE TABLE IF NOT EXISTS images (
    path       TEXT PRIMARY KEY,
    run_id     INTEGER,
    well       TEXT,
    size       INTEGER NOT NULL,
    state      TEXT NOT NULL,              -- pending | done | failed
    width      INTEGER,
    height     INTEGER,
    mask_path  TEXT,
    error      TEXT,
    found_at   REAL NOT NULL,
    done_at    REAL
);
CREATE INDEX IF NOT EXISTS images_state ON images(state);
CREATE INDEX IF NOT EXISTS images_run ON images(run_id, state);
CREATE INDEX IF NOT EXISTS images_well ON images(run_id, well);

CREATE TABLE IF NOT EXISTS telemetry (
    ts     REAL PRIMARY KEY,
    run_id INTEGER,
    data   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS telemetry_run ON telemetry(run_id, ts);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    kind    TEXT NOT NULL,
    run_id  INTEGER,
    ts      REAL NOT NULL,
    message TEXT NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind, ts);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

ACTIVE_RUN = ("queued", "running", "paused")
TERMINAL_RUN = ("done", "failed", "cancelled")


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def _x(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur

    # --- runs --------------------------------------------------------------

    def create_run(self, name: str, plate: str, expected: int, notes: str,
                   baseline: int, status: str = "queued") -> int:
        return int(self._x(
            "INSERT INTO runs(name, plate, expected_image_count, status, notes, "
            "created_at, baseline_count) VALUES (?,?,?,?,?,?,?)",
            (name, plate, expected, status, notes, time.time(), baseline),
        ).lastrowid)

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM runs WHERE id=?", (run_id,))
        return dict(rows[0]) if rows else None

    def list_runs(self, statuses: tuple[str, ...] | None = None,
                  limit: int = 200) -> list[dict[str, Any]]:
        sql = "SELECT * FROM runs"
        args: tuple = ()
        if statuses:
            sql += f" WHERE status IN ({','.join('?' * len(statuses))})"
            args = statuses
        sql += " ORDER BY id DESC LIMIT ?"
        return [dict(r) for r in self._q(sql, (*args, limit))]

    def update_run(self, run_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), run_id))

    def active_acquisition(self) -> dict[str, Any] | None:
        """The one run holding the microscope, if any."""
        rows = self._q(
            "SELECT * FROM runs WHERE status IN ('running','paused') ORDER BY id LIMIT 1")
        return dict(rows[0]) if rows else None

    def current_run_id(self) -> int | None:
        """Where newly discovered images belong."""
        rows = self._q(
            "SELECT id FROM runs WHERE status IN ('running','paused','queued') "
            "ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END, id "
            "LIMIT 1")
        return int(rows[0]["id"]) if rows else None

    # --- images ------------------------------------------------------------

    def known_paths(self) -> dict[str, str]:
        return {r["path"]: r["state"] for r in self._q("SELECT path, state FROM images")}

    def add_pending(self, path: str, size: int, width: int | None, height: int | None,
                    job_id: int | None = None, well: str | None = None) -> None:
        self._x(
            "INSERT OR IGNORE INTO images(path, run_id, well, size, state, width, height, found_at)"
            " VALUES (?,?,?,?,'pending',?,?,?)",
            (path, job_id, well, size, width, height, time.time()),
        )

    def pending(self, limit: int, run_id: int | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM images WHERE state='pending'"
        args: list[Any] = []
        if run_id is not None:
            sql += " AND run_id=?"
            args.append(run_id)
        sql += " ORDER BY found_at LIMIT ?"
        args.append(limit)
        return self._q(sql, tuple(args))

    def mark_done(self, path: str, mask_path: str) -> None:
        self._x("UPDATE images SET state='done', mask_path=?, error=NULL, done_at=? "
                "WHERE path=?", (mask_path, time.time(), path))

    def mark_failed(self, path: str, error: str) -> None:
        self._x("UPDATE images SET state='failed', error=?, done_at=? WHERE path=?",
                (error[:4000], time.time(), path))

    def set_dims(self, path: str, width: int, height: int) -> None:
        self._x("UPDATE images SET width=?, height=? WHERE path=?", (width, height, path))

    def bump_job(self, job_id: int, completed: int = 0, failed: int = 0, total: int = 0) -> None:
        """No-op: run progress is counted from the images table, not cached.

        The parent Worker calls this after each batch. Here the counts are
        derived, so there is nothing to increment and nothing to drift.
        """

    def counts(self, run_id: int | None = None) -> dict[str, int]:
        """Headline counts. Images of a cancelled run are set aside, not backlog."""
        sql = ("SELECT i.state AS state, COALESCE(r.status,'')='cancelled' AS abandoned, "
               "COUNT(*) AS n FROM images i LEFT JOIN runs r ON r.id=i.run_id")
        args: tuple = ()
        if run_id is not None:
            sql += " WHERE i.run_id=?"
            args = (run_id,)
        sql += " GROUP BY state, abandoned"
        done = failed = pending = set_aside = 0
        for r in self._q(sql, args):
            if r["state"] == "done":
                done += r["n"]
            elif r["state"] == "failed":
                failed += r["n"]
            elif r["abandoned"]:
                set_aside += r["n"]
            else:
                pending += r["n"]
        return {"found": done + failed + pending + set_aside, "segmented": done,
                "failed": failed, "backlog": pending, "set_aside": set_aside}

    def well_counts(self, run_id: int) -> dict[str, dict[str, int]]:
        rows = self._q(
            "SELECT well, state, COUNT(*) n FROM images WHERE run_id=? AND well IS NOT NULL "
            "GROUP BY well, state", (run_id,))
        out: dict[str, dict[str, int]] = {}
        for r in rows:
            out.setdefault(r["well"], {})[r["state"]] = r["n"]
        return out

    def wells_seen(self, run_id: int) -> int:
        rows = self._q("SELECT COUNT(DISTINCT well) n FROM images "
                       "WHERE run_id=? AND well IS NOT NULL", (run_id,))
        return int(rows[0]["n"])

    def failures(self, run_id: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT path, well, run_id, error, done_at FROM images WHERE state='failed'"
        args: tuple = ()
        if run_id is not None:
            sql += " AND run_id=?"
            args = (run_id,)
        return [dict(r) for r in self._q(sql + " ORDER BY done_at DESC LIMIT 500", args)]

    def done_since(self, seconds: float) -> int:
        rows = self._q("SELECT COUNT(*) n FROM images WHERE state='done' AND done_at >= ?",
                       (time.time() - seconds,))
        return int(rows[0]["n"])

    def first_found_at(self, run_id: int | None = None) -> float | None:
        sql, args = "SELECT MIN(found_at) t FROM images", ()
        if run_id is not None:
            sql += " WHERE run_id=?"
            args = (run_id,)
        rows = self._q(sql, args)
        return rows[0]["t"] if rows and rows[0]["t"] is not None else None

    # --- telemetry ---------------------------------------------------------

    def add_sample(self, ts: float, run_id: int | None, data: dict[str, Any]) -> None:
        self._x("INSERT OR REPLACE INTO telemetry(ts, run_id, data) VALUES (?,?,?)",
                (ts, run_id, json.dumps(data)))

    def history(self, minutes: float) -> list[dict[str, Any]]:
        return [json.loads(r["data"]) for r in
                self._q("SELECT data FROM telemetry WHERE ts >= ? ORDER BY ts",
                        (time.time() - minutes * 60,))]

    def run_history(self, run_id: int) -> list[dict[str, Any]]:
        """Every sample recorded for one run — the replay source."""
        return [json.loads(r["data"]) for r in
                self._q("SELECT data FROM telemetry WHERE run_id=? ORDER BY ts", (run_id,))]

    # --- events ------------------------------------------------------------

    def last_event(self, kind: str, run_id: int | None = None) -> dict[str, Any] | None:
        sql, args = "SELECT * FROM events WHERE kind=?", [kind]
        if run_id is not None:
            sql += " AND run_id=?"
            args.append(run_id)
        rows = self._q(sql + " ORDER BY ts DESC LIMIT 1", tuple(args))
        return dict(rows[0]) if rows else None

    def add_event(self, kind: str, run_id: int | None, message: str, delivered: bool) -> int:
        return int(self._x(
            "INSERT INTO events(kind, run_id, ts, message, delivered) VALUES (?,?,?,?,?)",
            (kind, run_id, time.time(), message, 1 if delivered else 0)).lastrowid)

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        return [dict(r) for r in
                self._q("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,))]

    # --- kv ----------------------------------------------------------------

    def kv_get(self, key: str, default: Any = None) -> Any:
        rows = self._q("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def kv_set(self, key: str, value: Any) -> None:
        self._x("INSERT OR REPLACE INTO kv(key, value) VALUES (?,?)", (key, json.dumps(value)))
