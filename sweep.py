#!/usr/bin/env python3
"""Drive a batch-size sweep against a running station, hands-off.

The point is the solo sweep: start this before the microscope does anything,
and by the time acquisition begins there is already a baseline at every batch
size, each one labelled, with no one standing at the keyboard between steps.

    python sweep.py --sizes 1,2,4,8,16 --hold 10m --prefix solo_sweep

That sets phase=solo_sweep_bs1 and batch size 1, waits ten minutes, moves to
bs2, and so on. Add --settle to discard the first part of each step, where
the previous batch size is still draining out of the numbers.

A plain label with no batch-size change is a step too:

    python sweep.py --phases acquisition_only=20m,concurrent_bs8=40m --batch-size 8

Nothing here reads the results: it only sets the labels. The report at
/api/report.md is what reads them back.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request


def parse_duration(text: str) -> float:
    """'90', '10m', '1h30m' -> seconds."""
    t = text.strip().lower()
    if not t:
        raise ValueError("empty duration")
    if t.replace(".", "", 1).isdigit():
        return float(t)
    total, num = 0.0, ""
    for ch in t:
        if ch.isdigit() or ch == ".":
            num += ch
        elif ch in "hms":
            if not num:
                raise ValueError(f"bad duration {text!r}")
            total += float(num) * {"h": 3600, "m": 60, "s": 1}[ch]
            num = ""
        else:
            raise ValueError(f"bad duration {text!r}")
    if num:
        raise ValueError(f"bad duration {text!r}: trailing {num!r} has no unit")
    return total


class Station:
    def __init__(self, base: str, token: str | None, dry: bool):
        self.base = base.rstrip("/")
        self.token = token
        self.dry = dry

    def post(self, path: str, body: dict) -> dict:
        if self.dry:
            print(f"    [dry-run] POST {path} {body}")
            return {}
        req = urllib.request.Request(
            f"{self.base}{path}", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     **({"X-Auth-Token": self.token} if self.token else {})},
            method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"{}")

    def get(self, path: str) -> dict:
        with urllib.request.urlopen(f"{self.base}{path}", timeout=15) as r:
            return json.loads(r.read() or b"{}")


def steps_from_args(a: argparse.Namespace) -> list[tuple[str, int | None, float]]:
    """(phase, batch size or None, seconds) in the order they will run."""
    out: list[tuple[str, int | None, float]] = []
    if a.phases:
        for part in a.phases.split(","):
            name, _, dur = part.partition("=")
            if not dur:
                raise SystemExit(f"--phases entry {part!r} needs a duration, e.g. {name}=20m")
            out.append((name.strip(), a.batch_size, parse_duration(dur)))
    if a.sizes:
        hold = parse_duration(a.hold)
        for s in a.sizes.split(","):
            n = int(s)
            out.append((f"{a.prefix}_bs{n}", n, hold))
    if not out:
        raise SystemExit("nothing to do: pass --sizes or --phases")
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base", default=os.environ.get("MM_ST_BASE", "http://localhost:8100"))
    p.add_argument("--token", default=os.environ.get("MM_ST_AUTH_TOKEN") or None)
    p.add_argument("--sizes", help="comma-separated batch sizes to sweep, e.g. 1,2,4,8,16")
    p.add_argument("--hold", default="10m", help="time at each batch size (default 10m)")
    p.add_argument("--prefix", default="solo_sweep", help="phase name prefix for --sizes")
    p.add_argument("--phases", help="explicit steps, e.g. acquisition_only=20m,concurrent_bs8=40m")
    p.add_argument("--batch-size", type=int, default=None,
                   help="batch size to set for --phases steps (left alone if omitted)")
    p.add_argument("--settle", default="0",
                   help="label each step 'NAME_settle' for this long first, so the "
                        "previous batch size draining out does not land in the step")
    p.add_argument("--final-phase", default="unlabeled",
                   help="phase to leave behind when the sweep ends")
    p.add_argument("--dry-run", action="store_true",
                   help="print the plan and exit; change nothing and wait for nothing")
    a = p.parse_args(argv)

    steps = steps_from_args(a)
    settle = parse_duration(a.settle)
    st = Station(a.base, a.token, a.dry_run)

    if not a.dry_run:
        try:
            cfg = st.get("/api/config")
        except urllib.error.URLError as e:
            print(f"cannot reach the station at {a.base}: {e}", file=sys.stderr)
            return 2
        if cfg.get("auth_required") and not a.token:
            print("the station requires a token; pass --token or set MM_ST_AUTH_TOKEN",
                  file=sys.stderr)
            return 2
        if cfg.get("dry_run"):
            print("WARNING: the station is in --dry-run, so these are not Cellpose timings.")

    total = sum(d for _, _, d in steps) + settle * len(steps)
    print(f"{len(steps)} steps, {total / 60:.0f} minutes total, against {a.base}")
    for name, bs, dur in steps:
        print(f"  {name:28s} batch={bs if bs is not None else 'unchanged':>9} "
              f"{dur / 60:.0f}m" + (f" (+{settle / 60:.0f}m settle)" if settle else ""))
    print()
    if a.dry_run:
        return 0

    try:
        for i, (name, bs, dur) in enumerate(steps, 1):
            if bs is not None:
                st.post("/api/config/batch-size", {"n": bs})
            if settle:
                st.post("/api/phase", {"name": f"{name}_settle"})
                print(f"[{time.strftime('%H:%M:%S')}] {i}/{len(steps)} {name}: settling "
                      f"{settle / 60:.0f}m")
                time.sleep(settle)
            st.post("/api/phase", {"name": name})
            print(f"[{time.strftime('%H:%M:%S')}] {i}/{len(steps)} {name} "
                  f"(batch {bs if bs is not None else 'unchanged'}) for {dur / 60:.0f}m")
            time.sleep(dur)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
    finally:
        # Never leave the next thing that happens mislabelled as part of a
        # sweep step that has stopped running.
        try:
            st.post("/api/phase", {"name": a.final_phase})
            print(f"phase left at {a.final_phase!r}")
        except Exception as e:  # noqa: BLE001
            print(f"could not reset the phase: {e}", file=sys.stderr)
    print(f"\ndone. Report: {a.base}/api/report.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
