"""Self-test for the parts that fail silently: alerts and the acquisition lock.

No pytest dependency — run it directly:

    python -m station.selftest

Notification debounce is the main target. It is the one piece of logic whose
failure mode is discovering at 9am that a webhook fired 400 times overnight,
or not at all, and neither shows up in ordinary use.

The experiment instrumentation is here for the same reason: a phase label
that silently stops changing, or a report that invents a number from four
samples, produces a plausible-looking study that is wrong.
"""
import sys
import tempfile
import time
from pathlib import Path

from backend import experiment

from .config import load_config
from .db import Database
from .notify import DISK_LOW, FALLING_BEHIND, Notifier
from .runs import RunError, RunManager
from .acquisition import ObservedAcquisition

_results: list[tuple[bool, str]] = []


def ok(cond: bool, msg: str) -> None:
    _results.append((bool(cond), msg))
    print(f"  {'PASS' if cond else 'FAIL'}  {msg}")


def _cfg(tmp: Path, **over) -> object:
    argv = ["--db-path", str(tmp / "t.sqlite"), "--input-dir", str(tmp / "in"),
            "--output-dir", str(tmp / "out")]
    for k, v in over.items():
        argv += [f"--{k.replace('_', '-')}", str(v)]
    cfg = load_config(argv)
    cfg.webhook_url = "http://example.invalid"  # enabled; _post is stubbed below
    return cfg


def test_alerts(tmp: Path) -> None:
    print("alerts")
    cfg = _cfg(tmp, webhook_min_interval=600, falling_behind_seconds=5)
    db = Database(tmp / "a.sqlite")
    n = Notifier(cfg, db)
    sent: list[str] = []
    n._post = lambda text: (sent.append(text), True)[1]  # noqa: SLF001

    ok(n._fire(DISK_LOW, None, "first") is True, "first disk alert fires")
    ok(n._fire(DISK_LOW, None, "second") is False, "a repeat inside the cooldown is suppressed")
    db._x("UPDATE events SET ts=ts-700 WHERE kind=?", (DISK_LOW,))  # noqa: SLF001
    ok(n._fire(DISK_LOW, None, "third") is True, "it fires again once the cooldown expires")

    run = {"id": 1, "name": "R", "status": "done"}
    n.run_finished(run, {"segmented": 10, "failed": 0})
    n.run_finished(run, {"segmented": 10, "failed": 0})
    ok(sum(1 for s in sent if s.startswith("Run complete")) == 1,
       "run-complete fires exactly once per run")
    n.run_finished({"id": 2, "name": "R2", "status": "done"}, {"segmented": 1, "failed": 0})
    ok(sum(1 for s in sent if s.startswith("Run complete")) == 2, "a different run still alerts")

    n2 = Notifier(cfg, Database(tmp / "b.sqlite"))
    n2._post = lambda t: True  # noqa: SLF001
    blip = {"falling_behind": True, "backlog": 9, "backlog_trend_per_min": 3,
            "disk_free_bytes": None}
    n2.check(blip, None)
    n2.check(blip, None)
    ok(n2.db.last_event(FALLING_BEHIND) is None, "a short blip does not alert")
    n2._behind_since -= 10  # noqa: SLF001 - pretend it has held
    n2.check(blip, None)
    ok(n2.db.last_event(FALLING_BEHIND) is not None, "it alerts once it has held past the window")
    n2.check({"falling_behind": False, "backlog": 0, "disk_free_bytes": None}, None)
    ok(n2._behind_since is None, "recovering resets the window")  # noqa: SLF001

    n3 = Notifier(cfg, Database(tmp / "c.sqlite"))
    n3._post = lambda t: False  # noqa: SLF001
    n3._fire(DISK_LOW, None, "undeliverable")  # noqa: SLF001
    e = n3.db.last_event(DISK_LOW)
    ok(e is not None and e["delivered"] == 0,
       "an undelivered alert is still recorded as having happened")


class _NullWorker:
    def recent_per_image_seconds(self): return []
    def request_cancel(self, run_id): self.cancelled = run_id


def test_lock(tmp: Path) -> None:
    print("acquisition lock")
    cfg = _cfg(tmp)
    db = Database(tmp / "d.sqlite")
    adapter = ObservedAcquisition(tmp / "in", "test")
    runs = RunManager(db, _NullWorker(), adapter, cfg)

    first = runs.create("A", "P1", 100, "")
    ok(first["status"] == "running", "a run starts")
    try:
        runs.create("B", "P2", 10, "")
        ok(False, "a second concurrent run is refused")
    except RunError as e:
        ok(e.status == 409, "a second concurrent run is refused with 409")

    try:
        runs.pause(first["id"])
        ok(False, "pausing is refused without hardware control")
    except RunError as e:
        ok(e.status == 409, "pausing is refused with 409 when control is unavailable")

    runs.cancel(first["id"])
    ok(db.get_run(first["id"])["status"] == "cancelled", "a run can be cancelled")
    ok(runs.create("C", "P3", 5, "")["status"] == "running",
       "the lock is released once the previous run ends")

    ok(runs.set_global_pause(True) is True, "global pause is set")
    ok(Database(tmp / "d.sqlite").kv_get("segmentation_paused") is True,
       "global pause survives reopening the database")


def test_eta(tmp: Path) -> None:
    print("eta")
    cfg = _cfg(tmp)
    db = Database(tmp / "e.sqlite")

    class W:
        times: list[float] = []
        def recent_per_image_seconds(self): return self.times
        def request_cancel(self, r): pass

    w = W()
    runs = RunManager(db, w, ObservedAcquisition(tmp / "in", "t"), cfg)
    ok(runs.segmentation_eta(100)["eta_confidence"] == "none", "no ETA below three batches")
    w.times = [1.0, 1.1, 1.2]
    e = runs.segmentation_eta(100)
    ok(e["eta_low"] <= e["eta_seconds"] <= e["eta_high"], "the ETA range brackets the estimate")
    ok(e["eta_confidence"] == "low", "three batches give a low-confidence ETA")
    w.times = [1.0, 1.0, 1.05, 1.05, 1.1, 1.1]
    ok(runs.segmentation_eta(100)["eta_confidence"] == "high",
       "six tight batches give a high-confidence ETA")
    w.times = [0.5, 0.6, 1.0, 2.0, 8.0, 20.0]
    ok(runs.segmentation_eta(100)["eta_confidence"] == "low",
       "six scattered batches stay low-confidence")


def _sample(phase: str, bs: int, ts: float, rate: float, inter: float,
            gpu: float | None = 80.0) -> dict:
    return {
        "ts": ts, "phase": phase, "current_rate": rate,
        "inter_arrival_mean_s": inter, "inter_arrival_p95_s": inter * 1.5,
        "cpu_pct": 40.0, "ram_pct": 30.0, "backlog": 10,
        "disk_read_mbs": 120.0, "disk_write_mbs": 60.0,
        "gpu": None if gpu is None else {"gpu_util_pct": gpu, "vram_used_mb": 9000.0},
        "worker": {"batch_size": bs},
    }


def test_phase(tmp: Path) -> None:
    print("phase labels")
    db = Database(tmp / "p.sqlite")
    ok(experiment.get_phase(db) == experiment.UNLABELED, "the default phase is 'unlabeled'")
    ok(experiment.set_phase(db, "  solo sweep bs4 ") == "solo_sweep_bs4",
       "a phase label is slugged, not stored raw")
    ok(experiment.get_phase(db) == "solo_sweep_bs4", "the phase is read back")
    ok(Database(tmp / "p.sqlite").kv_get(experiment.PHASE_KEY) == "solo_sweep_bs4",
       "the phase survives reopening the database")
    ok(experiment.set_phase(db, "   ") == experiment.UNLABELED,
       "an empty label falls back to the default rather than labelling nothing")

    from .worker import StationWorker
    cfg = _cfg(tmp)
    cfg.dry_run = True
    w = StationWorker(cfg, db)
    ok(w.phase() == experiment.UNLABELED, "the worker reads the phase from the database")
    experiment.set_phase(db, "concurrent_bs8")
    ok(w.phase() == "concurrent_bs8", "a phase change reaches the worker with no restart")
    ok(w.snapshot()["phase"] == "concurrent_bs8", "the snapshot carries the phase")


def test_batch_size(tmp: Path) -> None:
    print("live batch size")
    from .worker import StationWorker
    db = Database(tmp / "bs.sqlite")
    cfg = _cfg(tmp, batch_size=16)
    w = StationWorker(cfg, db)
    ok(w.batch_size == 16, "the batch size starts at the configured value")
    ok(w.set_batch_size(4) == 4, "the batch size can be changed at runtime")
    ok(w.snapshot()["batch_size"] == 4, "the new batch size is visible in the snapshot")
    ok(w.set_batch_size(0) == 1 and w.set_batch_size(9999) == 256,
       "out-of-range batch sizes are clamped rather than accepted")

    w.set_batch_size(8)
    experiment.set_phase(db, "solo_sweep_bs8")
    w._record_batch(8, 4.0)  # noqa: SLF001
    rows = db.batches()
    ok(len(rows) == 1 and rows[0]["batch_size"] == 8 and rows[0]["phase"] == "solo_sweep_bs8",
       "a batch row records the batch size and phase that were in force")
    ok(abs(rows[0]["per_image_s"] - 0.5) < 1e-9, "the batch row records per-image seconds")
    ok(w.recent_per_image_seconds() == [0.5],
       "the same call still feeds the ETA, so timing and the report cannot disagree")

    w.set_batch_size(16)
    ok(db.batches()[0]["batch_size"] == 8,
       "changing the batch size does not rewrite batches already recorded")


def test_arrivals(tmp: Path) -> None:
    print("inter-arrival")
    from .worker import StationWorker
    w = StationWorker(_cfg(tmp), Database(tmp / "ia.sqlite"))
    ok(w.arrival_stats()["mean_s"] is None, "no inter-arrival below three gaps")
    now = time.time()
    for i in range(10):
        w._record_arrival(now - 100 + i * 2.0)  # noqa: SLF001
    st = w.arrival_stats()
    ok(abs(st["mean_s"] - 2.0) < 1e-6, "a steady 2 s cadence reads as 2 s")
    ok(st["p95_s"] >= st["mean_s"], "p95 is at or above the mean")
    w._record_arrival(now - 60)  # noqa: SLF001 - a 20 s stall
    ok(w.arrival_stats()["p95_s"] > 2.0, "a stall widens p95, which is the point of measuring it")
    ok(w.arrival_stats(window_s=1.0)["mean_s"] is None,
       "arrivals outside the window are excluded")


def test_report(tmp: Path) -> None:
    print("report aggregation")
    t0 = 1_700_000_000.0
    thin = [_sample("acquisition_only", 4, t0 + i, 0.0, 2.0) for i in range(5)]
    rep = experiment.build_report(thin)
    p = rep["phases"][0]
    ok(p["enough_data"] is False and "not enough data" in p["note"],
       "a phase under the sample floor reports 'not enough data'")
    ok("throughput" not in p, "and reports no computed-looking throughput at all")

    solo = [_sample("solo_sweep_bs4", 4, t0 + i, 1.0, 2.0) for i in range(30)]
    conc = [_sample("concurrent_bs4", 4, t0 + 100 + i, 0.6, 3.0) for i in range(30)]
    rep = experiment.build_report(solo + conc + thin)
    ok([x["phase"] for x in rep["phases"]] == ["solo_sweep_bs4", "concurrent_bs4",
                                               "acquisition_only"],
       "phases keep the order they were first seen in")
    s0 = rep["phases"][0]
    ok(abs(s0["throughput"]["mean"] - 1.0) < 1e-9, "throughput is averaged per phase")
    ok(s0["duration_s"] == 29.0, "the phase duration is its sample span")
    ok(s0["gpu_idle_fraction"] == 0.0, "a busy GPU has an idle fraction of zero")

    c = rep["contention"]
    ok(c["available"] and abs(c["percent_of_solo"] - 60.0) < 1e-6,
       "the headline is concurrent throughput as a percent of the solo baseline")
    ok(abs(c["contention_cost_pct"] - 40.0) < 1e-6, "the contention cost is its complement")
    ok(c["batch_size"] == 4, "the comparison is made at a batch size both phases ran")

    tun = rep["tuning"]
    ok(len(tun) == 1 and tun[0]["batch_size"] == 4, "the tuning table has one row per batch size")
    ok(tun[0]["inter_arrival_mean_s"] == 2.5,
       "the tuning row carries inter-arrival beside throughput")

    bs8 = [_sample("solo_sweep_bs8", 8, t0 + 300 + i, 1.4, 2.1) for i in range(30)]
    tun = experiment.build_report(solo + bs8)["tuning"]
    ok([t["batch_size"] for t in tun] == [4, 8], "batch sizes are listed in order")
    ok(tun[1]["images_per_s"] > tun[0]["images_per_s"], "the sweep is readable as a trend")

    mixed = ([_sample("mixed", 4, t0 + 500 + i, 1.0, 2.0) for i in range(15)]
             + [_sample("mixed", 8, t0 + 520 + i, 2.0, 2.0) for i in range(15)])
    ok(experiment.build_report(mixed)["tuning"] == [],
       "a phase that changed batch size mid-way is left out of the tuning table")

    idle = [_sample("solo_idle", 4, t0 + 700 + i, 0.1, 2.0, gpu=5.0) for i in range(30)]
    ok(experiment.build_report(idle)["phases"][0]["gpu_idle_fraction"] == 1.0,
       "a GPU below 20% counts as idle")
    nogpu = [_sample("solo_cpu", 4, t0 + 800 + i, 0.1, 2.0, gpu=None) for i in range(30)]
    ok(experiment.build_report(nogpu)["phases"][0]["gpu_idle_fraction"] is None,
       "with no GPU the idle fraction is absent, not zero")

    ok(experiment.contention(experiment.build_report(solo)["phases"])["available"] is False,
       "no headline without both a solo and a concurrent phase")

    md = experiment.to_markdown(experiment.build_report(solo + conc + thin))
    ok("60.0% of the solo baseline" in md, "the markdown leads with the contention number")
    ok("not enough data" in md, "the markdown repeats the honesty rule in the table")
    ok(md.count("|") > 20, "the markdown contains the tables")


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_alerts(tmp)
        test_lock(tmp)
        test_eta(tmp)
        test_phase(tmp)
        test_batch_size(tmp)
        test_arrivals(tmp)
        test_report(tmp)
    failed = [m for ok_, m in _results if not ok_]
    print(f"\n{len(_results) - len(failed)} passed, {len(failed)} failed")
    for m in failed:
        print(f"  - {m}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
