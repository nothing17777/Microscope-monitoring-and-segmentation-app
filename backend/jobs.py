"""The two queues, and the arithmetic that turns them into ETAs.

Acquisition moves physical hardware, so at most one acquisition job may be
active at a time. That lock lives here, in the backend, not in the UI: two
browsers pressing Start at the same moment get one 409 and one job.

Segmentation runs concurrently with acquisition and is driven by the worker.
"""
import logging
import threading
import time
from typing import Any

from .acquisition import AcquisitionAdapter
from .db import Database

log = logging.getLogger("jobs")

ACQUISITION, SEGMENTATION = "acquisition", "segmentation"
GLOBAL_PAUSE_KEY = "segmentation_paused"

# Below this many completed batches an ETA is a guess dressed as a number.
MIN_BATCHES_FOR_ETA = 3


class JobError(Exception):
    """Refused for a reason the user should see (mapped to 4xx)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class JobManager:
    def __init__(self, db: Database, worker, adapter: AcquisitionAdapter):
        self.db = db
        self.worker = worker
        self.adapter = adapter
        self._lock = threading.Lock()

    # --- acquisition -------------------------------------------------------

    def create_acquisition(self, plate: str, expected_images: int, notes: str = "") -> dict:
        if expected_images <= 0:
            raise JobError("expected_images must be greater than zero")
        with self._lock:
            # The hardware lock. Checked and taken under one lock so two
            # simultaneous requests cannot both see an idle scope.
            active = self.db.running_acquisition()
            if active:
                raise JobError(
                    f"acquisition job #{active['id']} is already {active['status']}; "
                    "only one acquisition can be active at a time",
                    status=409,
                )
            queued = self.db.list_jobs(ACQUISITION, ("queued",))
            params = {
                "plate": plate,
                "expected_images": expected_images,
                "notes": notes,
                # Where counting starts, so a second run on the same folder
                # measures its own progress rather than inheriting the first.
                "baseline_count": self.adapter.observed_count(),
            }
            job_id = self.db.create_job(ACQUISITION, params, total=expected_images)
            if not queued and self.adapter.can_control:
                self._start_acquisition(job_id)
            elif not queued:
                # Observed mode: the run is started in LumaViewPro by hand, so
                # the job is marked running immediately and simply watches.
                self.db.update_job(job_id, status="running", started_at=time.time())
        return self.db.get_job(job_id)

    def _start_acquisition(self, job_id: int) -> None:
        job = self.db.get_job(job_id)
        try:
            self.adapter.start(job)
        except NotImplementedError as e:
            raise JobError(str(e), status=409) from e
        self.db.update_job(job_id, status="running", started_at=time.time())

    # --- control verbs -----------------------------------------------------

    def pause(self, job_id: int) -> dict:
        job = self._require(job_id)
        if job["status"] not in ("running", "queued"):
            raise JobError(f"job #{job_id} is {job['status']}; nothing to pause")
        if job["kind"] == ACQUISITION:
            self._hardware(self.adapter.pause, job)
        self.db.update_job(job_id, status="paused")
        log.info("job #%s paused", job_id)
        return self.db.get_job(job_id)

    def resume(self, job_id: int) -> dict:
        job = self._require(job_id)
        if job["status"] != "paused":
            raise JobError(f"job #{job_id} is {job['status']}; nothing to resume")
        if job["kind"] == ACQUISITION:
            self._hardware(self.adapter.resume, job)
        # Back to running if it had already started, otherwise back in line.
        self.db.update_job(job_id, status="running" if job["started_at"] else "queued")
        log.info("job #%s resumed", job_id)
        return self.db.get_job(job_id)

    def cancel(self, job_id: int) -> dict:
        job = self._require(job_id)
        if job["status"] in ("done", "failed", "cancelled"):
            raise JobError(f"job #{job_id} is already {job['status']}")
        if job["kind"] == ACQUISITION:
            self._hardware(self.adapter.cancel, job)
        else:
            # The worker finishes the batch it is mid-way through, then stops
            # pulling. Files it never reached stay pending — they are not
            # failures, and a later job will pick them up.
            self.worker.request_cancel(job_id)
        self.db.update_job(job_id, status="cancelled", finished_at=time.time())
        log.info("job #%s cancelled", job_id)
        return self.db.get_job(job_id)

    def _hardware(self, fn, job) -> None:
        try:
            fn(job)
        except NotImplementedError as e:
            raise JobError(str(e), status=409) from e

    def _require(self, job_id: int) -> dict:
        job = self.db.get_job(job_id)
        if not job:
            raise JobError(f"no job #{job_id}", status=404)
        return job

    # --- global pause ------------------------------------------------------

    def global_paused(self) -> bool:
        return bool(self.db.kv_get(GLOBAL_PAUSE_KEY, False))

    def set_global_pause(self, paused: bool) -> bool:
        # Persisted, so the state survives a restart.
        self.db.kv_set(GLOBAL_PAUSE_KEY, bool(paused))
        log.info("segmentation worker globally %s", "PAUSED" if paused else "resumed")
        return bool(paused)

    # --- ETA ---------------------------------------------------------------

    def segmentation_eta(self, pending: int) -> dict[str, Any]:
        """A range, or an honest refusal to guess.

        Built from per-image times of recently completed batches rather than
        a single average, so the spread of those batches becomes the spread
        of the estimate.
        """
        samples = sorted(self.worker.recent_per_image_seconds())
        n = len(samples)
        if n < MIN_BATCHES_FOR_ETA:
            return {
                "eta_seconds": None, "eta_low": None, "eta_high": None,
                "eta_confidence": "none",
                "eta_note": f"not enough data ({n}/{MIN_BATCHES_FOR_ETA} batches)",
            }
        if pending <= 0:
            return {"eta_seconds": 0, "eta_low": 0, "eta_high": 0,
                    "eta_confidence": "high", "eta_note": "queue empty"}
        lo = samples[int(0.25 * (n - 1))]
        hi = samples[int(0.75 * (n - 1))]
        mid = samples[n // 2]
        spread = hi / lo if lo > 0 else float("inf")
        confidence = "high" if n >= 6 and spread < 1.5 else "low"
        return {
            "eta_seconds": pending * mid,
            "eta_low": pending * lo,
            "eta_high": pending * hi,
            "eta_confidence": confidence,
            "eta_note": f"from {n} recent batch(es)",
        }

    def acquisition_eta(self, job: dict) -> dict[str, Any]:
        """Expected total over the rate actually observed so far."""
        expected = int(job["params"].get("expected_images") or 0)
        done = self._acquisition_progress(job)
        if not job["started_at"] or done <= 0:
            return {"eta_seconds": None, "eta_low": None, "eta_high": None,
                    "eta_confidence": "none", "eta_note": "no images observed yet"}
        elapsed = max(1e-6, time.time() - job["started_at"])
        rate = done / elapsed
        remaining = max(0, expected - done)
        if rate <= 0:
            return {"eta_seconds": None, "eta_low": None, "eta_high": None,
                    "eta_confidence": "none", "eta_note": "rate not measurable yet"}
        eta = remaining / rate
        # The observed rate is noisy early on; widen the range until a decent
        # fraction of the plate is in.
        frac = done / expected if expected else 0
        band = 0.5 if frac < 0.1 else (0.25 if frac < 0.5 else 0.12)
        return {
            "eta_seconds": eta,
            "eta_low": eta * (1 - band),
            "eta_high": eta * (1 + band),
            "eta_confidence": "none" if done < 5 else ("low" if frac < 0.1 else "high"),
            "eta_note": f"{done}/{expected} images at {rate * 60:.1f}/min",
        }

    def _acquisition_progress(self, job: dict) -> int:
        baseline = int(job["params"].get("baseline_count") or 0)
        if job["status"] in ("done", "cancelled", "failed"):
            return int(job["completed"])
        return max(0, self.adapter.observed_count() - baseline)

    # --- queue view --------------------------------------------------------

    def queue(self) -> dict[str, Any]:
        """Both queues, with position, progress, ETA and estimated start."""
        return {
            "acquisition": self._lane(ACQUISITION),
            "segmentation": self._lane(SEGMENTATION),
            "global_pause": self.global_paused(),
            "adapter": self.adapter.describe(),
        }

    def _lane(self, kind: str) -> dict[str, Any]:
        jobs = self.db.list_jobs(kind, ("running", "paused", "queued"))
        recent = [j for j in self.db.list_jobs(kind, ("done", "failed", "cancelled"))][-5:]
        out: list[dict[str, Any]] = []
        # Estimated start accumulates the upper bound of everything ahead;
        # a queued job's wait is only as trustworthy as those estimates.
        ahead = 0.0
        ahead_known = True
        for position, job in enumerate(jobs):
            view = self._job_view(kind, job, position)
            if position == 0 and job["status"] in ("running", "paused"):
                view["starts_in_seconds"] = 0
            else:
                view["starts_in_seconds"] = ahead if ahead_known else None
            high = view.get("eta_high")
            if high is None:
                ahead_known = False
            else:
                ahead += high
            out.append(view)
        return {"jobs": out, "recent": [self._job_view(kind, j, None) for j in recent]}

    def _job_view(self, kind: str, job: dict, position: int | None) -> dict[str, Any]:
        view = {
            "id": job["id"], "kind": job["kind"], "status": job["status"],
            "params": job["params"], "created_at": job["created_at"],
            "started_at": job["started_at"], "finished_at": job["finished_at"],
            "failed": job["failed"], "error": job["error"],
            "position": position,
        }
        if kind == SEGMENTATION:
            pending = self.db.job_pending_count(job["id"])
            view["completed"] = job["completed"]
            view["total"] = job["total"]
            view["pending"] = pending
            view["label"] = f"Segmentation #{job['id']}"
            view.update(
                self.segmentation_eta(pending)
                if job["status"] in ("running", "queued")
                else {"eta_seconds": None, "eta_low": None, "eta_high": None,
                      "eta_confidence": "none",
                      "eta_note": "paused — backlog is growing" if job["status"] == "paused"
                      else job["status"]}
            )
        else:
            done = self._acquisition_progress(job)
            view["completed"] = done
            view["total"] = int(job["params"].get("expected_images") or job["total"])
            view["pending"] = max(0, view["total"] - done)
            view["label"] = job["params"].get("plate") or f"Acquisition #{job['id']}"
            view.update(
                self.acquisition_eta(job) if job["status"] == "running"
                else {"eta_seconds": None, "eta_low": None, "eta_high": None,
                      "eta_confidence": "none", "eta_note": job["status"]}
            )
            if job["status"] == "running" and view["total"] and done >= view["total"]:
                self.db.update_job(job["id"], status="done", completed=done,
                                   finished_at=time.time())
                view["status"] = "done"
        return view
