"""SQLite state. One connection, serialized behind a lock.

Tables:
  files     - one row per discovered TIFF, the resume log
  telemetry - one row per sampler tick, the chart source
  specs     - single row, the machine spec dump
  jobs      - the two queues (acquisition and segmentation)
  kv        - small persisted flags, e.g. the global segmentation pause
"""
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path        TEXT PRIMARY KEY,
    size        INTEGER NOT NULL,
    state       TEXT NOT NULL,              -- pending | done | failed
    width       INTEGER,
    height      INTEGER,
    mask_path   TEXT,
    error       TEXT,
    found_at    REAL NOT NULL,
    done_at     REAL
);
CREATE INDEX IF NOT EXISTS files_state ON files(state);

CREATE TABLE IF NOT EXISTS telemetry (
    ts    REAL PRIMARY KEY,
    data  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS specs (
    id    INTEGER PRIMARY KEY CHECK (id = 1),
    ts    REAL NOT NULL,
    data  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,              -- acquisition | segmentation
    status      TEXT NOT NULL,              -- queued|running|paused|done|failed|cancelled
    params      TEXT NOT NULL DEFAULT '{}',
    created_at  REAL NOT NULL,
    started_at  REAL,
    finished_at REAL,
    total       INTEGER NOT NULL DEFAULT 0,
    completed   INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS jobs_kind_status ON jobs(kind, status);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Columns added after the first release. SQLite has no "ADD COLUMN IF NOT
# EXISTS", so each is attempted and the duplicate-column error ignored.
MIGRATIONS = [
    "ALTER TABLE files ADD COLUMN job_id INTEGER",
]

ACTIVE = ("queued", "running", "paused")
TERMINAL = ("done", "failed", "cancelled")


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            for stmt in MIGRATIONS:
                try:
                    self._conn.execute(stmt)
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- files -------------------------------------------------------------

    def known_paths(self) -> dict[str, str]:
        """path -> state, for every file we have ever seen."""
        with self._lock:
            rows = self._conn.execute("SELECT path, state FROM files").fetchall()
        return {r["path"]: r["state"] for r in rows}

    def add_pending(self, path: str, size: int, width: int | None, height: int | None,
                    job_id: int | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO files(path, size, state, width, height, found_at, job_id) "
                "VALUES (?,?,'pending',?,?,?,?)",
                (path, size, width, height, time.time(), job_id),
            )
            self._conn.commit()

    def pending(self, limit: int, job_id: int | None = None) -> list[sqlite3.Row]:
        """Oldest pending files, optionally only those belonging to one job."""
        sql = "SELECT * FROM files WHERE state='pending'"
        args: list[Any] = []
        if job_id is not None:
            sql += " AND job_id=?"
            args.append(job_id)
        sql += " ORDER BY found_at LIMIT ?"
        args.append(limit)
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def mark_done(self, path: str, mask_path: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE files SET state='done', mask_path=?, error=NULL, done_at=? WHERE path=?",
                (mask_path, time.time(), path),
            )
            self._conn.commit()

    def mark_failed(self, path: str, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE files SET state='failed', error=?, done_at=? WHERE path=?",
                (error[:4000], time.time(), path),
            )
            self._conn.commit()

    def set_dims(self, path: str, width: int, height: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE files SET width=?, height=? WHERE path=?", (width, height, path)
            )
            self._conn.commit()

    def counts(self) -> dict[str, int]:
        """Headline counts.

        Files left pending by a cancelled job are deliberately kept out of
        `backlog`: they are not failures, but nothing is going to segment
        them either, and counting them would keep the falling-behind banner
        red forever. They are reported separately as `set_aside`.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT f.state AS state, "
                "       COALESCE(j.status, '') = 'cancelled' AS abandoned, "
                "       COUNT(*) AS n "
                "FROM files f LEFT JOIN jobs j ON j.id = f.job_id "
                "GROUP BY state, abandoned"
            ).fetchall()
        done = failed = pending = set_aside = 0
        for r in rows:
            if r["state"] == "done":
                done += r["n"]
            elif r["state"] == "failed":
                failed += r["n"]
            elif r["abandoned"]:
                set_aside += r["n"]
            else:
                pending += r["n"]
        return {
            "found": pending + done + failed + set_aside,
            "segmented": done,
            "failed": failed,
            "backlog": pending,
            "set_aside": set_aside,
        }

    def failures(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT path, error, done_at FROM files WHERE state='failed' ORDER BY done_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def first_found_at(self) -> float | None:
        with self._lock:
            row = self._conn.execute("SELECT MIN(found_at) t FROM files").fetchone()
        return row["t"] if row and row["t"] is not None else None

    # --- telemetry ---------------------------------------------------------

    def add_sample(self, ts: float, data: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO telemetry(ts, data) VALUES (?,?)", (ts, json.dumps(data))
            )
            self._conn.commit()

    def history(self, minutes: float) -> list[dict[str, Any]]:
        since = time.time() - minutes * 60
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM telemetry WHERE ts >= ? ORDER BY ts", (since,)
            ).fetchall()
        return [json.loads(r["data"]) for r in rows]

    # --- specs -------------------------------------------------------------

    def set_specs(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO specs(id, ts, data) VALUES (1,?,?)",
                (time.time(), json.dumps(data)),
            )
            self._conn.commit()

    def get_specs(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT data FROM specs WHERE id=1").fetchone()
        return json.loads(row["data"]) if row else None

    # --- jobs --------------------------------------------------------------

    def create_job(self, kind: str, params: dict[str, Any], total: int = 0,
                   status: str = "queued") -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO jobs(kind, status, params, created_at, total) VALUES (?,?,?,?,?)",
                (kind, status, json.dumps(params), time.time(), total),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._job_dict(row) if row else None

    def list_jobs(self, kind: str | None = None,
                  statuses: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM jobs WHERE 1=1"
        args: list[Any] = []
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args.extend(statuses)
        sql += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [self._job_dict(r) for r in rows]

    @staticmethod
    def _job_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        try:
            d["params"] = json.loads(d.get("params") or "{}")
        except ValueError:
            d["params"] = {}
        return d

    def update_job(self, job_id: int, **fields: Any) -> None:
        if not fields:
            return
        if "params" in fields and not isinstance(fields["params"], str):
            fields["params"] = json.dumps(fields["params"])
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE jobs SET {cols} WHERE id=?",
                               (*fields.values(), job_id))
            self._conn.commit()

    def bump_job(self, job_id: int, completed: int = 0, failed: int = 0,
                 total: int = 0) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET completed=completed+?, failed=failed+?, total=total+? "
                "WHERE id=?",
                (completed, failed, total, job_id),
            )
            self._conn.commit()

    def open_segmentation_job(self) -> dict[str, Any] | None:
        """The segmentation job newly discovered files should join.

        Segmenting a continuous stream is one piece of work, so files append
        to the open job — running, queued or paused — rather than opening a
        new one per poll. A fresh job starts only once the previous one has
        drained (or was cancelled), which keeps the running row stable on a
        dashboard someone is sitting next to for two hours.

        Appending to a *paused* job is deliberate: that is what makes its
        backlog visibly grow while it is held.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE kind='segmentation' "
                "AND status IN ('running','queued','paused') ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return self._job_dict(row) if row else None

    def next_runnable_segmentation_job(self) -> dict[str, Any] | None:
        """Oldest segmentation job that is allowed to run right now."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE kind='segmentation' AND status IN ('running','queued') "
                "ORDER BY id LIMIT 1"
            ).fetchone()
        return self._job_dict(row) if row else None

    def job_pending_count(self, job_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) n FROM files WHERE state='pending' AND job_id=?", (job_id,)
            ).fetchone()
        return int(row["n"])

    def running_acquisition(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE kind='acquisition' AND status IN ('running','paused') "
                "ORDER BY id LIMIT 1"
            ).fetchone()
        return self._job_dict(row) if row else None

    def orphan_pending_to(self, job_id: int) -> int:
        """Adopt pending files from before the jobs table existed."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE files SET job_id=? WHERE state='pending' AND job_id IS NULL", (job_id,)
            )
            self._conn.commit()
            return cur.rowcount

    # --- kv ----------------------------------------------------------------

    def kv_get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?,?)",
                               (key, json.dumps(value)))
            self._conn.commit()

    def done_since(self, seconds: float) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) n FROM files WHERE state='done' AND done_at >= ?",
                (time.time() - seconds,),
            ).fetchone()
        return int(row["n"])
