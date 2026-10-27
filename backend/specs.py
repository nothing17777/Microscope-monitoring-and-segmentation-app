"""Machine spec capture. Runs once at startup; never raises."""
import platform
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

# label -> argv. Missing binaries are reported as unavailable, not errors.
COMMANDS: list[tuple[str, list[str]]] = [
    ("uname", ["uname", "-a"]),
    ("lscpu", ["lscpu"]),
    ("free", ["free", "-h"]),
    ("lsblk", ["lsblk"]),
    ("df", ["df", "-h"]),
    ("nvidia-smi", ["nvidia-smi"]),
    ("nvidia-smi-query", ["nvidia-smi", "--query-gpu=name,driver_version,memory.total,compute_cap",
                          "--format=csv"]),
    # macOS equivalents, so a dev box dump is still useful
    ("sysctl-cpu", ["sysctl", "-n", "machdep.cpu.brand_string"]),
    ("system_profiler-hw", ["system_profiler", "SPHardwareDataType"]),
]


def _run(argv: list[str], timeout: float = 30.0) -> str:
    if shutil.which(argv[0]) is None:
        return f"<{argv[0]} not available on this machine>"
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr.strip() else "")
        return out.strip() or "<no output>"
    except Exception as e:  # noqa: BLE001 - a spec dump must never break startup
        return f"<failed: {e!r}>"


def _driver_version() -> str | None:
    if shutil.which("nvidia-smi") is None:
        return None
    out = _run(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"])
    first = out.splitlines()[0].strip() if out else ""
    return first or None


def _block_device_for(path: Path) -> dict[str, Any]:
    """Which block device holds `path`."""
    info: dict[str, Any] = {"path": str(path)}
    target = path if path.exists() else path.parent
    if shutil.which("findmnt"):  # Linux
        info["findmnt"] = _run(["findmnt", "-T", str(target), "-o",
                                "SOURCE,TARGET,FSTYPE,SIZE,USED,AVAIL"])
    if shutil.which("df"):
        info["df"] = _run(["df", "-h", str(target)])
    if shutil.which("lsblk") and shutil.which("df"):
        # resolve SOURCE -> parent disk + model where possible
        src = ""
        for line in info.get("df", "").splitlines()[1:]:
            parts = line.split()
            if parts:
                src = parts[0]
                break
        if src.startswith("/dev/"):
            info["device"] = src
            info["lsblk_device"] = _run(
                ["lsblk", "-o", "NAME,MODEL,SIZE,ROTA,TYPE,MOUNTPOINT", src]
            )
    return info


def _torch_info() -> dict[str, Any]:
    try:
        import torch
    except Exception as e:  # noqa: BLE001
        return {"available": False, "error": repr(e)}
    info: dict[str, Any] = {
        "available": True,
        "version": torch.__version__,
        "cuda_build": getattr(torch.version, "cuda", None),
        "cuda_available": bool(torch.cuda.is_available()),
        "mps_available": bool(getattr(torch.backends, "mps", None)
                              and torch.backends.mps.is_available()),
    }
    if info["cuda_available"]:
        info["device_count"] = torch.cuda.device_count()
        info["device_name"] = torch.cuda.get_device_name(0)
        info["device_capability"] = ".".join(map(str, torch.cuda.get_device_capability(0)))
        props = torch.cuda.get_device_properties(0)
        info["device_total_memory_bytes"] = props.total_memory
    return info


def _cellpose_version() -> str | None:
    try:
        import cellpose
        return getattr(cellpose, "version", None) or getattr(cellpose, "__version__", None)
    except Exception:  # noqa: BLE001
        return None


def capture(input_dir: Path, device: str) -> dict[str, Any]:
    cmds = {label: _run(argv) for label, argv in COMMANDS}
    return {
        "captured_at": time.time(),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "python": platform.python_version(),
            "node": platform.node(),
        },
        "commands": cmds,
        "nvidia_driver_version": _driver_version(),
        "torch": _torch_info(),
        "cellpose_version": _cellpose_version(),
        "device_in_use": device,
        "input_volume": _block_device_for(input_dir),
    }


def to_text(specs: dict[str, Any]) -> str:
    p = specs["platform"]
    lines = [
        "=" * 72,
        "MICROSCOPE WORKSTATION SPEC",
        f"captured_at (unix): {specs['captured_at']}",
        "=" * 72,
        "",
        "[platform]",
        *(f"  {k}: {v}" for k, v in p.items()),
        "",
        "[versions]",
        f"  nvidia driver: {specs['nvidia_driver_version']}",
        f"  cellpose: {specs['cellpose_version']}",
        *(f"  torch.{k}: {v}" for k, v in specs["torch"].items()),
        f"  device in use: {specs['device_in_use']}",
        "",
        "[input volume]",
        *(f"  --- {k} ---\n{v}" for k, v in specs["input_volume"].items()),
        "",
    ]
    for label, out in specs["commands"].items():
        lines += ["=" * 72, f"[{label}]", "=" * 72, out, ""]
    return "\n".join(lines) + "\n"


def write_text(specs: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_text(specs))
