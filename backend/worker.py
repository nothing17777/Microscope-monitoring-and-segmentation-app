"""Segmentation worker.

One background thread. Each cycle:
  1. scan the input dir recursively for TIFFs and note sizes
  2. enqueue only files whose size was identical on the previous poll
     (LumaViewPro writes incrementally; a growing file is never queued)
  3. segment pending files in dimension-homogeneous batches

Discovered files are attached to a segmentation job, so the queue panel has
something real to show and so a job can be paused or cancelled independently
of the worker as a whole.

State lives in SQLite, so a restart resumes and never redoes work — including
the global pause flag.
"""
import logging
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import numpy as np

from .config import Config
from .db import Database
from .device import pick_device
from .experiment import PHASE_KEY, UNLABELED

log = logging.getLogger("worker")

TIFF_SUFFIXES = {".tif", ".tiff"}

# Window for the rolling inter-arrival statistics, and how many arrivals to
# keep. Ten minutes is long enough to survive a well change and short enough
# that a phase switch shows up inside one phase.
ARRIVAL_WINDOW_S = 600.0
ARRIVAL_MAX = 2000


def _is_tiff(p: Path) -> bool:
    return p.suffix.lower() in TIFF_SUFFIXES


def _read_image(path: Path) -> np.ndarray:
    import tifffile

    arr = tifffile.imread(str(path))
    return np.asarray(arr)


def _dims(arr: np.ndarray) -> tuple[int, int]:
    """(height, width) of the last two axes — works for 2D and stacks."""
    if arr.ndim < 2:
        raise ValueError(f"not an image: shape {arr.shape}")
    return int(arr.shape[-2]), int(arr.shape[-1])


class Worker:
    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        self.device = pick_device()
        self.model: Any = None
        self.model_label = "dry-run" if cfg.dry_run else cfg.model_name
        self.batch_size = cfg.batch_size
        self.oom_events = 0
        self.last_error: str | None = None
        self.input_bytes = 0
        self.input_files_on_disk = 0
        self.last_scan_at: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # The partial-file guard: path -> (size, when we first saw that size).
        # A file is ready only when the size is unchanged across consecutive
        # polls AND has held still for cfg.stable_seconds. The two-poll rule
        # alone is not enough: a writer that stalls for longer than one poll
        # interval mid-file looks identical to a finished file.
        self._seen: dict[str, tuple[int, float]] = {}
        # Reads that failed because the file was still truncated. Retried on
        # later polls rather than failed outright.
        self._read_retries: dict[str, int] = {}
        # Per-image seconds for recent batches; the ETA range is built from
        # their spread rather than from a single running average.
        self._batch_times: deque[float] = deque(maxlen=20)
        # Acquisition arrival times, for the inter-arrival metric. These are
        # file mtimes, not discovery times: LumaViewPro's own write timestamp
        # is the only thing here with better resolution than the poll
        # interval, and poll-quantised arrivals would show the scanner's
        # cadence rather than the microscope's.
        self._arrivals: deque[float] = deque(maxlen=ARRIVAL_MAX)
        self._cancelled: set[int] = set()
        self.current_job_id: int | None = None
        self.paused_reason: str | None = None

    MAX_READ_RETRIES = 5

    # --- queue plumbing ----------------------------------------------------

    def recent_per_image_seconds(self) -> list[float]:
        return list(self._batch_times)

    def set_batch_size(self, n: int) -> int:
        """Change the batch size for the next batch. No restart, no drain.

        The batch in flight finishes at the size it started with; segment_one_batch
        reads this at the top of each batch.
        """
        n = max(1, min(256, int(n)))
        if n != self.batch_size:
            log.info("batch size %d -> %d (effective next batch)", self.batch_size, n)
        self.batch_size = n
        return n

    def phase(self) -> str:
        """The experiment phase label in force, from the database."""
        try:
            return str(self.db.kv_get(PHASE_KEY, UNLABELED)) or UNLABELED
        except Exception:  # noqa: BLE001
            return UNLABELED

    def arrival_stats(self, window_s: float = ARRIVAL_WINDOW_S) -> dict[str, Any]:
        """Rolling mean and p95 of the gap between consecutive arrivals.

        This is how a slowdown in LumaViewPro is detected: it reports nothing
        about itself, but if segmentation starts stealing disk or CPU from
        acquisition the gap between images widens. Fewer than three gaps in
        the window is not a measurement, so it reports nothing.
        """
        cutoff = time.time() - window_s
        ts = sorted(t for t in self._arrivals if t >= cutoff)
        gaps = [b - a for a, b in zip(ts, ts[1:]) if b >= a]
        if len(gaps) < 3:
            return {"mean_s": None, "p95_s": None, "n": len(gaps),
                    "note": "not enough data"}
        gaps.sort()
        i = min(len(gaps) - 1, max(0, int(round(0.95 * (len(gaps) - 1)))))
        return {"mean_s": sum(gaps) / len(gaps), "p95_s": gaps[i], "n": len(gaps),
                "note": ""}

    def _record_arrival(self, mtime: float) -> None:
        self._arrivals.append(mtime)

    def _record_batch(self, n_images: int, seconds: float) -> None:
        """Per-image timing for the ETA, plus one durable row for the report.

        Both workers call this instead of appending to _batch_times, so the
        batch size and phase in force are recorded with the timing in one
        place and cannot disagree between the two apps.
        """
        per_image = seconds / max(1, n_images)
        self._batch_times.append(per_image)
        add = getattr(self.db, "add_batch", None)
        if add is None:
            return  # the dashboard DB has no batches table; timing-only is fine
        try:
            run_id = getattr(self, "current_run_id", None) or self.current_job_id
            add(ts=time.time(), run_id=run_id, phase=self.phase(),
                batch_size=self.batch_size, n_images=n_images, seconds=seconds,
                per_image_s=per_image, device=self.device)
        except Exception:  # noqa: BLE001
            log.exception("failed to record batch row")

    def request_cancel(self, job_id: int) -> None:
        """Stop pulling work for this job after the current batch finishes."""
        self._cancelled.add(job_id)

    def _globally_paused(self) -> bool:
        from .jobs import GLOBAL_PAUSE_KEY

        return bool(self.db.kv_get(GLOBAL_PAUSE_KEY, False))

    def _job_for_new_files(self) -> int:
        """The segmentation job newly discovered files belong to."""
        job = self.db.open_segmentation_job()
        if job is None:
            job_id = self.db.create_job("segmentation", {"source": "auto"})
            adopted = self.db.orphan_pending_to(job_id)
            if adopted:
                self.db.bump_job(job_id, total=adopted)
                log.info("segmentation job #%s adopted %d pending file(s)", job_id, adopted)
            return job_id
        return int(job["id"])

    # --- lifecycle ---------------------------------------------------------

    def load_model(self) -> None:
        """Load CellposeModel once, at startup, and reuse it for every batch."""
        if self.cfg.dry_run:
            log.warning("DRY RUN: Cellpose is not loaded; masks are a trivial threshold")
            return
        from cellpose.models import CellposeModel

        log.info("loading CellposeModel(pretrained_model=%r) on %s",
                 self.cfg.model_name, self.device)
        t0 = time.time()
        self.model = CellposeModel(
            gpu=self.device != "cpu",
            pretrained_model=self.cfg.model_name,
            device=self._torch_device(),
        )
        log.info("model ready in %.1fs", time.time() - t0)

    def _torch_device(self):
        try:
            import torch

            return torch.device(self.device)
        except Exception:  # noqa: BLE001
            return None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="segmenter", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _run(self) -> None:
        try:
            self.load_model()
        except Exception as e:  # noqa: BLE001
            self.last_error = f"model load failed: {e!r}"
            log.exception("model load failed; worker will scan but not segment")
        while not self._stop.is_set():
            cycle_start = time.time()
            try:
                self.scan()
            except Exception:  # noqa: BLE001
                log.exception("scan failed")
            # Drain whatever is ready before the next scan, but keep scanning
            # often enough that the backlog number stays honest. A pause is
            # checked here, between batches: the batch in flight always runs
            # to completion so no file is left half-segmented.
            while not self._stop.is_set():
                if self._globally_paused():
                    self.paused_reason = "worker paused globally"
                    break
                self.paused_reason = None
                try:
                    did = self.segment_one_batch()
                except Exception:  # noqa: BLE001
                    log.exception("batch failed unexpectedly")
                    did = False
                if not did or time.time() - cycle_start > self.cfg.poll_interval:
                    break
            sleep = max(0.0, self.cfg.poll_interval - (time.time() - cycle_start))
            self._stop.wait(sleep)

    # --- discovery ---------------------------------------------------------

    def scan(self) -> int:
        """Record sizes; enqueue files whose size is unchanged since last poll."""
        root = self.cfg.input_dir
        sizes: dict[str, int] = {}
        mtimes: dict[str, float] = {}
        total_bytes = 0
        if root.exists():
            for p in root.rglob("*"):
                if not _is_tiff(p):
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue  # vanished mid-scan
                if not p.is_file():
                    continue
                sizes[str(p)] = st.st_size
                mtimes[str(p)] = st.st_mtime
                total_bytes += st.st_size
        now = time.time()
        self.input_bytes = total_bytes
        self.input_files_on_disk = len(sizes)
        self.last_scan_at = now

        known = self.db.known_paths()
        enqueued = 0
        job_id: int | None = None
        seen: dict[str, tuple[int, float]] = {}
        for path, size in sizes.items():
            prev = self._seen.get(path)
            # Carry the "unchanged since" timestamp forward only while the
            # size actually matches; any change restarts the clock.
            since = prev[1] if prev is not None and prev[0] == size else now
            seen[path] = (size, since)
            if path in known or size <= 0:
                continue
            if prev is None or prev[0] != size:
                continue  # new, or grew since the last poll
            if now - since < self.cfg.stable_seconds:
                continue  # stable, but not for long enough yet
            try:
                h, w = _dims(_read_image(Path(path)))
            except Exception as e:  # noqa: BLE001
                # Most likely still truncated despite a size that held still
                # (a writer stalled mid-file). Retry on later polls; only give
                # up after several, so a genuinely corrupt file is reported.
                n = self._read_retries.get(path, 0) + 1
                self._read_retries[path] = n
                if n >= self.MAX_READ_RETRIES:
                    if job_id is None:
                        job_id = self._job_for_new_files()
                    self.db.add_pending(path, size, None, None, job_id=job_id)
                    self.db.mark_failed(path, f"unreadable after {n} attempts: {e!r}")
                    self.db.bump_job(job_id, failed=1, total=1)
                    # It still arrived; acquisition cadence does not care that
                    # we could not read it.
                    self._record_arrival(mtimes.get(path, now))
                    log.warning("unreadable TIFF %s: %r", path, e)
                else:
                    seen[path] = (size, now)  # restart the stability clock
                    log.info("not readable yet (attempt %d), will retry: %s", n, path)
                continue
            self._read_retries.pop(path, None)
            if job_id is None:
                job_id = self._job_for_new_files()
            self.db.add_pending(path, size, w, h, job_id=job_id)
            self._record_arrival(mtimes.get(path, now))
            enqueued += 1
        self._seen = seen
        if enqueued and job_id is not None:
            self.db.bump_job(job_id, total=enqueued)
            log.info("enqueued %d file(s) into segmentation job #%s; %d TIFF(s) on disk",
                     enqueued, job_id, len(sizes))
        return enqueued

    # --- segmentation ------------------------------------------------------

    def segment_one_batch(self) -> bool:
        """Segment the largest dimension-homogeneous group of pending files.

        Returns True if a batch was attempted.
        """
        job = self.db.next_runnable_segmentation_job()
        if job is None:
            self.current_job_id = None
            return False
        job_id = int(job["id"])
        if job_id in self._cancelled:
            # Cancelled between batches. Its files stay pending, not failed,
            # and are adopted by the next job that opens.
            self._cancelled.discard(job_id)
            self.current_job_id = None
            return False

        # Over-fetch so grouping by dimensions can still fill a batch when
        # channels/z-slices of different sizes are interleaved.
        rows = self.db.pending(self.batch_size * 8, job_id=job_id)
        if not rows:
            if job["status"] == "running":
                self.db.update_job(job_id, status="done", finished_at=time.time())
                log.info("segmentation job #%s complete", job_id)
            self.current_job_id = None
            return False

        if job["status"] == "queued":
            self.db.update_job(job_id, status="running", started_at=time.time())
        self.current_job_id = job_id

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
                    continue  # dims changed since discovery; next batch picks it up
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
            # Model-level failure (OOM that survived halving, or a backend
            # error). Files stay pending; back off until the next poll so we
            # don't spin on them.
            return False

        self._record_batch(len(kept), time.time() - t0)

        ok = failed = 0
        for path, mask in zip(kept, masks):
            try:
                out = self._mask_path(Path(path))
                out.parent.mkdir(parents=True, exist_ok=True)
                np.save(str(out), np.asarray(mask))
                self.db.mark_done(path, str(out))
                ok += 1
            except Exception as e:  # noqa: BLE001
                self.db.mark_failed(path, f"mask write failed: {e!r}")
                log.exception("mask write failed for %s", path)
                failed += 1
        self.db.bump_job(job_id, completed=ok, failed=failed)
        return True

    def _mask_path(self, src: Path) -> Path:
        try:
            rel = src.relative_to(self.cfg.input_dir)
        except ValueError:
            rel = Path(src.name)
        return (self.cfg.output_dir / rel).with_suffix(".npy")

    def _run_model(self, images: list[np.ndarray]) -> list[np.ndarray] | None:
        """Run the model, halving the batch on CUDA OOM. Never raises."""
        bs = len(images)
        while bs >= 1:
            try:
                if self.cfg.dry_run or self.model is None:
                    return [self._dry_mask(im) for im in images]
                out = self.model.eval(images, batch_size=bs)
                masks = out[0] if isinstance(out, (tuple, list)) else out
                if isinstance(masks, np.ndarray) and len(images) > 1 and masks.ndim == 3:
                    masks = list(masks)
                elif not isinstance(masks, list):
                    masks = [masks]
                return masks
            except Exception as e:  # noqa: BLE001
                if self._is_oom(e):
                    self.oom_events += 1
                    new_bs = max(1, bs // 2)
                    self.last_error = f"CUDA OOM at batch {bs}; retrying at {new_bs}"
                    log.error("CUDA OOM at batch %d, retrying at %d", bs, new_bs)
                    self._empty_cache()
                    if new_bs == bs:  # already at 1 and still OOM
                        self.last_error = f"CUDA OOM at batch size 1: {e!r}"
                        log.error("OOM at batch size 1; giving up on this batch")
                        return None
                    self.batch_size = max(1, min(self.batch_size, new_bs))
                    bs = new_bs
                    continue
                self.last_error = repr(e)
                log.exception("segmentation batch failed")
                return None
        return None

    @staticmethod
    def _dry_mask(arr: np.ndarray) -> np.ndarray:
        im = arr if arr.ndim == 2 else arr.reshape(-1, *arr.shape[-2:])[0]
        return (im > np.median(im)).astype(np.int32)

    @staticmethod
    def _is_oom(e: Exception) -> bool:
        text = f"{type(e).__name__}: {e}".lower()
        return "out of memory" in text or "cuda_error_out_of_memory" in text

    def _empty_cache(self) -> None:
        try:
            import torch

            torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass

    def snapshot(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "model": self.model_label,
            "model_loaded": self.model is not None or self.cfg.dry_run,
            "batch_size": self.batch_size,
            "oom_events": self.oom_events,
            "last_error": self.last_error,
            "input_bytes": self.input_bytes,
            "input_files_on_disk": self.input_files_on_disk,
            "last_scan_at": self.last_scan_at,
            "dry_run": self.cfg.dry_run,
            "current_job_id": self.current_job_id,
            "paused_reason": self.paused_reason,
            "batches_timed": len(self._batch_times),
            "phase": self.phase(),
            "arrivals": self.arrival_stats(),
        }
