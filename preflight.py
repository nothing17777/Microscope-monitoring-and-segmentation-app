#!/usr/bin/env python3
"""Fail fast, before a two-hour acquisition run.

Checks the things that actually break on a fresh workstation: a torch wheel
that doesn't support the card, the undeclared dinov3 dependency, cpdino
weights that aren't cached yet, nvidia-smi, and whether the input and output
directories are really readable/writable.

    python preflight.py --input-dir /data/lumaviewpro/output
    python preflight.py --input-dir ... --load-model     # also builds cpdino
    python preflight.py --input-dir ... --segment        # also segments one image

Exit code 0 if nothing FAILed (warnings are allowed), 1 otherwise.
"""
import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
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


def check_model_load(model: str, device: str, segment: bool, output_dir: Path) -> None:
    """Actually construct cpdino, and optionally segment one small image."""
    try:
        import numpy as np
        import torch
        from cellpose.models import CellposeModel
    except Exception as e:  # noqa: BLE001
        report(FAIL, f"load model '{model}'", repr(e))
        return
    try:
        t0 = time.time()
        m = CellposeModel(gpu=device != "cpu", pretrained_model=model, device=torch.device(device))
        report(PASS, f"load model '{model}'", f"{type(m).__name__} on {device} in {time.time() - t0:.1f}s")
    except Exception as e:  # noqa: BLE001
        report(FAIL, f"load model '{model}'", f"{type(e).__name__}: {e}")
        return
    if not segment:
        return
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
    if a.load_model or a.segment:
        check_model_load(a.model, device, a.segment, output_dir)
    else:
        report(WARN, "model construction", "skipped — pass --load-model (or --segment) to really build it")

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
