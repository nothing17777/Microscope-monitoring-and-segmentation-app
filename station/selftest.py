"""Self-test for the parts that fail silently: alerts and the acquisition lock.

No pytest dependency — run it directly:

    python -m station.selftest

Notification debounce is the main target. It is the one piece of logic whose
failure mode is discovering at 9am that a webhook fired 400 times overnight,
or not at all, and neither shows up in ordinary use.
"""
import sys
import tempfile
from pathlib import Path

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


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_alerts(tmp)
        test_lock(tmp)
        test_eta(tmp)
    failed = [m for ok_, m in _results if not ok_]
    print(f"\n{len(_results) - len(failed)} passed, {len(failed)} failed")
    for m in failed:
        print(f"  - {m}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
