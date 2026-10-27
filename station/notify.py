"""Webhook alerts for unattended runs.

Four things are worth waking someone for: a run finished, a run failed,
segmentation has been losing ground for long enough that it is not a blip,
and the disk is about to fill.

Two layers of debounce, because the point of an overnight alert is that it
is rare enough to be believed:

  sustained  - "falling behind" must hold continuously for
               --falling-behind-seconds before it fires at all, so one slow
               minute during a z-stack says nothing.
  cooldown   - the same alert kind cannot fire again within
               --webhook-min-interval, tracked in the events table so a
               restart does not reset it and re-spam.

The payload is {"text": ...}, which Slack accepts directly and most other
receivers can parse.
"""
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from .db import Database

log = logging.getLogger("station.notify")

RUN_COMPLETE = "run_complete"
RUN_FAILED = "run_failed"
FALLING_BEHIND = "falling_behind"
DISK_LOW = "disk_low"

# Terminal, once-per-run facts: cooldown would be the wrong tool, so these
# are keyed on the run and simply never repeat.
PER_RUN_ONCE = (RUN_COMPLETE, RUN_FAILED)


class Notifier:
    def __init__(self, cfg, db: Database):
        self.cfg = cfg
        self.db = db
        self._behind_since: float | None = None
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.webhook_url)

    # --- delivery ----------------------------------------------------------

    def _post(self, text: str) -> bool:
        if not self.enabled:
            return False
        body = json.dumps({"text": text}).encode()
        req = urllib.request.Request(
            self.cfg.webhook_url, data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return 200 <= r.status < 300
        except urllib.error.HTTPError as e:
            log.warning("webhook rejected: %s %s", e.code, e.reason)
        except Exception as e:  # noqa: BLE001
            log.warning("webhook failed: %r", e)
        return False

    def _fire(self, kind: str, run_id: int | None, text: str) -> bool:
        """Record the event whether or not delivery worked.

        An alert that could not be delivered still happened, and the history
        endpoint should show it rather than pretending the night was quiet.
        """
        with self._lock:
            last = self.db.last_event(kind, run_id if kind in PER_RUN_ONCE else None)
            if last:
                if kind in PER_RUN_ONCE:
                    return False
                if time.time() - last["ts"] < self.cfg.webhook_min_interval:
                    return False
            delivered = self._post(text) if self.enabled else False
            self.db.add_event(kind, run_id, text, delivered)
            log.info("alert %s (delivered=%s): %s", kind, delivered, text)
            return True

    # --- triggers ----------------------------------------------------------

    def run_finished(self, run: dict[str, Any], counts: dict[str, int]) -> None:
        if run["status"] == "done":
            self._fire(RUN_COMPLETE, run["id"],
                       f"Run complete: {run['name']} — {counts['segmented']} images segmented, "
                       f"{counts['failed']} failed.")
        elif run["status"] == "failed":
            self._fire(RUN_FAILED, run["id"],
                       f"Run FAILED: {run['name']} — {run.get('error') or 'no error recorded'}")

    def check(self, snap: dict[str, Any], run: dict[str, Any] | None) -> None:
        """Called on every telemetry tick."""
        self._check_behind(snap, run)
        self._check_disk(snap)

    def _check_behind(self, snap: dict[str, Any], run: dict[str, Any] | None) -> None:
        now = time.time()
        if not snap.get("falling_behind"):
            self._behind_since = None
            return
        if self._behind_since is None:
            self._behind_since = now
            return
        held = now - self._behind_since
        if held < self.cfg.falling_behind_seconds:
            return
        self._fire(
            FALLING_BEHIND, run["id"] if run else None,
            f"Segmentation falling behind for {held / 60:.0f} min"
            + (f" on {run['name']}" if run else "")
            + f" — backlog {snap.get('backlog')} images, growing "
              f"{snap.get('backlog_trend_per_min', 0):.0f}/min.")

    def _check_disk(self, snap: dict[str, Any]) -> None:
        free = snap.get("disk_free_bytes")
        if free is None:
            return
        free_gb = free / 1024 ** 3
        if free_gb >= self.cfg.disk_free_threshold_gb:
            return
        self._fire(DISK_LOW, None,
                   f"Disk low: {free_gb:.1f} GB free, below the "
                   f"{self.cfg.disk_free_threshold_gb:.0f} GB threshold.")

    def test(self) -> dict[str, Any]:
        if not self.enabled:
            return {"sent": False, "reason": "no webhook URL configured"}
        ok = self._post("Station test alert — if you can read this, unattended "
                        "run notifications will reach you.")
        self.db.add_event("test", None, "manual test alert", ok)
        return {"sent": ok, "reason": None if ok else "the receiver did not accept it"}
