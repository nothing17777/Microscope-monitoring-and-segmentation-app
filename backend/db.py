"""SQLite state. One connection, serialized behind a lock.

Tables:
  files     - one row per discovered TIFF, the resume log
  telemetry - one row per sampler tick, the chart source
  specs     - single row, the machine spec dump
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
"""


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

    # --- files -------------------------------------------------------------

    def known_paths(self) -> dict[str, str]:
        """path -> state, for every file we have ever seen."""
        with self._lock:
            rows = self._conn.execute("SELECT path, state FROM files").fetchall()
        return {r["path"]: r["state"] for r in rows}

    def add_pending(self, path: str, size: int, width: int | None, height: int | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO files(path, size, state, width, height, found_at) "
                "VALUES (?,?,'pending',?,?,?)",
                (path, size, width, height, time.time()),
            )
            self._conn.commit()

    def pending(self, limit: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM files WHERE state='pending' ORDER BY found_at LIMIT ?", (limit,)
            ).fetchall()

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
        with self._lock:
            rows = self._conn.execute(
                "SELECT state, COUNT(*) n FROM files GROUP BY state"
            ).fetchall()
        c = {r["state"]: r["n"] for r in rows}
        pending, done, failed = c.get("pending", 0), c.get("done", 0), c.get("failed", 0)
        return {
            "found": pending + done + failed,
            "segmented": done,
            "failed": failed,
            "backlog": pending,
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

    def done_since(self, seconds: float) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) n FROM files WHERE state='done' AND done_at >= ?",
                (time.time() - seconds,),
            ).fetchone()
        return int(row["n"])
