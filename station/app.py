"""Station API.

Every GET is open: read-only monitoring is for the whole lab. Every POST
changes hardware or queue state and needs the token.
"""
import asyncio
import contextlib
import logging
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend import specs as specs_mod

from . import acquisition
from .config import Config
from .db import Database
from .notify import Notifier
from .runs import RunError, RunManager
from .telemetry import Sampler
from .worker import StationWorker

log = logging.getLogger("station.app")

FRONTEND_DIST = Path(__file__).resolve().parent / "frontend" / "dist"


class NewRun(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    plate: str = Field(default="", max_length=120)
    expected_image_count: int = Field(gt=0)
    notes: str = Field(default="", max_length=4000)


class PauseBody(BaseModel):
    paused: bool


def create_app(cfg: Config) -> FastAPI:
    cfg.input_dir.mkdir(parents=True, exist_ok=True)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)

    db = Database(cfg.db_path)
    worker = StationWorker(cfg, db)
    adapter = acquisition.probe(cfg.input_dir, cfg.enable_acquisition_control)
    notifier = Notifier(cfg, db)
    runs = RunManager(db, worker, adapter, cfg)
    sampler = Sampler(cfg, db, worker, runs, notifier)

    def require_token(x_auth_token: str | None = Header(default=None)) -> None:
        if not cfg.auth_token:
            return
        if not x_auth_token or not secrets.compare_digest(x_auth_token, cfg.auth_token):
            raise HTTPException(status_code=401, detail="missing or bad X-Auth-Token")

    guard = [Depends(require_token)]

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        worker.start()
        sampler.start()
        log.info("station: device=%s model=%s input=%s", worker.device, worker.model_label,
                 cfg.input_dir)
        log.info("acquisition mode=%s (%s)", adapter.mode, adapter.reason)
        if runs.global_paused():
            log.warning("segmentation is globally PAUSED (restored from the database)")
        if not cfg.auth_token:
            log.warning("MM_ST_AUTH_TOKEN is not set — any client can start, pause or "
                        "cancel runs. Set it before exposing this beyond a trusted network.")
        if not notifier.enabled:
            log.warning("no webhook configured — unattended runs will alert nobody")
        try:
            yield
        finally:
            sampler.stop()
            worker.stop()
            db.close()

    app = FastAPI(title="Microscope Station", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                       allow_headers=["*"])

    # --- read-only ---------------------------------------------------------

    @app.get("/api/status")
    def status():
        return sampler.latest or sampler.sample()

    @app.get("/api/queue")
    def queue():
        return runs.queue()

    @app.get("/api/runs")
    def list_runs(limit: int = 100):
        """Past and present runs with summary stats, newest first."""
        return {"runs": [runs.run_view(r) for r in db.list_runs(limit=limit)]}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: int):
        run = db.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail=f"no run #{run_id}")
        return {"run": runs.run_view(run), "plate": runs.plate(run_id),
                "failures": db.failures(run_id)}

    @app.get("/api/runs/{run_id}/history")
    def run_history(run_id: int):
        """Stored telemetry for one run — replays its charts after it ended."""
        if not db.get_run(run_id):
            raise HTTPException(status_code=404, detail=f"no run #{run_id}")
        return {"run_id": run_id, "samples": db.run_history(run_id)}

    @app.get("/api/runs/{run_id}/plate")
    def run_plate(run_id: int):
        if not db.get_run(run_id):
            raise HTTPException(status_code=404, detail=f"no run #{run_id}")
        return runs.plate(run_id)

    @app.get("/api/history")
    def history(minutes: float = 30.0):
        return {"minutes": minutes, "samples": db.history(minutes)}

    @app.get("/api/events")
    def events(limit: int = 50):
        return {"events": db.recent_events(limit), "webhook_configured": notifier.enabled}

    @app.get("/api/failures")
    def failures():
        return {"failures": db.failures()}

    @app.get("/api/specs")
    def get_specs():
        spec = specs_mod.capture(cfg.input_dir, worker.device)
        return {"specs": spec, "text": specs_mod.to_text(spec)}

    @app.get("/api/config")
    def get_config():
        return {
            "input_dir": str(cfg.input_dir), "output_dir": str(cfg.output_dir),
            "model": worker.model_label, "device": worker.device, "dry_run": cfg.dry_run,
            "batch_size": cfg.batch_size, "poll_interval": cfg.poll_interval,
            "stable_seconds": cfg.stable_seconds,
            "auth_required": bool(cfg.auth_token),
            "acquisition": adapter.describe(),
            "webhook_configured": notifier.enabled,
            "plate": {"rows": cfg.plate_rows, "cols": cfg.plate_cols},
        }

    # --- state-changing ----------------------------------------------------

    @app.post("/api/runs", dependencies=guard, status_code=201)
    def create_run(body: NewRun):
        try:
            return runs.create(body.name, body.plate, body.expected_image_count, body.notes)
        except RunError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from e

    @app.post("/api/runs/{run_id}/{action}", dependencies=guard)
    def run_action(run_id: int, action: str):
        fn = {"pause": runs.pause, "resume": runs.resume, "cancel": runs.cancel}.get(action)
        if fn is None:
            raise HTTPException(status_code=404, detail=f"unknown action {action!r}")
        try:
            return fn(run_id)
        except RunError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from e

    @app.post("/api/runs/{run_id}/segmentation/pause", dependencies=guard)
    def run_seg_pause(run_id: int, body: PauseBody):
        try:
            return runs.set_run_segmentation(run_id, body.paused)
        except RunError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from e

    @app.post("/api/segmentation/pause", dependencies=guard)
    def global_pause(body: PauseBody):
        return {"paused": runs.set_global_pause(body.paused)}

    @app.post("/api/notify/test", dependencies=guard)
    def notify_test():
        return notifier.test()

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        try:
            while True:
                await socket.send_json(sampler.latest or sampler.sample())
                await asyncio.sleep(cfg.telemetry_interval)
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            log.debug("websocket closed", exc_info=True)

    if FRONTEND_DIST.is_dir():
        # Content-hashed filenames: a given URL never changes contents.
        app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

        @app.get("/")
        def index():
            # The asset filenames are content-hashed and may be cached
            # forever, but index.html is what points at them: if a browser
            # caches it, a tab left open over a redeploy keeps running the
            # previous bundle against the new API.
            return FileResponse(FRONTEND_DIST / "index.html",
                                headers={"Cache-Control": "no-store"})
    else:
        @app.get("/")
        def no_frontend():
            return PlainTextResponse(
                "Station frontend not built. Run `npm install && npm run build` in "
                "station/frontend/, or use the Vite dev server on :5174.\n")

    return app
