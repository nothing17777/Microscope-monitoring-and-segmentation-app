"""Telemetry sampler. Samples are tagged with the run, so a finished run's
charts can be replayed long after it ended."""
import logging
import shutil
import threading
import time
from typing import Any

import psutil

from backend.experiment import UNLABELED
from backend.telemetry import RATE_WINDOW_S, TREND_WINDOW_S, DiskIO, sample_gpu

from .config import Config
from .db import Database

log = logging.getLogger("station.telemetry")


class Sampler:
    def __init__(self, cfg: Config, db: Database, worker, runs, notifier):
        self.cfg = cfg
        self.db = db
        self.worker = worker
        self.runs = runs
        self.notifier = notifier
        self.started_at = time.time()
        self.latest: dict[str, Any] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_status: dict[int, str] = {}
        self.disk_io = DiskIO(cfg.input_dir if cfg.input_dir.exists() else cfg.input_dir.parent)
        psutil.cpu_percent(interval=None)

    def start(self) -> None:
        self.sample()
        self._thread = threading.Thread(target=self._run, name="st-telemetry", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sample()
            except Exception:  # noqa: BLE001
                log.exception("telemetry sample failed")
            self._stop.wait(self.cfg.telemetry_interval)

    def _backlog_trend(self) -> tuple[float, bool]:
        """Least-squares slope of backlog (images/min) over the last 5 min."""
        pts = [(h["ts"], h["backlog"]) for h in self.db.history(TREND_WINDOW_S / 60.0)
               if "backlog" in h]
        if len(pts) < 10:
            return 0.0, False
        t0 = pts[0][0]
        xs = [t - t0 for t, _ in pts]
        ys = [float(b) for _, b in pts]
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        den = sum((x - mx) ** 2 for x in xs)
        if den == 0:
            return 0.0, False
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
        per_min = slope * 60.0
        return per_min, per_min > 1.0 and ys[-1] > 5

    def sample(self) -> dict[str, Any]:
        now = time.time()
        counts = self.db.counts()
        wk = self.worker.snapshot()
        run = self.db.active_acquisition()

        current_rate = self.db.done_since(RATE_WINDOW_S) / RATE_WINDOW_S
        first = self.db.first_found_at() or self.started_at
        avg_rate = counts["segmented"] / max(1e-6, now - first)

        try:
            d = self.cfg.input_dir if self.cfg.input_dir.exists() else self.cfg.input_dir.parent
            du = shutil.disk_usage(d)
            disk = {"disk_free_bytes": du.free, "disk_total_bytes": du.total}
        except Exception:  # noqa: BLE001
            disk = {"disk_free_bytes": None, "disk_total_bytes": None}

        vm = psutil.virtual_memory()
        trend, rising = self._backlog_trend()
        # The phase and batch size in force come from the worker snapshot, so
        # a sample can never claim a phase the worker was not actually in.
        arrivals = wk.get("arrivals") or {}

        snap: dict[str, Any] = {
            "ts": now,
            "elapsed_s": now - self.started_at,
            "phase": wk.get("phase", UNLABELED),
            "batch_size": wk.get("batch_size"),
            "inter_arrival_mean_s": arrivals.get("mean_s"),
            "inter_arrival_p95_s": arrivals.get("p95_s"),
            "inter_arrival_n": arrivals.get("n", 0),
            **counts,
            "current_rate": current_rate,
            "avg_rate": avg_rate,
            "backlog_trend_per_min": trend,
            "falling_behind": rising,
            "cpu_pct": psutil.cpu_percent(interval=None),
            "ram_used_bytes": vm.total - vm.available,
            "ram_total_bytes": vm.total,
            "ram_pct": vm.percent,
            **disk,
            **self.disk_io.sample(),
            "gpu": sample_gpu(),
            "worker": wk,
            "run_id": run["id"] if run else None,
        }
        snap.update({k: v for k, v in self.runs.segmentation_eta(counts["backlog"]).items()
                     if k != "eta_seconds"})
        snap["eta_s"] = self.runs.segmentation_eta(counts["backlog"])["eta_seconds"]

        self.latest = dict(snap)
        # Stored without the queue: it is live state, and keeping it would
        # bloat every row of the replay history.
        self.db.add_sample(now, run["id"] if run else None, snap)
        self.latest["queue"] = self.runs.queue()

        try:
            self.notifier.check(snap, run)
            self._watch_run_endings()
        except Exception:  # noqa: BLE001
            log.exception("alert check failed")
        return self.latest

    def _watch_run_endings(self) -> None:
        """Fire run-complete / run-failed once, as a run reaches a terminal state."""
        for r in self.db.list_runs(limit=20):
            prev = self._last_status.get(r["id"])
            if prev is None:
                self._last_status[r["id"]] = r["status"]
                continue
            if prev != r["status"] and r["status"] in ("done", "failed"):
                self.notifier.run_finished(r, self.db.counts(r["id"]))
            self._last_status[r["id"]] = r["status"]
