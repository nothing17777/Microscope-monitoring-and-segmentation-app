"""Station configuration.

Deliberately separate from the file-centric dashboard in ../backend: its own
port, its own database, its own env prefix (MM_ST_*), so both can run on the
workstation without touching each other's state.

Do not point both apps at the same --input-dir at the same time: each runs
its own segmenter and they would both try to segment the same TIFFs.
"""
import argparse
import os
from dataclasses import dataclass, field
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
    auth_token: str | None
    enable_acquisition_control: bool
    webhook_url: str | None
    webhook_min_interval: float
    falling_behind_seconds: float
    disk_free_threshold_gb: float
    plate_rows: int
    plate_cols: int
    include_existing: bool = False
    capture_folders: tuple[str, ...] = ()
    active_window: float = 900.0

    # Named for the parent Worker in ../backend, which reads cfg.specs_path.
    specs_path: Path = field(default=Path("/dev/null"))


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _flag(name: str) -> bool:
    return _env(name, "0") not in ("0", "", "false", "False")


def load_config(argv: list[str] | None = None) -> Config:
    p = argparse.ArgumentParser(prog="station")
    p.add_argument("--input-dir", default=_env("MM_ST_INPUT_DIR", "./data/station/input"))
    p.add_argument("--output-dir", default=_env("MM_ST_OUTPUT_DIR", "./data/station/masks"))
    p.add_argument("--db-path", default=_env("MM_ST_DB_PATH", "./data/station.sqlite"))
    p.add_argument("--host", default=_env("MM_ST_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(_env("MM_ST_PORT", "8100")))
    p.add_argument("--batch-size", type=int, default=int(_env("MM_ST_BATCH_SIZE", "2")),
                   help="Images per GPU call. 2 is what the validated script ran with.")
    p.add_argument("--poll-interval", type=float, default=float(_env("MM_ST_POLL_INTERVAL", "5")))
    p.add_argument("--stable-seconds", type=float, default=float(_env("MM_ST_STABLE_SECONDS", "0")),
                   help="0 means 2x --poll-interval")
    p.add_argument("--telemetry-interval", type=float,
                   default=float(_env("MM_ST_TELEMETRY_INTERVAL", "2")))
    p.add_argument("--model", default=_env("MM_ST_MODEL", "cpdino"))
    p.add_argument("--dry-run", action="store_true", default=_flag("MM_ST_DRY_RUN"))
    p.add_argument("--auth-token", default=os.environ.get("MM_ST_AUTH_TOKEN") or None,
                   help="Required for every state-changing POST. GETs stay open.")
    p.add_argument("--enable-acquisition-control", action="store_true",
                   default=_flag("MM_ST_ENABLE_ACQUISITION_CONTROL"),
                   help="Try to drive LumaViewPro. The hardware verbs are stubs.")
    p.add_argument("--webhook-url", default=os.environ.get("MM_ST_WEBHOOK_URL") or None,
                   help="Slack-compatible incoming webhook for unattended runs.")
    p.add_argument("--webhook-min-interval", type=float,
                   default=float(_env("MM_ST_WEBHOOK_MIN_INTERVAL", "900")),
                   help="Debounce: seconds before the same alert may fire again.")
    p.add_argument("--falling-behind-seconds", type=float,
                   default=float(_env("MM_ST_FALLING_BEHIND_SECONDS", "300")),
                   help="How long the backlog must keep growing before alerting.")
    p.add_argument("--disk-free-threshold-gb", type=float,
                   default=float(_env("MM_ST_DISK_FREE_GB", "50")))
    p.add_argument("--plate-rows", type=int, default=int(_env("MM_ST_PLATE_ROWS", "8")))
    p.add_argument("--plate-cols", type=int, default=int(_env("MM_ST_PLATE_COLS", "12")))
    # Pointing --input-dir at LVP's ProtocolData makes every capture folder
    # in it a run of its own. These decide which folders already there at
    # startup are taken; folders that appear later always are.
    p.add_argument("--include-existing", action="store_true",
                   default=_flag("MM_ST_INCLUDE_EXISTING"),
                   help="Also segment capture folders that were already finished "
                        "at startup. Off by default: ProtocolData keeps every "
                        "past run, and they would all be queued.")
    p.add_argument("--capture-folder", action="append", default=[],
                   help="A capture folder name under --input-dir to segment even "
                        "though it predates startup. Repeatable.")
    p.add_argument("--active-window", type=float,
                   default=float(_env("MM_ST_ACTIVE_WINDOW", "900")),
                   help="A folder present at startup is taken if it changed in "
                        "the last this-many seconds: an acquisition in progress.")
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
        auth_token=a.auth_token,
        enable_acquisition_control=a.enable_acquisition_control,
        webhook_url=a.webhook_url,
        webhook_min_interval=a.webhook_min_interval,
        falling_behind_seconds=a.falling_behind_seconds,
        disk_free_threshold_gb=a.disk_free_threshold_gb,
        plate_rows=a.plate_rows,
        plate_cols=a.plate_cols,
        include_existing=a.include_existing,
        capture_folders=tuple(a.capture_folder),
        active_window=a.active_window,
    )
