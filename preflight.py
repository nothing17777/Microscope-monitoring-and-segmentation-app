#!/usr/bin/env python3
"""Fail fast, before a two-hour acquisition run.

Checks the things that actually break on a fresh workstation: a torch wheel
that doesn't support the card, the undeclared dinov3 dependency, cpdino
weights that aren't cached yet, nvidia-smi, and whether the input and output
directories are really readable/writable.

    python preflight.py --input-dir /data/lumaviewpro/output
    python preflight.py --input-dir ... --load-model     # also builds cpdino
    python preflight.py --input-dir ... --segment        # also segments one image
    python preflight.py --input-dir ... --throughput     # ...and times a full-size batch
    python preflight.py --study                          # exercise a running station's
                                                         # experiment instrumentation

Exit code 0 if nothing FAILed (warnings are allowed), 1 otherwise.
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
COLOR = {PASS: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m"}
RESET = "\033[0m"
results: list[tuple[str, str, str]] = []


def report(status: str, name: str, detail: str = "") -> str:
    results.append((status, name, detail))
    c = COLOR[status] if sys.stdout.isatty() else ""
    r = RESET if sys.stdout.isatty() else ""
    print(f"  {c}{status}{r}  {name}" + (f"\n          {detail}" if detail else ""), flush=True)
    return status


def section(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}", flush=True)


# --- checks -----------------------------------------------------------------

def check_python() -> None:
    v = sys.version_info
    detail = f"{sys.version.split()[0]} at {sys.executable}"
    if v < (3, 10):
        report(FAIL, "python >= 3.10", detail)
    elif v >= (3, 13):
        report(WARN, "python version", detail + " — torch/cellpose wheels may not exist yet")
    else:
        report(PASS, "python version", detail)


def check_torch() -> object | None:
    try:
        import torch
    except Exception as e:  # noqa: BLE001
        report(FAIL, "import torch", f"{e!r}\n          pip install torch")
        return None
    report(PASS, "import torch", f"{torch.__version__} (built against CUDA {torch.version.cuda})")
    return torch


def check_device(torch) -> str:
    """Pick the device the app will pick, and prove a kernel actually runs."""
    if torch.cuda.is_available():
        device = "cuda"
    elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        device = "mps"
    else:
        device = "cpu"

    if device == "cpu":
        report(WARN, "compute device", "cpu — no CUDA and no MPS; segmentation will be slow")
    else:
        report(PASS, "compute device", device)

    if device == "cuda":
        name = torch.cuda.get_device_name(0)
        cap = torch.cuda.get_device_capability(0)
        sm = f"sm_{cap[0]}{cap[1]}"
        arches = list(torch.cuda.get_arch_list())
        total_gb = torch.cuda.get_device_properties(0).total_memory / 1024**3
        report(PASS, "cuda device", f"{name} ({sm}, {total_gb:.1f} GB), {torch.cuda.device_count()} visible")
        # The RTX 5090 trap: the wheel imports fine but has no kernels for the
        # card, and only dies at the first real launch.
        if not arches:
            report(WARN, "wheel supports this card",
                   "torch reports an empty arch list; cannot confirm — rely on the kernel launch below")
        elif sm in arches:
            report(PASS, "wheel supports this card", f"{sm} in torch arch list")
        else:
            report(
                FAIL, "wheel supports this card",
                f"{sm} NOT in torch arch list {arches}\n"
                "          pip install torch --index-url https://download.pytorch.org/whl/cu128",
            )

    if device != "cpu":
        try:
            t0 = time.time()
            a = torch.randn(512, 512, device=device)
            b = (a @ a).sum().item()
            if device == "cuda":
                torch.cuda.synchronize()
            assert b == b  # not NaN
            report(PASS, "kernel launch", f"512x512 matmul on {device} in {time.time() - t0:.2f}s")
        except Exception as e:  # noqa: BLE001
            report(FAIL, "kernel launch", f"{type(e).__name__}: {e}")
    return device


def check_nvidia_smi() -> None:
    if shutil.which("nvidia-smi") is None:
        report(WARN, "nvidia-smi", "not on PATH — GPU telemetry will read None (fine off-NVIDIA)")
        return
    try:
        p = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version,memory.total,utilization.gpu,"
             "temperature.gpu,power.draw", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:  # noqa: BLE001
        report(FAIL, "nvidia-smi", repr(e))
        return
    if p.returncode != 0:
        report(FAIL, "nvidia-smi", (p.stderr or p.stdout).strip()[:300])
        return
    line = p.stdout.strip().splitlines()[0]
    report(PASS, "nvidia-smi", line)
    if "[N/A]" in line:
        report(WARN, "nvidia-smi fields", "some fields report [N/A]; those tiles will show '—'")


def check_dinov3() -> None:
    try:
        from dinov3.hub.backbones import dinov3_vitl16  # noqa: F401
    except Exception as e:  # noqa: BLE001
        report(
            FAIL, "import dinov3",
            f"{type(e).__name__}: {e}\n"
            "          cellpose imports this under a bare try/except, so cpdino only\n"
            "          fails later with NameError: dinov3_vitl16. Install it with:\n"
            "          pip install git+https://github.com/facebookresearch/dinov3",
        )
        return
    report(PASS, "import dinov3", "DINOv3 backbone available (cpdino needs it)")


def check_cellpose() -> None:
    try:
        import cellpose
    except Exception as e:  # noqa: BLE001
        report(FAIL, "import cellpose", repr(e))
        return
    ver = getattr(cellpose, "version", None) or getattr(cellpose, "__version__", "?")
    report(PASS, "import cellpose", f"version {ver}")


def check_model_cache(model: str) -> None:
    """cpdino weights are a ~1.1 GB download; don't discover that mid-run."""
    cache = Path(os.environ.get("CELLPOSE_LOCAL_MODELS_PATH", Path.home() / ".cellpose"))
    if not cache.exists():
        report(WARN, "model cache", f"{cache} does not exist — weights download on first batch")
        return
    files = [p for p in cache.rglob("*") if p.is_file() and p.stat().st_size > 10_000_000]
    total = sum(p.stat().st_size for p in files)
    if not files:
        report(WARN, "model cache", f"{cache} has no large weight files — '{model}' will download")
    else:
        names = ", ".join(sorted(p.name for p in files)[:4])
        report(PASS, "model cache", f"{cache}: {len(files)} file(s), {total / 1024**3:.2f} GB ({names})")
    if not os.access(cache, os.W_OK):
        report(FAIL, "model cache writable", f"{cache} is not writable by {os.getenv('USER', '?')}")


def check_dirs(input_dir: Path, output_dir: Path) -> None:
    if not input_dir.exists():
        report(FAIL, "input dir exists", f"{input_dir} — create it, or point --input-dir at LumaViewPro's output")
    elif not input_dir.is_dir():
        report(FAIL, "input dir is a directory", str(input_dir))
    elif not os.access(input_dir, os.R_OK | os.X_OK):
        report(FAIL, "input dir readable", f"{input_dir} not readable by {os.getenv('USER', '?')}")
    else:
        n = sum(1 for p in input_dir.rglob("*") if p.suffix.lower() in (".tif", ".tiff"))
        report(PASS, "input dir", f"{input_dir} — {n} TIFF(s) already present")

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=output_dir, prefix=".preflight-", delete=True):
            pass
        report(PASS, "output dir writable", str(output_dir))
    except Exception as e:  # noqa: BLE001
        report(FAIL, "output dir writable", f"{output_dir}: {e!r}")

    # Walk up to the nearest existing ancestor, so a missing input dir
    # reports once rather than cascading into a disk-free failure too.
    probe = input_dir
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        du = shutil.disk_usage(probe)
        free_gb = du.free / 1024**3
        detail = f"{free_gb:.1f} GB free of {du.total / 1024**3:.1f} GB on {probe}"
        # ~7700 images at 8 MB each is roughly 60 GB of TIFFs, plus masks.
        if free_gb < 80:
            report(WARN, "disk free", detail + " — a full 96-well run is ~60 GB of TIFFs")
        else:
            report(PASS, "disk free", detail)
    except Exception as e:  # noqa: BLE001
        report(FAIL, "disk free", repr(e))

    if shutil.which("findmnt"):
        out = subprocess.run(["findmnt", "-T", str(probe), "-o", "SOURCE,FSTYPE,TARGET"],
                             capture_output=True, text=True).stdout.strip()
        tail = out.splitlines()[-1] if out else "?"
        report(PASS, "input volume", tail)
        if any(fs in out for fs in (" nfs", " cifs", " smb", " fuse")):
            report(WARN, "input volume type", "network filesystem — size-stability polling matters more here")


def check_port(port: int) -> None:
    s = socket.socket()
    try:
        s.bind(("0.0.0.0", port))
        report(PASS, "port free", f"0.0.0.0:{port}")
    except OSError as e:
        report(FAIL, "port free", f"0.0.0.0:{port} — {e}")
    finally:
        s.close()


def check_frontend() -> None:
    dist = Path(__file__).resolve().parent / "frontend" / "dist" / "index.html"
    if dist.is_file():
        report(PASS, "frontend built", str(dist.parent))
    else:
        report(WARN, "frontend built",
               "frontend/dist missing — run `cd frontend && npm install && npm run build`,\n"
               "          or use the Vite dev server on :5173")


def check_model_load(model: str, device: str, segment: bool, output_dir: Path) -> object | None:
    """Actually construct cpdino, and optionally segment one small image."""
    try:
        import numpy as np
        import torch
        from cellpose.models import CellposeModel
    except Exception as e:  # noqa: BLE001
        report(FAIL, f"load model '{model}'", repr(e))
        return None
    try:
        t0 = time.time()
        m = CellposeModel(gpu=device != "cpu", pretrained_model=model, device=torch.device(device))
        report(PASS, f"load model '{model}'", f"{type(m).__name__} on {device} in {time.time() - t0:.1f}s")
    except Exception as e:  # noqa: BLE001
        report(FAIL, f"load model '{model}'", f"{type(e).__name__}: {e}")
        return None
    if not segment:
        return m
    try:
        rng = np.random.default_rng(0)
        im = rng.integers(200, 900, size=(256, 256), dtype=np.uint16)
        yy, xx = np.mgrid[0:256, 0:256]
        for cy, cx in [(60, 60), (60, 190), (190, 60), (190, 190), (125, 125)]:
            im[(yy - cy) ** 2 + (xx - cx) ** 2 < 400] += 6000
        t0 = time.time()
        masks = m.eval([im], batch_size=1)[0]
        mask = masks[0] if isinstance(masks, list) else masks
        mask = np.asarray(mask)
        n = int(np.unique(mask).size - (1 if 0 in mask else 0))
        report(PASS, "segment one image",
               f"256x256 -> mask {mask.shape} {mask.dtype}, {n} object(s), {time.time() - t0:.1f}s")
    except Exception as e:  # noqa: BLE001
        report(FAIL, "segment one image", f"{type(e).__name__}: {e}")
    return m


def _hms(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    h, rem = divmod(seconds, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else (f"{m}m{sec:02d}s" if m else f"{sec}s")


def _image_shape(input_dir: Path, fallback: int) -> tuple[tuple[int, int], str]:
    """Dimensions of the first real TIFF, so the estimate uses the real size."""
    try:
        import tifffile
        for suffix in ("*.tif", "*.tiff", "*.TIF", "*.TIFF"):
            for path in sorted(input_dir.rglob(suffix)):
                with tifffile.TiffFile(str(path)) as tf:
                    shape = tf.pages[0].shape
                if len(shape) >= 2:
                    return (int(shape[-2]), int(shape[-1])), path.name
    except Exception:  # noqa: BLE001
        pass
    return (fallback, fallback), "no TIFF found — assumed"


def check_plate_plan(plan: dict, shape: tuple[int, int], shape_src: str) -> dict:
    """The arithmetic of the planned run: how many images, how fast they land."""
    total = plan["wells"] * plan["tiles"] * plan["slices"] * plan["channels"]
    run_s = plan["hours"] * 3600
    rate = total / run_s
    report(PASS, "plate plan",
           f"{plan['wells']} wells x {plan['tiles']} tiles x {plan['slices']} slices "
           f"x {plan['channels']} channels = {total:,} images in {plan['hours']:g}h")
    report(PASS, "acquisition rate",
           f"{rate:.2f} images/s — one every {1 / rate:.2f}s; "
           f"segmentation must stay under that to hold backlog flat")
    report(PASS, "image size", f"{shape[0]}x{shape[1]} (from {shape_src})")
    return {"total": total, "run_s": run_s, "rate": rate}


def check_throughput(m, device: str, batch_size: int, shape: tuple[int, int], plan: dict) -> None:
    """Time one full-size batch, then project the backlog over the whole run."""
    try:
        import numpy as np
        import torch
    except Exception as e:  # noqa: BLE001
        report(FAIL, "throughput", repr(e))
        return
    try:
        rng = np.random.default_rng(0)
        h, w = shape
        im = rng.integers(200, 900, size=(h, w), dtype=np.uint16)
        yy, xx = np.mgrid[0:h, 0:w]
        for cy in range(h // 10, h, h // 10):
            for cx in range(w // 10, w, w // 10):
                im[(yy - cy) ** 2 + (xx - cx) ** 2 < (min(h, w) // 50) ** 2] += 6000
        batch = [im] * batch_size
        m.eval(batch[:1], batch_size=1)  # warm up; first call pays one-off costs
        t0 = time.time()
        m.eval(batch, batch_size=batch_size)
        per_image = (time.time() - t0) / batch_size
    except Exception as e:  # noqa: BLE001
        report(FAIL, "throughput", f"{type(e).__name__}: {e}")
        return

    seg_rate = 1 / per_image
    report(PASS, "measured throughput",
           f"{per_image:.2f}s/image on {device} at {h}x{w}, batch of {batch_size} "
           f"({seg_rate:.2f} images/s)")

    headroom = seg_rate / plan["rate"]
    seg_total_s = plan["total"] * per_image
    if headroom >= 1:
        report(PASS, "keeps up with acquisition",
               f"{headroom:.1f}x headroom — backlog stays flat; "
               f"{_hms(seg_total_s)} of GPU time across a {_hms(plan['run_s'])} run")
    else:
        backlog = plan["total"] - int(plan["run_s"] * seg_rate)
        catch_up = backlog * per_image
        report(WARN, "keeps up with acquisition",
               f"{headroom:.2f}x — segmentation is {1 / headroom:.1f}x too slow. "
               f"Backlog at the end of acquisition: ~{backlog:,} images,\n"
               f"          cleared {_hms(catch_up)} later (total {_hms(plan['run_s'] + catch_up)}). "
               f"The banner will read SEGMENTATION FALLING BEHIND.\n"
               f"          To hold it flat, the plan must shrink to "
               f"~{int(plan['run_s'] * seg_rate):,} images.")


# --- the capacity study -----------------------------------------------------

# Deliberately recognisable in the report afterwards, and not a name anyone
# would use for a real phase.
PROBE_PHASE = "preflight_check"
PROBE_BATCH = 2
SAMPLE_WAIT_S = 20.0


class _Station:
    def __init__(self, base: str, token: str | None):
        self.base = base.rstrip("/")
        self.token = token

    def get(self, path: str, raw: bool = False):
        with urllib.request.urlopen(f"{self.base}{path}", timeout=15) as r:
            body = r.read()
        return body.decode() if raw else json.loads(body or b"{}")

    def post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base}{path}", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     **({"X-Auth-Token": self.token} if self.token else {})},
            method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"{}")


def _await_sample(st: _Station, want_phase: str) -> dict | None:
    """Wait for a telemetry sample taken after the phase change, not before it."""
    deadline = time.time() + SAMPLE_WAIT_S
    while time.time() < deadline:
        try:
            snap = st.get("/api/status")
        except Exception:  # noqa: BLE001
            time.sleep(1.0)
            continue
        if snap.get("phase") == want_phase:
            return snap
        time.sleep(1.0)
    return None


def check_study(base: str, token: str | None, force: bool) -> None:
    """Exercise phase labels, the live batch size and the report, then undo it.

    This writes to a running station: it sets a phase and a batch size and
    puts both back. Everything it does lands in the study's own data, so it
    refuses to run while the microscope is actually acquiring unless forced.
    """
    st = _Station(base, token)
    try:
        cfg = st.get("/api/config")
    except urllib.error.HTTPError as e:
        report(FAIL, "station reachable", f"{base} -> HTTP {e.code}")
        return
    except Exception as e:  # noqa: BLE001
        report(FAIL, "station reachable",
               f"{base}: {type(e).__name__}: {e}\n"
               "          start it with: python -m station.main --input-dir ... "
               "(or pass --station-url)")
        return
    report(PASS, "station reachable",
           f"{base} — {cfg.get('model')} on {cfg.get('device')}, input {cfg.get('input_dir')}")
    if cfg.get("dry_run"):
        report(WARN, "station is segmenting for real",
               "the station is running --dry-run: masks are a threshold and the timings "
               "in the report are not Cellpose timings")

    if cfg.get("auth_required") and not token:
        report(FAIL, "study auth",
               "the station requires a token and none was given\n"
               "          pass --station-token, or export MM_ST_AUTH_TOKEN")
        return

    # Changing the phase mid-acquisition mislabels the real data, which is
    # the one thing here that cannot be undone afterwards.
    try:
        running = [r for r in st.get("/api/runs?limit=5").get("runs", [])
                   if r.get("status") == "running" and r.get("name") != "Unassigned"]
    except Exception:  # noqa: BLE001
        running = []
    if running and not force:
        report(FAIL, "safe to probe",
               f"run #{running[0]['id']} ({running[0]['name']}) is acquiring; this check "
               "would relabel its samples\n          run it before the acquisition starts, "
               "or pass --force if you really mean to")
        return
    if running:
        report(WARN, "safe to probe",
               f"run #{running[0]['id']} is acquiring and --force was given; "
               f"a short '{PROBE_PHASE}' phase will appear in the report")

    before_phase = cfg.get("phase")
    before_batch = cfg.get("batch_size_effective")
    if before_phase is None or before_batch is None:
        report(FAIL, "station has the study instrumentation",
               "/api/config has no 'phase' or 'batch_size_effective' — this station is "
               "running a build from before the capacity study was added")
        return

    try:
        # --- phase ---------------------------------------------------------
        got = st.post("/api/phase", {"name": PROBE_PHASE.replace("_", " ")})["phase"]
        if got != PROBE_PHASE:
            report(FAIL, "set phase", f"asked for {PROBE_PHASE!r}, station stored {got!r}")
        else:
            report(PASS, "set phase", f"{before_phase!r} -> {got!r} (labels are slugged)")

        # --- batch size ----------------------------------------------------
        r = st.post("/api/config/batch-size", {"n": PROBE_BATCH})
        if r.get("batch_size") != PROBE_BATCH:
            report(FAIL, "set batch size", f"asked for {PROBE_BATCH}, station reports {r}")
        else:
            report(PASS, "set batch size",
                   f"{before_batch} -> {PROBE_BATCH}, effective on the next batch, no restart")

        # --- does it reach the telemetry? -----------------------------------
        snap = _await_sample(st, PROBE_PHASE)
        if snap is None:
            report(FAIL, "phase reaches telemetry",
                   f"no sample carried phase {PROBE_PHASE!r} within {SAMPLE_WAIT_S:.0f}s — "
                   "is the telemetry sampler running?")
        else:
            report(PASS, "phase reaches telemetry",
                   f"a sample is tagged {snap['phase']!r} at batch size "
                   f"{snap.get('worker', {}).get('batch_size')}")
            _check_sample_fields(snap)
    finally:
        # Always put it back, including after a failure above: a station left
        # on batch size 2 and a probe phase is worse than a failed check.
        try:
            st.post("/api/phase", {"name": before_phase})
            st.post("/api/config/batch-size", {"n": before_batch})
            report(PASS, "restored", f"phase {before_phase!r}, batch size {before_batch}")
        except Exception as e:  # noqa: BLE001
            report(FAIL, "restored",
                   f"could not put the phase and batch size back: {e!r}\n"
                   f"          set them by hand: phase {before_phase!r}, batch {before_batch}")

    _check_report(st)


def _check_sample_fields(snap: dict) -> None:
    """The four things the study reads off every sample."""
    n = snap.get("inter_arrival_n") or 0
    if snap.get("inter_arrival_mean_s") is not None:
        report(PASS, "inter-arrival measured",
               f"mean {snap['inter_arrival_mean_s']:.2f}s, p95 "
               f"{snap['inter_arrival_p95_s']:.2f}s over {n} gap(s)")
    else:
        report(WARN, "inter-arrival measured",
               f"not enough data ({n} gap(s)) — correct when nothing is arriving; "
               "it needs three images to land while the station is up")

    rd, wr = snap.get("disk_read_mbs"), snap.get("disk_write_mbs")
    dev = snap.get("disk_device")
    if rd is None or wr is None:
        report(FAIL, "disk read/write sampled",
               "disk_read_mbs / disk_write_mbs are None — psutil has no IO counters here, "
               "so disk contention cannot be told from GPU contention")
    elif dev == "system":
        report(WARN, "disk read/write sampled",
               f"read {rd:.1f} / write {wr:.1f} MB/s, but counted machine-wide: the block "
               "device behind the input dir could not be resolved,\n          so another "
               "disk's traffic lands in these numbers")
    else:
        report(PASS, "disk read/write sampled",
               f"read {rd:.1f} / write {wr:.1f} MB/s on {dev} (the input volume)")

    gpu = snap.get("gpu")
    if not gpu:
        report(WARN, "gpu telemetry in samples",
               "no GPU block in the sample — GPU util, VRAM and the GPU-idle fraction will "
               "be blank in the report (expected off-NVIDIA)")
    elif gpu.get("gpu_util_pct") is None or gpu.get("vram_used_mb") is None:
        report(FAIL, "gpu telemetry in samples",
               f"nvidia-smi returned a block with empty fields: {gpu} — the per-phase GPU "
               "columns will be '—'")
    else:
        report(PASS, "gpu telemetry in samples",
               f"util {gpu['gpu_util_pct']:.0f}%, VRAM {gpu['vram_used_mb']:.0f} of "
               f"{gpu.get('vram_total_mb') or 0:.0f} MB")


def _check_report(st: _Station) -> None:
    try:
        rep = st.get("/api/report")
    except Exception as e:  # noqa: BLE001
        report(FAIL, "GET /api/report", repr(e))
        return
    usable = [p for p in rep.get("phases", []) if p.get("enough_data")]
    thin = [p for p in rep.get("phases", []) if not p.get("enough_data")]
    report(PASS, "GET /api/report",
           f"{rep['samples']} sample(s), {len(rep.get('phases', []))} phase(s): "
           f"{len(usable)} with enough data, {len(thin)} reporting 'not enough data' "
           f"(the floor is {rep['min_samples_per_phase']} samples)")

    c = rep.get("contention", {})
    if c.get("available"):
        report(PASS, "contention headline",
               f"concurrent at {c['percent_of_solo']:.1f}% of solo, {c['basis']}")
    else:
        report(WARN, "contention headline",
               f"not available yet: {c.get('note')} — expected before the study has run")

    if rep.get("tuning"):
        sizes = ", ".join(str(t["batch_size"]) for t in rep["tuning"])
        report(PASS, "tuning table", f"batch size(s) {sizes}")
    else:
        report(WARN, "tuning table",
               "empty — no phase has held one batch size long enough yet; "
               "that is what sweep.py is for")

    try:
        md = st.get("/api/report.md", raw=True)
    except Exception as e:  # noqa: BLE001
        report(FAIL, "GET /api/report.md", repr(e))
        return
    if md.lstrip().startswith("# Capacity and tuning report") and "## Tuning" in md:
        report(PASS, "GET /api/report.md", f"{len(md.splitlines())} lines of markdown")
    else:
        report(FAIL, "GET /api/report.md", f"unexpected content: {md[:120]!r}")

    dist = Path(__file__).resolve().parent / "station" / "frontend" / "dist" / "index.html"
    if dist.is_file():
        report(PASS, "station frontend built", str(dist.parent))
    else:
        report(WARN, "station frontend built",
               "station/frontend/dist missing — the Experiment page (the phase chips you "
               "tap from a phone) will not be served")


# --- main -------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Pre-run checks for the microscope monitor.")
    p.add_argument("--input-dir", default=os.environ.get("MM_INPUT_DIR", "./data/input"))
    p.add_argument("--output-dir", default=os.environ.get("MM_OUTPUT_DIR", "./data/masks"))
    p.add_argument("--port", type=int, default=int(os.environ.get("MM_PORT", "8000")))
    p.add_argument("--model", default=os.environ.get("MM_MODEL", "cpdino"))
    p.add_argument("--load-model", action="store_true",
                   help="actually construct the model (downloads ~1.1 GB on first use)")
    p.add_argument("--segment", action="store_true",
                   help="implies --load-model, and segments one synthetic 256x256 image")
    p.add_argument("--throughput", action="store_true",
                   help="implies --load-model: time a full-size batch and project the backlog")
    p.add_argument("--batch-size", type=int, default=int(os.environ.get("MM_BATCH_SIZE", "16")))
    # Defaults are the planned 96-well run: 2x2 tiles, 20-slice stack, phase + green.
    p.add_argument("--wells", type=int, default=96)
    p.add_argument("--tiles", type=int, default=4, help="tiles per well (2x2 = 4)")
    p.add_argument("--slices", type=int, default=20)
    p.add_argument("--channels", type=int, default=2)
    p.add_argument("--run-hours", type=float, default=2.0, help="planned acquisition duration")
    p.add_argument("--study", action="store_true",
                   help="exercise a running station's experiment instrumentation: set a "
                        "phase, change the batch size, read the report, put it all back")
    p.add_argument("--station-url", default=os.environ.get("MM_ST_BASE", "http://localhost:8100"))
    p.add_argument("--station-token", default=os.environ.get("MM_ST_AUTH_TOKEN") or None)
    p.add_argument("--force", action="store_true",
                   help="with --study, probe even while a run is acquiring")
    a = p.parse_args()

    input_dir = Path(a.input_dir).expanduser().resolve()
    output_dir = Path(a.output_dir).expanduser().resolve()

    print("microscope-monitor preflight")
    print(f"input={input_dir}  output={output_dir}  model={a.model}  port={a.port}")

    section("runtime")
    check_python()
    torch = check_torch()
    device = check_device(torch) if torch else "cpu"

    section("gpu")
    check_nvidia_smi()

    section("segmentation stack")
    check_dinov3()
    check_cellpose()
    check_model_cache(a.model)
    model = None
    if a.load_model or a.segment or a.throughput:
        model = check_model_load(a.model, device, a.segment, output_dir)
    else:
        report(WARN, "model construction", "skipped — pass --load-model (or --segment) to really build it")

    section("throughput")
    shape, shape_src = _image_shape(input_dir, 2048)
    plan = check_plate_plan(
        {"wells": a.wells, "tiles": a.tiles, "slices": a.slices,
         "channels": a.channels, "hours": a.run_hours},
        shape, shape_src,
    )
    if a.throughput and model is not None:
        check_throughput(model, device, a.batch_size, shape, plan)
    elif a.throughput:
        report(FAIL, "measured throughput", "model failed to load; cannot measure")
    else:
        report(WARN, "measured throughput",
               "not measured — pass --throughput to time a real batch and project the backlog")

    if a.study:
        section("capacity study instrumentation")
        check_study(a.station_url, a.station_token, a.force)

    section("storage and service")
    check_dirs(input_dir, output_dir)
    check_port(a.port)
    check_frontend()

    fails = [r for r in results if r[0] == FAIL]
    warns = [r for r in results if r[0] == WARN]
    print(f"\n{len(results) - len(fails) - len(warns)} passed, {len(warns)} warning(s), {len(fails)} failure(s)")
    if fails:
        print("\nNOT READY. Fix these first:")
        for _, name, detail in fails:
            print(f"  - {name}: {detail.strip().splitlines()[0] if detail else ''}")
        return 1
    print("\nReady to run." + (" Review the warnings above." if warns else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
