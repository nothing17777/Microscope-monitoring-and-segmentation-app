"""Run-scoped segmentation worker.

Subclasses the dashboard's Worker rather than copying it. `scan()` — which
holds the partial-file guard (size stable across two polls AND held for
stable_seconds, plus the truncated-read retry) — is inherited untouched, so
that logic physically cannot drift between the two apps. Only the parts that
are run-aware are overridden:

  _wanted             -> which LVP capture folders to look at
  _job_for_path       -> one run per capture folder (ProtocolData/<timestamp>)
  _job_for_new_files  -> the run a loose image belongs to
  _globally_paused    -> station's own pause flag
  segment_one_batch   -> pulls work for one run, honours per-run pause/cancel

Everything to do with the model (batching by dimensions, the OOM halving,
the validated settings and exports in backend/analysis.py, dry-run masks)
comes from the parent.
"""
import logging
import threading
import time
from pathlib import Path
from typing import Any

from backend.worker import Worker as BaseWorker

from . import lvp
from .config import Config
from .db import Database
from .wells import well_of

log = logging.getLogger("station.worker")

GLOBAL_PAUSE_KEY = "segmentation_paused"
UNASSIGNED_RUN = "Unassigned"


class StationWorker(BaseWorker):
    def __init__(self, cfg: Config, db: Database):
        super().__init__(cfg, db)
        self.db: Database = db
        self.current_run_id: int | None = None
        self._cancelled: set[int] = set()
        self._lock = threading.Lock()
        # Capture folder name -> segment it or not. Decided once per folder.
        self._folders: dict[str, bool] = {}
        self._started_at = time.time()

    # --- run attribution ---------------------------------------------------

    def _job_for_new_files(self) -> int:
        """Images always belong to a run; one is opened if none is active.

        A dark folder that starts filling before anybody created a run is a
        real case (someone starts LumaViewPro first). Those images land in an
        'Unassigned' run rather than being dropped or silently merged into
        whatever run is created next.
        """
        with self._lock:
            run_id = self.db.current_run_id()
            if run_id is not None:
                return run_id
            run_id = self.db.create_run(
                UNASSIGNED_RUN, plate="", expected=0,
                notes="Opened automatically: images appeared with no run active.",
                baseline=0, status="running")
            self.db.update_run(run_id, started_at=time.time())
            log.warning("images appeared with no active run; opened run #%s", run_id)
            return run_id

    # --- capture folders -----------------------------------------------------

    def _folder_of(self, path: Path) -> str | None:
        """The capture folder an image sits directly inside, or None if loose."""
        try:
            rel = path.relative_to(self.cfg.input_dir)
        except ValueError:
            return None
        return rel.parts[0] if len(rel.parts) == 2 else None

    def _wanted(self, path: Path) -> bool:
        try:
            parts = path.relative_to(self.cfg.input_dir).parts
        except ValueError:
            return False
        if len(parts) == 1:
            return True  # loose files: the original single-folder mode
        if len(parts) != 2:
            return False  # LVP post-processing output nested inside a run
        folder = parts[0]
        ok = self._folders.get(folder)
        if ok is None:
            ok = self._folders[folder] = self._decide(folder)
        return ok

    def _decide(self, folder: str) -> bool:
        """Whether a capture folder seen for the first time is segmented.

        ProtocolData keeps every run ever taken. Queuing all of them on
        startup would bury the live acquisition under weeks of old plates,
        so a folder that was already there is taken only if it already has a
        run, was named, or is still being written to.
        """
        if folder in self.db.source_dirs() or folder in self.cfg.capture_folders:
            return True
        if self.cfg.include_existing:
            return True
        try:
            mtime = (self.cfg.input_dir / folder).stat().st_mtime
        except OSError:
            return False
        if mtime >= self._started_at - self.cfg.active_window:
            return True
        log.info("skipping capture folder %s: finished before startup "
                 "(use --capture-folder %s to segment it)", folder, folder)
        return False

    def _job_for_path(self, path: Path) -> int:
        folder = self._folder_of(path)
        if folder is None:
            return self._job_for_new_files()
        with self._lock:
            run_id = self.db.run_for_source(folder)
            if run_id is not None:
                return run_id
            d = self.cfg.input_dir / folder
            expected = lvp.expected_images(d)
            run_id = self.db.create_run(
                folder, plate="", expected=expected, baseline=0, status="running",
                notes=f"LumaViewPro capture folder {d}")
            self.db.update_run(run_id, source_dir=folder,
                               started_at=lvp.folder_started_at(d) or time.time())
            log.info("capture folder %s -> run #%s (%s images expected)",
                     folder, run_id, expected or "unknown")
            return run_id

    def scan(self) -> int:
        n = super().scan()
        if n:
            self._tag_wells()
        return n

    def _tag_wells(self) -> None:
        """Fill in well, channel, tile and Z for images the scan just added."""
        rows = self.db._q(  # noqa: SLF001 - same package, one connection
            "SELECT path FROM images WHERE well IS NULL AND channel IS NULL LIMIT 5000")
        for r in rows:
            w = well_of(r["path"], self.cfg.input_dir)
            meta = lvp.parse_name(Path(r["path"])) or {}
            self.db._x(  # noqa: SLF001
                "UPDATE images SET well=?, channel=?, tile=?, z=? WHERE path=?",
                (w, meta.get("channel", ""), meta.get("tile"), meta.get("z"), r["path"]))

    # --- pausing -----------------------------------------------------------

    def _globally_paused(self) -> bool:
        return bool(self.db.kv_get(GLOBAL_PAUSE_KEY, False))

    def request_cancel(self, run_id: int) -> None:
        self._cancelled.add(run_id)

    def recent_per_image_seconds(self) -> list[float]:
        return list(self._batch_times)

    # --- segmentation ------------------------------------------------------

    def _next_run(self) -> dict[str, Any] | None:
        """Oldest run with work whose segmentation is neither paused nor cancelled."""
        rows = self.db._q(  # noqa: SLF001
            "SELECT r.* FROM runs r WHERE r.seg_status='active' "
            "AND r.status != 'cancelled' "
            "AND EXISTS (SELECT 1 FROM images i WHERE i.run_id=r.id AND i.state='pending') "
            "ORDER BY r.id LIMIT 1")
        return dict(rows[0]) if rows else None

    def segment_one_batch(self) -> bool:
        run = self._next_run()
        if run is None:
            self.current_run_id = None
            self._finish_drained_runs()
            return False
        run_id = int(run["id"])
        if run_id in self._cancelled:
            self._cancelled.discard(run_id)
            self.current_run_id = None
            return False

        rows = self.db.pending(self.batch_size * 8, run_id=run_id)
        if not rows:
            self.current_run_id = None
            return False
        self.current_run_id = run_id

        if self._segment_rows(rows) is None:
            return False  # model failure: back off until the next poll
        return True

    def _finish_drained_runs(self) -> None:
        """Close a run once acquisition stopped and every image is segmented."""
        rows = self.db._q(  # noqa: SLF001
            "SELECT id FROM runs WHERE status='running' AND expected_image_count > 0 "
            "AND (SELECT COUNT(*) FROM images i WHERE i.run_id=runs.id) >= expected_image_count "
            "AND NOT EXISTS (SELECT 1 FROM images i WHERE i.run_id=runs.id AND i.state='pending')")
        for r in rows:
            self.db.update_run(int(r["id"]), status="done", finished_at=time.time())
            log.info("run #%s complete", r["id"])

    def snapshot(self) -> dict[str, Any]:
        snap = super().snapshot()
        snap["current_run_id"] = self.current_run_id
        return snap
