"""Configuration: env vars, overridable by CLI flags in main.py."""
import argparse
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    input_dir: Path
    output_dir: Path
    db_path: Path
    host: str
    port: int
    batch_size: int
    poll_interval: float
    stable_seconds: float
    telemetry_interval: float
    model_name: str
    dry_run: bool
    specs_path: Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def load_config(argv: list[str] | None = None) -> Config:
    p = argparse.ArgumentParser(prog="microscope-monitor")
    p.add_argument("--input-dir", default=_env("MM_INPUT_DIR", "./data/input"))
    p.add_argument("--output-dir", default=_env("MM_OUTPUT_DIR", "./data/masks"))
    p.add_argument("--db-path", default=_env("MM_DB_PATH", "./data/monitor.sqlite"))
    p.add_argument("--host", default=_env("MM_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(_env("MM_PORT", "8000")))
    p.add_argument("--batch-size", type=int, default=int(_env("MM_BATCH_SIZE", "16")))
    p.add_argument("--poll-interval", type=float, default=float(_env("MM_POLL_INTERVAL", "5")))
    p.add_argument(
        "--stable-seconds", type=float, default=float(_env("MM_STABLE_SECONDS", "0")),
        help="A file is ready only once its size has been unchanged for this "
             "long. 0 means 2x --poll-interval.",
    )
    p.add_argument(
        "--telemetry-interval", type=float, default=float(_env("MM_TELEMETRY_INTERVAL", "2"))
    )
    p.add_argument("--model", default=_env("MM_MODEL", "cpdino"))
    p.add_argument("--specs-path", default=_env("MM_SPECS_PATH", "./specs.txt"))
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=_env("MM_DRY_RUN", "0") not in ("0", "", "false", "False"),
        help="Skip Cellpose; write a trivial threshold mask. For wiring up the "
        "dashboard on a machine without a working cellpose/torch install.",
    )
    a = p.parse_args(argv)
    return Config(
        input_dir=Path(a.input_dir).expanduser().resolve(),
        output_dir=Path(a.output_dir).expanduser().resolve(),
        db_path=Path(a.db_path).expanduser().resolve(),
        host=a.host,
        port=a.port,
        batch_size=a.batch_size,
        poll_interval=a.poll_interval,
        stable_seconds=a.stable_seconds or 2 * a.poll_interval,
        telemetry_interval=a.telemetry_interval,
        model_name=a.model,
        dry_run=a.dry_run,
        specs_path=Path(a.specs_path).expanduser().resolve(),
    )
