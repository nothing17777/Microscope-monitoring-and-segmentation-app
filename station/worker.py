"""Run-scoped segmentation worker.

Subclasses the dashboard's Worker rather than copying it. `scan()` — which
holds the partial-file guard (size stable across two polls AND held for
stable_seconds, plus the truncated-read retry) — is inherited untouched, so
that logic physically cannot drift between the two apps. Only the parts that
are run-aware are overridden:

  _job_for_new_files  -> the run a newly discovered image belongs to
  _globally_paused    -> station's own pause flag
  segment_one_batch   -> pulls work for one run, honours per-run pause/cancel

Everything to do with the model (batching by dimensions, the OOM halving,
mask paths, dry-run masks) comes from the parent.
"""
import logging
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

from backend.worker import Worker as BaseWorker, _dims, _read_image

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

    def scan(self) -> int:
        n = super().scan()
        if n:
            self._tag_wells()
        return n

    def _tag_wells(self) -> None:
        """Fill in the well for images the scan just added."""
        rows = self.db._q(  # noqa: SLF001 - same package, one connection
            "SELECT path FROM images WHERE well IS NULL LIMIT 5000")
        for r in rows:
            w = well_of(r["path"], self.cfg.input_dir)
            if w:
                self.db._x("UPDATE images SET well=? WHERE path=?", (w, r["path"]))  # noqa: SLF001

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

        groups: dict[tuple[Any, Any], list[str]] = defaultdict(list)
        for r in rows:
            groups[(r["width"], r["height"])].append(r["path"])
        key = max(groups, key=lambda k: len(groups[k]))
        paths = groups[key][: self.batch_size]

        images: list[np.ndarray] = []
        kept: list[str] = []
        for path in paths:
            try:
                arr = _read_image(Path(path))
                h, w = _dims(arr)
                self.db.set_dims(path, w, h)
                if (w, h) != key and key != (None, None):
                    continue
                images.append(arr)
                kept.append(path)
            except Exception as e:  # noqa: BLE001
                self.db.mark_failed(path, f"read failed: {e!r}")
                log.warning("read failed %s: %r", path, e)
        if not kept:
            return True

        t0 = time.time()
        masks = self._run_model(images)
        if masks is None:
            return False
        self._record_batch(len(kept), time.time() - t0)

        for path, mask in zip(kept, masks):
            try:
                out = self._mask_path(Path(path))
                out.parent.mkdir(parents=True, exist_ok=True)
                np.save(str(out), np.asarray(mask))
                self.db.mark_done(path, str(out))
            except Exception as e:  # noqa: BLE001
                self.db.mark_failed(path, f"mask write failed: {e!r}")
                log.exception("mask write failed for %s", path)
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
