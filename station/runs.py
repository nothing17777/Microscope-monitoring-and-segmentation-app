"""Run lifecycle: the acquisition lock, pause/resume/cancel, ETAs, the queue.

The microscope is a single physical resource, so only one run may hold it.
That is enforced here, inside one mutex, not in the UI: two browsers pressing
Start at the same moment produce one run and one 409.
"""
import logging
import threading
import time
from typing import Any

from .acquisition import AcquisitionAdapter
from .db import Database
from .wells import grid
from .worker import GLOBAL_PAUSE_KEY

log = logging.getLogger("station.runs")

MIN_BATCHES_FOR_ETA = 3


class RunError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class RunManager:
    def __init__(self, db: Database, worker, adapter: AcquisitionAdapter, cfg):
        self.db = db
        self.worker = worker
        self.adapter = adapter
        self.cfg = cfg
        self._lock = threading.Lock()

    # --- lifecycle ---------------------------------------------------------

    def create(self, name: str, plate: str, expected: int, notes: str) -> dict:
        if expected <= 0:
            raise RunError("expected_image_count must be greater than zero")
        with self._lock:
            active = self.db.active_acquisition()
            if active:
                raise RunError(
                    f"run #{active['id']} ({active['name']}) is already {active['status']}; "
                    "the microscope can only do one run at a time", status=409)
            run_id = self.db.create_run(
                name=name, plate=plate, expected=expected, notes=notes,
                baseline=self.adapter.observed_count())
            if self.adapter.can_control:
                try:
                    self.adapter.start(self.db.get_run(run_id))
                except NotImplementedError as e:
                    raise RunError(str(e), status=409) from e
            # In Observed mode the run is already happening in LumaViewPro;
            # marking it running is what starts the clock for rate and ETA.
            self.db.update_run(run_id, status="running", started_at=time.time())
        return self.db.get_run(run_id)

    def pause(self, run_id: int) -> dict:
        run = self._require(run_id)
        if run["status"] not in ("running", "queued"):
            raise RunError(f"run #{run_id} is {run['status']}; nothing to pause")
        self._hardware(self.adapter.pause, run)
        self.db.update_run(run_id, status="paused")
        return self.db.get_run(run_id)

    def resume(self, run_id: int) -> dict:
        run = self._require(run_id)
        if run["status"] != "paused":
            raise RunError(f"run #{run_id} is {run['status']}; nothing to resume")
        self._hardware(self.adapter.resume, run)
        self.db.update_run(run_id, status="running")
        return self.db.get_run(run_id)

    def cancel(self, run_id: int) -> dict:
        run = self._require(run_id)
        if run["status"] in ("done", "failed", "cancelled"):
            raise RunError(f"run #{run_id} is already {run['status']}")
        if self.adapter.can_control:
            self._hardware(self.adapter.cancel, run)
        # Segmentation stops after the batch in flight. Images it never
        # reached stay pending, never failed.
        self.worker.request_cancel(run_id)
        self.db.update_run(run_id, status="cancelled", seg_status="cancelled",
                           finished_at=time.time())
        log.info("run #%s cancelled", run_id)
        return self.db.get_run(run_id)

    def _hardware(self, fn, run) -> None:
        if not self.adapter.can_control:
            raise RunError(
                f"acquisition control is unavailable: {self.adapter.reason}. "
                "Pause or stop the run in LumaViewPro itself.", status=409)
        try:
            fn(run)
        except NotImplementedError as e:
            raise RunError(str(e), status=409) from e

    def _require(self, run_id: int) -> dict:
        run = self.db.get_run(run_id)
        if not run:
            raise RunError(f"no run #{run_id}", status=404)
        return run

    # --- segmentation pausing ---------------------------------------------

    def set_run_segmentation(self, run_id: int, paused: bool) -> dict:
        run = self._require(run_id)
        if run["seg_status"] == "cancelled":
            raise RunError(f"segmentation for run #{run_id} was cancelled")
        self.db.update_run(run_id, seg_status="paused" if paused else "active")
        return self.db.get_run(run_id)

    def global_paused(self) -> bool:
        return bool(self.db.kv_get(GLOBAL_PAUSE_KEY, False))

    def set_global_pause(self, paused: bool) -> bool:
        self.db.kv_set(GLOBAL_PAUSE_KEY, bool(paused))
        log.info("segmentation worker globally %s", "PAUSED" if paused else "resumed")
        return bool(paused)

    # --- ETA ---------------------------------------------------------------

    def segmentation_eta(self, pending: int) -> dict[str, Any]:
        samples = sorted(self.worker.recent_per_image_seconds())
        n = len(samples)
        if n < MIN_BATCHES_FOR_ETA:
            return {"eta_seconds": None, "eta_low": None, "eta_high": None,
                    "eta_confidence": "none",
                    "eta_note": f"not enough data ({n}/{MIN_BATCHES_FOR_ETA} batches)"}
        if pending <= 0:
            return {"eta_seconds": 0, "eta_low": 0, "eta_high": 0,
                    "eta_confidence": "high", "eta_note": "queue empty"}
        lo, mid, hi = samples[int(0.25 * (n - 1))], samples[n // 2], samples[int(0.75 * (n - 1))]
        spread = hi / lo if lo > 0 else float("inf")
        return {"eta_seconds": pending * mid, "eta_low": pending * lo, "eta_high": pending * hi,
                "eta_confidence": "high" if n >= 6 and spread < 1.5 else "low",
                "eta_note": f"from {n} recent batch(es)"}

    def acquisition_eta(self, run: dict, captured: int) -> dict[str, Any]:
        expected = int(run["expected_image_count"] or 0)
        if not run["started_at"] or captured <= 0:
            return {"eta_seconds": None, "eta_low": None, "eta_high": None,
                    "eta_confidence": "none", "eta_note": "no images captured yet"}
        elapsed = max(1e-6, time.time() - run["started_at"])
        rate = captured / elapsed
        remaining = max(0, expected - captured)
        if rate <= 0:
            return {"eta_seconds": None, "eta_low": None, "eta_high": None,
                    "eta_confidence": "none", "eta_note": "rate not measurable yet"}
        eta = remaining / rate
        frac = captured / expected if expected else 0
        band = 0.5 if frac < 0.1 else (0.25 if frac < 0.5 else 0.12)
        return {"eta_seconds": eta, "eta_low": eta * (1 - band), "eta_high": eta * (1 + band),
                "eta_confidence": "none" if captured < 5 else ("low" if frac < 0.1 else "high"),
                "eta_note": f"{captured}/{expected} at {rate * 60:.1f}/min"}

    # --- views -------------------------------------------------------------

    def captured(self, run: dict) -> int:
        if run["status"] in ("done", "cancelled", "failed") or run.get("source_dir"):
            # A capture-folder run counts its own folder; the adapter counts
            # every TIFF under ProtocolData, past runs included.
            return self.db.counts(run["id"])["found"]
        observed = self.adapter.observed_count() - int(run["baseline_count"] or 0)
        # The scanner's own count is the floor: files it has already recorded
        # are captured no matter what the directory walk says this instant.
        return max(self.db.counts(run["id"])["found"], max(0, observed))

    def plate(self, run_id: int) -> dict[str, Any]:
        """96 wells, each in exactly one state, for the plate map."""
        counts = self.db.well_counts(run_id)
        results = self.db.well_results(run_id)
        cells = []
        for well in grid(self.cfg.plate_rows, self.cfg.plate_cols):
            c = counts.get(well, {})
            done, pending, failed = c.get("done", 0), c.get("pending", 0), c.get("failed", 0)
            total = done + pending + failed
            if total == 0:
                state = "empty"
            elif failed and not pending and not done:
                state = "failed"
            elif pending and done:
                state = "segmenting"
            elif pending:
                state = "acquired"
            else:
                state = "done"
            cells.append({"well": well, "state": state, "done": done,
                          "pending": pending, "failed": failed, "total": total,
                          "channels": results.get(well, {})})
        unknown = self.db.counts(run_id)["found"] - sum(c["total"] for c in cells)
        return {"rows": self.cfg.plate_rows, "cols": self.cfg.plate_cols,
                "cells": cells, "unplaced": max(0, unknown)}

    def run_view(self, run: dict, position: int | None = None) -> dict[str, Any]:
        counts = self.db.counts(run["id"])
        captured = self.captured(run)
        view = dict(run)
        view.update({
            "position": position,
            "captured": captured,
            "wells_done": sum(1 for c in self.plate(run["id"])["cells"] if c["state"] == "done"),
            "wells_total": self.cfg.plate_rows * self.cfg.plate_cols,
            "wells_seen": self.db.wells_seen(run["id"]),
            **counts,
        })
        view["acquisition_eta"] = (
            self.acquisition_eta(run, captured) if run["status"] == "running"
            else {"eta_seconds": None, "eta_low": None, "eta_high": None,
                  "eta_confidence": "none", "eta_note": run["status"]})
        if run["seg_status"] == "paused":
            seg = {"eta_seconds": None, "eta_low": None, "eta_high": None,
                   "eta_confidence": "none", "eta_note": "paused — backlog is growing"}
        elif run["seg_status"] == "cancelled":
            seg = {"eta_seconds": None, "eta_low": None, "eta_high": None,
                   "eta_confidence": "none", "eta_note": "cancelled"}
        else:
            seg = self.segmentation_eta(counts["backlog"])
        view["segmentation_eta"] = seg
        if run["started_at"]:
            end = run["finished_at"] or time.time()
            view["elapsed_s"] = end - run["started_at"]
        else:
            view["elapsed_s"] = None
        view["capture_rate"] = (
            captured / max(1e-6, time.time() - run["started_at"])
            if run["started_at"] and run["status"] == "running" else None)
        return view

    def queue(self) -> dict[str, Any]:
        active = self.db.list_runs(("running", "paused", "queued"))
        active.sort(key=lambda r: r["id"])
        acq, seg = [], []
        ahead, known = 0.0, True
        for i, run in enumerate(active):
            v = self.run_view(run, position=i)
            v["starts_in_seconds"] = 0 if i == 0 else (ahead if known else None)
            high = v["acquisition_eta"].get("eta_high")
            if high is None:
                known = False
            else:
                ahead += high
            acq.append(v)
            if run["seg_status"] != "cancelled" and (v["backlog"] or run["status"] == "running"):
                seg.append(v)
        return {
            "acquisition": acq,
            "segmentation": seg,
            "global_pause": self.global_paused(),
            "adapter": self.adapter.describe(),
        }
