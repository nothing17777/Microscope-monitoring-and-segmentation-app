"""Acquisition adapters.

Headless control of LumaViewPro is UNVERIFIED. The app probes at startup and
falls back to watching files on any doubt, so the default path never depends
on something that has not been confirmed against hardware.
"""
import logging
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("station.acquisition")

TIFF_SUFFIXES = {".tif", ".tiff"}


class AcquisitionAdapter:
    mode = "unknown"
    can_control = False
    reason: str | None = None

    def describe(self) -> dict[str, Any]:
        return {"mode": self.mode, "can_control": self.can_control, "reason": self.reason}

    def start(self, run: dict[str, Any]) -> None: raise NotImplementedError
    def pause(self, run: dict[str, Any]) -> None: raise NotImplementedError
    def resume(self, run: dict[str, Any]) -> None: raise NotImplementedError
    def cancel(self, run: dict[str, Any]) -> None: raise NotImplementedError
    def observed_count(self) -> int: raise NotImplementedError


class ObservedAcquisition(AcquisitionAdapter):
    """Count files. Never touch the microscope."""

    mode = "observed"
    can_control = False

    def __init__(self, input_dir: Path, reason: str):
        self.input_dir = input_dir
        self.reason = reason
        self._count = 0
        self._at = 0.0
        self._lock = threading.Lock()

    def _no(self) -> NotImplementedError:
        return NotImplementedError(
            f"acquisition control is unavailable: {self.reason}. Start, pause and "
            "stop the run in LumaViewPro itself; this app only watches its output.")

    def start(self, run): raise self._no()
    def pause(self, run): raise self._no()
    def resume(self, run): raise self._no()
    def cancel(self, run): raise self._no()

    def observed_count(self) -> int:
        with self._lock:
            if time.time() - self._at < 2.0:
                return self._count
            n = 0
            if self.input_dir.exists():
                for p in self.input_dir.rglob("*"):
                    if p.suffix.lower() in TIFF_SUFFIXES:
                        n += 1
            self._count, self._at = n, time.time()
            return n


class ControlledAcquisition(AcquisitionAdapter):
    """Would drive LumaViewPro via lumascope_api. STUBBED.

    The import and construction are real, so the probe reports honestly, but
    every verb below only logs. Nothing here has run against a microscope.
    """

    mode = "controlled"
    can_control = True

    def __init__(self, scope: Any, input_dir: Path):
        self.scope = scope
        self._observed = ObservedAcquisition(input_dir, "n/a")
        self.reason = "lumascope_api imported and control explicitly enabled"

    # TODO(headless): build a LumaViewPro protocol from the run's plate,
    # tile grid, z-stack and channels, and run it. Confirm on hardware first.
    def start(self, run): log.warning("STUB start(run=%s) — no hardware moved", run["id"])

    # TODO(headless): halt without losing stage position.
    def pause(self, run): log.warning("STUB pause(run=%s)", run["id"])

    # TODO(headless): resume from the last completed well.
    def resume(self, run): log.warning("STUB resume(run=%s)", run["id"])

    # TODO(headless): abort the protocol and park the stage safely.
    def cancel(self, run): log.warning("STUB cancel(run=%s)", run["id"])

    def observed_count(self) -> int:
        # Progress is counted from disk even with control: that number is
        # true whether or not the stubs above do anything.
        return self._observed.observed_count()


def probe(input_dir: Path, enable_control: bool) -> AcquisitionAdapter:
    if not enable_control:
        return ObservedAcquisition(
            input_dir, "control not enabled (pass --enable-acquisition-control)")
    try:
        import lumascope_api  # type: ignore
    except Exception as e:  # noqa: BLE001
        log.info("lumascope_api not importable (%s); watching files instead", e)
        return ObservedAcquisition(input_dir, f"lumascope_api not importable: {e}")
    try:
        scope = lumascope_api.Lumascope()
    except Exception as e:  # noqa: BLE001
        log.warning("lumascope_api imported but Lumascope() failed: %r", e)
        return ObservedAcquisition(input_dir, f"lumascope_api present but unusable: {e!r}")
    log.warning("acquisition control ENABLED — hardware verbs are still stubs")
    return ControlledAcquisition(scope, input_dir)
