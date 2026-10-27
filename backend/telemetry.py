"""Telemetry sampler: one snapshot every `telemetry_interval` seconds."""
import logging
import shutil
import subprocess
import threading
import time
from typing import Any

import psutil

from .config import Config
from .db import Database
from .worker import Worker

log = logging.getLogger("telemetry")

RATE_WINDOW_S = 30.0       # window for "current" images/sec
TREND_WINDOW_S = 300.0     # 5 minutes, for the falling-behind test

GPU_QUERY = (
    "utilization.gpu,memory.used,memory.total,temperature.gpu,"
    "power.draw,utilization.memory"
)


def sample_gpu() -> dict[str, Any] | None:
    """nvidia-smi dump, or None where there is no NVIDIA GPU (e.g. macOS)."""
    if shutil.which("nvidia-smi") is None:
        return None
    try:
        p = subprocess.run(
            ["nvidia-smi", f"--query-gpu={GPU_QUERY}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        line = p.stdout.strip().splitlines()[0]
    except Exception as e:  # noqa: BLE001
        log.debug("nvidia-smi failed: %r", e)
        return None

    def num(x: str) -> float | None:
        x = x.strip()
        try:
            return float(x)
        except ValueError:
            return None

    f = [num(v) for v in line.split(",")]
    f += [None] * (6 - len(f))
    return {
        "gpu_util_pct": f[0],
        "vram_used_mb": f[1],
        "vram_total_mb": f[2],
        "gpu_temp_c": f[3],
        "gpu_power_w": f[4],
        "vram_util_pct": f[5],
    }


class Sampler:
    def __init__(self, cfg: Config, db: Database, worker: Worker, jobs=None):
        self.cfg = cfg
        self.db = db
        self.worker = worker
        self.jobs = jobs
        self.started_at = time.time()
        self.latest: dict[str, Any] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        psutil.cpu_percent(interval=None)  # prime the counter

    def start(self) -> None:
        self.sample()
        self._thread = threading.Thread(target=self._run, name="telemetry", daemon=True)
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

    # --- derived numbers ---------------------------------------------------

    def _backlog_trend(self) -> tuple[float, bool]:
        """Least-squares slope of backlog (images/min) over the last 5 min."""
        hist = self.db.history(TREND_WINDOW_S / 60.0)
        pts = [(h["ts"], h["backlog"]) for h in hist if "backlog" in h]
        if len(pts) < 10:
            return 0.0, False
        t0 = pts[0][0]
        xs = [(t - t0) for t, _ in pts]
        ys = [float(b) for _, b in pts]
        n = len(xs)
        mx = sum(xs) / n
        my = sum(ys) / n
        den = sum((x - mx) ** 2 for x in xs)
        if den == 0:
            return 0.0, False
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den  # images/sec
        per_min = slope * 60.0
        # Rising, and by enough to matter: more than one image/min of growth
        # with a backlog that is actually non-trivial.
        rising = per_min > 1.0 and ys[-1] > 5
        return per_min, rising

    def sample(self) -> dict[str, Any]:
        now = time.time()
        counts = self.db.counts()
        wk = self.worker.snapshot()

        current_rate = self.db.done_since(RATE_WINDOW_S) / RATE_WINDOW_S
        first = self.db.first_found_at() or self.started_at
        elapsed = max(1e-6, now - first)
        avg_rate = counts["segmented"] / elapsed

        rate_for_eta = current_rate if current_rate > 1e-6 else avg_rate
        eta_s = counts["backlog"] / rate_for_eta if rate_for_eta > 1e-6 else None

        try:
            du = shutil.disk_usage(
                self.cfg.input_dir if self.cfg.input_dir.exists() else self.cfg.input_dir.parent
            )
            disk = {"disk_free_bytes": du.free, "disk_total_bytes": du.total}
        except Exception:  # noqa: BLE001
            disk = {"disk_free_bytes": None, "disk_total_bytes": None}

        vm = psutil.virtual_memory()
        trend_per_min, rising = self._backlog_trend()

        snap: dict[str, Any] = {
            "ts": now,
            "elapsed_s": now - self.started_at,
            "acquisition_elapsed_s": now - first,
            **counts,
            "current_rate": current_rate,
            "avg_rate": avg_rate,
            "eta_s": eta_s,
            "backlog_trend_per_min": trend_per_min,
            "falling_behind": rising,
            "input_bytes": wk["input_bytes"],
            "cpu_pct": psutil.cpu_percent(interval=None),
            "ram_used_bytes": vm.total - vm.available,
            "ram_total_bytes": vm.total,
            "ram_pct": vm.percent,
            **disk,
            "gpu": sample_gpu(),
            "worker": wk,
        }
        if self.jobs is not None:
            # The queue rides along with telemetry so the dashboard gets both
            # from one WebSocket frame and cannot show them out of step.
            snap["queue"] = self.jobs.queue()
            snap.update({k: v for k, v in self.jobs.segmentation_eta(counts["backlog"]).items()
                         if k != "eta_seconds"})
        self.latest = snap
        # The queue is live state, not history; storing it would bloat every
        # telemetry row and it is never read back from there.
        self.db.add_sample(now, {k: v for k, v in snap.items() if k != "queue"})
        return snap
