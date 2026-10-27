"""Experiment instrumentation shared by both apps.

The capacity study needs every telemetry sample to say what was being tried
when it was taken (the phase) and at what batch size, and it needs those
samples aggregated per phase afterwards. None of that is specific to runs or
to files, so it lives here and both apps read it from their own `kv` table.

The honesty rule that governs ETAs governs this too: a phase with fewer than
MIN_SAMPLES samples reports "not enough data" rather than a mean of three
numbers that will be read as a measurement.
"""
from typing import Any

PHASE_KEY = "experiment_phase"
UNLABELED = "unlabeled"

# Below this, a phase is a fragment of a phase. At the default 2 s telemetry
# interval it is 40 seconds of samples.
MIN_SAMPLES = 20

# nvidia-smi utilisation under this counts as the GPU waiting for work.
GPU_IDLE_PCT = 20.0

# Phase-name conventions for the headline contention number. A phase is a
# baseline if its name contains "solo", and contended if it contains
# "concurrent"; anything else is reported but not compared.
SOLO_MARK = "solo"
CONCURRENT_MARK = "concurrent"


def clean_phase(name: str) -> str:
    """Normalise a phase label. Empty becomes the default."""
    out = "_".join(name.strip().split()).strip("_")[:64]
    return out or UNLABELED


def get_phase(db: Any) -> str:
    return clean_phase(str(db.kv_get(PHASE_KEY, UNLABELED)))


def set_phase(db: Any, name: str) -> str:
    phase = clean_phase(name)
    db.kv_set(PHASE_KEY, phase)
    return phase


# --- statistics --------------------------------------------------------------

def _nums(values: Any) -> list[float]:
    return [float(v) for v in values if isinstance(v, (int, float)) and v is not None]


def pct(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile. No interpolation: with 20-odd samples the
    interpolated value implies a resolution the sample count does not have."""
    xs = sorted(values)
    if not xs:
        return None
    i = min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))
    return xs[i]


def stats(values: Any) -> dict[str, float | None]:
    xs = _nums(values)
    if not xs:
        return {"mean": None, "p50": None, "p95": None, "min": None, "max": None, "n": 0}
    return {
        "mean": sum(xs) / len(xs),
        "p50": pct(xs, 0.50),
        "p95": pct(xs, 0.95),
        "min": min(xs),
        "max": max(xs),
        "n": len(xs),
    }


def _gpu(samples: list[dict], key: str) -> list[float]:
    return _nums([(s.get("gpu") or {}).get(key) for s in samples])


def _mode_batch(samples: list[dict]) -> int | None:
    counts: dict[int, int] = {}
    for s in samples:
        b = (s.get("worker") or {}).get("batch_size")
        if isinstance(b, int):
            counts[b] = counts.get(b, 0) + 1
    return max(counts, key=lambda k: counts[k]) if counts else None


def summarise_phase(name: str, samples: list[dict]) -> dict[str, Any]:
    """One row of the per-phase table. Honest about thin data."""
    ts = _nums([s.get("ts") for s in samples])
    row: dict[str, Any] = {
        "phase": name,
        "samples": len(samples),
        "started_at": min(ts) if ts else None,
        "ended_at": max(ts) if ts else None,
        "duration_s": (max(ts) - min(ts)) if len(ts) > 1 else 0.0,
        "batch_sizes": sorted({b for b in
                               ((s.get("worker") or {}).get("batch_size") for s in samples)
                               if isinstance(b, int)}),
        "batch_size": _mode_batch(samples),
    }
    if len(samples) < MIN_SAMPLES:
        row["enough_data"] = False
        row["note"] = f"not enough data ({len(samples)} of {MIN_SAMPLES} samples)"
        return row

    gpu_utils = _gpu(samples, "gpu_util_pct")
    row.update({
        "enough_data": True,
        "note": "",
        "throughput": stats([s.get("current_rate") for s in samples]),
        "inter_arrival": stats([s.get("inter_arrival_mean_s") for s in samples]),
        "inter_arrival_p95": stats([s.get("inter_arrival_p95_s") for s in samples]),
        "cpu_pct": stats([s.get("cpu_pct") for s in samples]),
        "ram_pct": stats([s.get("ram_pct") for s in samples]),
        "gpu_util_pct": stats(gpu_utils),
        "vram_used_mb": stats(_gpu(samples, "vram_used_mb")),
        "disk_read_mbs": stats([s.get("disk_read_mbs") for s in samples]),
        "disk_write_mbs": stats([s.get("disk_write_mbs") for s in samples]),
        "backlog": stats([s.get("backlog") for s in samples]),
        # Measured only where there is a GPU to be idle.
        "gpu_idle_fraction": (sum(1 for v in gpu_utils if v < GPU_IDLE_PCT) / len(gpu_utils)
                              if gpu_utils else None),
    })
    return row


def _mean_throughput(row: dict) -> float | None:
    return row.get("throughput", {}).get("mean") if row.get("enough_data") else None


def tuning_table(phases: list[dict]) -> list[dict[str, Any]]:
    """One row per batch size: throughput against acquisition impact.

    Only phases that held one batch size throughout are used — a row that
    averaged two batch sizes would hide the thing being looked for.
    """
    by_bs: dict[int, list[dict]] = {}
    for p in phases:
        if not p.get("enough_data") or len(p.get("batch_sizes") or []) != 1:
            continue
        by_bs.setdefault(p["batch_sizes"][0], []).append(p)
    out = []
    for bs in sorted(by_bs):
        rows = by_bs[bs]
        thr = _nums([_mean_throughput(r) for r in rows])
        ia = _nums([r["inter_arrival"]["mean"] for r in rows])
        ia95 = _nums([r["inter_arrival_p95"]["mean"] for r in rows])
        idle = _nums([r["gpu_idle_fraction"] for r in rows])
        out.append({
            "batch_size": bs,
            "phases": [r["phase"] for r in rows],
            "images_per_s": sum(thr) / len(thr) if thr else None,
            "inter_arrival_mean_s": sum(ia) / len(ia) if ia else None,
            "inter_arrival_p95_s": sum(ia95) / len(ia95) if ia95 else None,
            "gpu_idle_fraction": sum(idle) / len(idle) if idle else None,
            "duration_s": sum(r["duration_s"] for r in rows),
        })
    return out


def contention(phases: list[dict]) -> dict[str, Any]:
    """Concurrent throughput as a percentage of the solo baseline.

    Compared at a shared batch size where one exists, because comparing a
    concurrent phase at bs=8 against a solo phase at bs=4 measures the batch
    size, not the contention.
    """
    solo = [p for p in phases if p.get("enough_data") and SOLO_MARK in p["phase"]]
    conc = [p for p in phases if p.get("enough_data") and CONCURRENT_MARK in p["phase"]]
    if not solo or not conc:
        missing = []
        if not solo:
            missing.append(f"no phase named *{SOLO_MARK}* with enough data")
        if not conc:
            missing.append(f"no phase named *{CONCURRENT_MARK}* with enough data")
        return {"available": False, "note": "; ".join(missing)}

    shared = ({b for p in solo for b in p["batch_sizes"]}
              & {b for p in conc for b in p["batch_sizes"]})
    if shared:
        bs = max(shared)
        solo_m = [p for p in solo if bs in p["batch_sizes"]]
        conc_m = [p for p in conc if bs in p["batch_sizes"]]
        basis = f"at batch size {bs}"
    else:
        bs = None
        solo_m, conc_m = solo, conc
        basis = "across all batch sizes (no batch size was run both solo and concurrent)"

    s = _nums([_mean_throughput(p) for p in solo_m])
    c = _nums([_mean_throughput(p) for p in conc_m])
    if not s or not c or sum(s) == 0:
        return {"available": False, "note": "no usable throughput in those phases"}
    solo_rate = sum(s) / len(s)
    conc_rate = sum(c) / len(c)
    return {
        "available": True,
        "batch_size": bs,
        "basis": basis,
        "solo_images_per_s": solo_rate,
        "concurrent_images_per_s": conc_rate,
        "percent_of_solo": 100.0 * conc_rate / solo_rate,
        "contention_cost_pct": 100.0 * (1.0 - conc_rate / solo_rate),
        "solo_phases": [p["phase"] for p in solo_m],
        "concurrent_phases": [p["phase"] for p in conc_m],
        # Over 100% is not a contention measurement — more likely the two
        # phases were not comparable (a cold model in the solo phase, a
        # different image size, a backlog that was empty in one of them).
        "note": ("concurrent measured faster than solo; check the two phases were "
                 "comparable before reading this as a contention cost"
                 if conc_rate > solo_rate else ""),
    }


def build_report(samples: list[dict], meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Group samples by phase, in the order the phases were first seen."""
    order: list[str] = []
    groups: dict[str, list[dict]] = {}
    for s in samples:
        name = clean_phase(str(s.get("phase") or UNLABELED))
        if name not in groups:
            groups[name] = []
            order.append(name)
        groups[name].append(s)
    phases = [summarise_phase(n, groups[n]) for n in order]
    return {
        "generated_at": max((s["ts"] for s in samples if "ts" in s), default=None),
        "samples": len(samples),
        "min_samples_per_phase": MIN_SAMPLES,
        "phases": phases,
        "tuning": tuning_table(phases),
        "contention": contention(phases),
        **(meta or {}),
    }


# --- markdown ----------------------------------------------------------------

def _n(v: Any, d: int = 2) -> str:
    return "—" if v is None else f"{float(v):.{d}f}"


def _dur(s: Any) -> str:
    if s is None:
        return "—"
    s = int(s)
    return f"{s // 3600}h{(s % 3600) // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def to_markdown(rep: dict[str, Any]) -> str:
    L: list[str] = ["# Capacity and tuning report", ""]
    if rep.get("input_dir"):
        L += [f"Input: `{rep['input_dir']}`  ", f"Device: {rep.get('device', '—')} · "
              f"model: {rep.get('model', '—')}", ""]
    L += [f"{rep['samples']} telemetry samples. A phase needs "
          f"{rep['min_samples_per_phase']} samples to be reported.", ""]

    c = rep["contention"]
    L += ["## Headline — contention cost", ""]
    if c["available"]:
        L += [f"**Concurrent segmentation runs at {_n(c['percent_of_solo'], 1)}% of the solo "
              f"baseline** ({basis_s(c)}).", "",
              f"- solo: {_n(c['solo_images_per_s'], 3)} images/s "
              f"({', '.join(c['solo_phases'])})",
              f"- concurrent: {_n(c['concurrent_images_per_s'], 3)} images/s "
              f"({', '.join(c['concurrent_phases'])})",
              f"- contention cost: {_n(c['contention_cost_pct'], 1)}%", ""]
    else:
        L += [f"Not enough data: {c['note']}.", ""]

    L += ["## Per phase", "",
          "| phase | batch | duration | img/s mean | p50 | p95 | inter-arr mean s | "
          "inter-arr p95 s | CPU mean/max | RAM mean/max | GPU mean/max | VRAM mean/max MB | "
          "disk R mean/max MB/s | disk W mean/max MB/s | GPU idle |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for p in rep["phases"]:
        if not p.get("enough_data"):
            L.append(f"| {p['phase']} | {p.get('batch_size') or '—'} | "
                     f"{_dur(p['duration_s'])} | " + " | ".join(["not enough data"] + ["—"] * 11)
                     + " |")
            continue
        def mm(key: str, d: int = 1) -> str:
            st = p[key]
            return f"{_n(st['mean'], d)} / {_n(st['max'], d)}"
        L.append(
            f"| {p['phase']} | {p['batch_size']} | {_dur(p['duration_s'])} | "
            f"{_n(p['throughput']['mean'], 3)} | {_n(p['throughput']['p50'], 3)} | "
            f"{_n(p['throughput']['p95'], 3)} | {_n(p['inter_arrival']['mean'], 2)} | "
            f"{_n(p['inter_arrival_p95']['mean'], 2)} | {mm('cpu_pct')} | {mm('ram_pct')} | "
            f"{mm('gpu_util_pct')} | {mm('vram_used_mb', 0)} | {mm('disk_read_mbs', 1)} | "
            f"{mm('disk_write_mbs', 1)} | "
            f"{'—' if p['gpu_idle_fraction'] is None else _n(100 * p['gpu_idle_fraction'], 0) + '%'} |")

    L += ["", "## Tuning — one row per batch size", "",
          "| batch size | img/s | inter-arrival mean s | inter-arrival p95 s | GPU idle | "
          "time at this size | phases |", "|---|---|---|---|---|---|---|"]
    if not rep["tuning"]:
        L.append("| — | no phase held a single batch size for long enough | | | | | |")
    for t in rep["tuning"]:
        L.append(f"| {t['batch_size']} | {_n(t['images_per_s'], 3)} | "
                 f"{_n(t['inter_arrival_mean_s'], 2)} | {_n(t['inter_arrival_p95_s'], 2)} | "
                 f"{'—' if t['gpu_idle_fraction'] is None else _n(100 * t['gpu_idle_fraction'], 0) + '%'} | "
                 f"{_dur(t['duration_s'])} | {', '.join(t['phases'])} |")
    L += ["", "Higher img/s with a flat inter-arrival time is free throughput. The batch size "
          "where inter-arrival starts to rise is where segmentation begins taking the disk or "
          "CPU acquisition needs.", ""]
    return "\n".join(L)


def basis_s(c: dict) -> str:
    return c.get("basis", "")
