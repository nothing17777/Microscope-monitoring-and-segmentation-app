"""Acquisition adapters.

Whether LumaViewPro can be driven headless is not confirmed. Rather than
assume either way, the app probes at startup and picks one of two adapters:

  ControlledAcquisition - drives the scope through lumascope_api. Every call
      that would move hardware is a STUB (see TODO markers); it is selected
      only when the import probe succeeds AND --enable-acquisition-control
      is passed, so it can never engage by accident.
  ObservedAcquisition  - no control at all. Progress is inferred by counting
      files that appear in the input dir, against an expected total the user
      types in when creating the job.

The UI reads `mode` and `can_control` and disables the controls it cannot
honour, with `reason` as the visible explanation. Nothing renders a button
that does nothing.
"""
import logging
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("acquisition")

TIFF_SUFFIXES = {".tif", ".tiff"}


class AcquisitionAdapter:
    """Interface both implementations satisfy."""

    mode = "unknown"
    can_control = False
    reason: str | None = None

    def describe(self) -> dict[str, Any]:
        return {"mode": self.mode, "can_control": self.can_control, "reason": self.reason}

    # Hardware verbs. Observed mode raises; the API layer turns that into a
    # 409 so a stale browser tab cannot pretend it has control.
    def start(self, job: dict[str, Any]) -> None:
        raise NotImplementedError

    def pause(self, job: dict[str, Any]) -> None:
        raise NotImplementedError

    def resume(self, job: dict[str, Any]) -> None:
        raise NotImplementedError

    def cancel(self, job: dict[str, Any]) -> None:
        raise NotImplementedError

    def observed_count(self) -> int:
        raise NotImplementedError


class ObservedAcquisition(AcquisitionAdapter):
    """Watch the output directory; never touch the microscope."""

    mode = "observed"
    can_control = False

    def __init__(self, input_dir: Path, reason: str):
        self.input_dir = input_dir
        self.reason = reason
        self._count = 0
        self._counted_at = 0.0
        self._lock = threading.Lock()

    def _unavailable(self) -> NotImplementedError:
        return NotImplementedError(
            f"acquisition control is unavailable: {self.reason}. "
            "Start, pause and stop the run in LumaViewPro itself; this "
            "dashboard only watches the files it writes."
        )

    def start(self, job): raise self._unavailable()
    def pause(self, job): raise self._unavailable()
    def resume(self, job): raise self._unavailable()
    def cancel(self, job): raise self._unavailable()

    def observed_count(self) -> int:
        """TIFFs on disk, cached briefly — this walks the whole tree."""
        with self._lock:
            if time.time() - self._counted_at < 2.0:
                return self._count
            n = 0
            if self.input_dir.exists():
                for p in self.input_dir.rglob("*"):
                    if p.suffix.lower() in TIFF_SUFFIXES:
                        n += 1
            self._count, self._counted_at = n, time.time()
            return n


class ControlledAcquisition(AcquisitionAdapter):
    """Drive LumaViewPro through lumascope_api.

    STUBBED. The import and object construction are real, so the probe tells
    the truth about whether the API is reachable, but every verb below is a
    no-op that only moves the job's own state. Nothing here has been tested
    against a microscope. See README, "Stubbed pending headless control".
    """

    mode = "controlled"
    can_control = True

    def __init__(self, scope: Any, input_dir: Path):
        self.scope = scope
        self.input_dir = input_dir
        self._observed = ObservedAcquisition(input_dir, "n/a")
        self.reason = "lumascope_api imported and control explicitly enabled"

    def start(self, job: dict[str, Any]) -> None:
        # TODO(headless): translate job["params"] (wells, tiles, z-slices,
        # channels) into a LumaViewPro protocol and run it. Confirm against
        # real hardware before removing this guard.
        log.warning("STUB ControlledAcquisition.start(job=%s) — no hardware was moved", job["id"])

    def pause(self, job: dict[str, Any]) -> None:
        # TODO(headless): halt the running protocol without losing position.
        log.warning("STUB ControlledAcquisition.pause(job=%s)", job["id"])

    def resume(self, job: dict[str, Any]) -> None:
        # TODO(headless): resume the protocol from the last completed well.
        log.warning("STUB ControlledAcquisition.resume(job=%s)", job["id"])

    def cancel(self, job: dict[str, Any]) -> None:
        # TODO(headless): abort the protocol and park the stage safely.
        log.warning("STUB ControlledAcquisition.cancel(job=%s)", job["id"])

    def observed_count(self) -> int:
        # Even with control, progress is still counted from files on disk:
        # that number is true whether or not the stubs above do anything.
        return self._observed.observed_count()


def probe(input_dir: Path, enable_control: bool) -> AcquisitionAdapter:
    """Pick an adapter. Falls back to Observed on any doubt whatsoever."""
    if not enable_control:
        return ObservedAcquisition(
            input_dir,
            "acquisition control not enabled (pass --enable-acquisition-control)",
        )
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
    log.warning("acquisition control ENABLED — the hardware verbs are still stubs")
    return ControlledAcquisition(scope, input_dir)
