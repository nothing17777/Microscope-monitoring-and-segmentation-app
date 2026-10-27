"""FastAPI app: status, history, specs, failures, queues, and a 2s WS push."""
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

from . import acquisition, specs as specs_mod
from .config import Config
from .db import Database
from .jobs import JobError, JobManager
from .telemetry import Sampler
from .worker import Worker

log = logging.getLogger("app")

FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"


class NewAcquisition(BaseModel):
    plate: str = Field(min_length=1, max_length=120)
    expected_images: int = Field(gt=0)
    notes: str = Field(default="", max_length=2000)


class GlobalPause(BaseModel):
    paused: bool


def create_app(cfg: Config) -> FastAPI:
    cfg.input_dir.mkdir(parents=True, exist_ok=True)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)

    db = Database(cfg.db_path)
    worker = Worker(cfg, db)
    adapter = acquisition.probe(cfg.input_dir, cfg.enable_acquisition_control)
    jobs = JobManager(db, worker, adapter)
    sampler = Sampler(cfg, db, worker, jobs)

    def require_token(x_auth_token: str | None = Header(default=None)) -> None:
        """Guard every state-changing POST. Read-only views stay open.

        Compared with secrets.compare_digest so the check does not leak the
        token's length or prefix through timing.
        """
        if not cfg.auth_token:
            return
        if not x_auth_token or not secrets.compare_digest(x_auth_token, cfg.auth_token):
            raise HTTPException(status_code=401, detail="missing or bad X-Auth-Token")

    guard = [Depends(require_token)]

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        spec = specs_mod.capture(cfg.input_dir, worker.device)
        db.set_specs(spec)
        try:
            specs_mod.write_text(spec, cfg.specs_path)
            log.info("wrote %s", cfg.specs_path)
        except Exception:  # noqa: BLE001
            log.exception("could not write specs.txt")
        worker.start()
        sampler.start()
        log.info("device=%s model=%s input=%s output=%s",
                 worker.device, worker.model_label, cfg.input_dir, cfg.output_dir)
        log.info("acquisition mode=%s (%s)", adapter.mode, adapter.reason)
        if jobs.global_paused():
            log.warning("segmentation is globally PAUSED (restored from the database)")
        if not cfg.auth_token:
            log.warning("MM_AUTH_TOKEN is not set — any client can pause, cancel or "
                        "queue jobs. Set it before exposing this beyond a trusted network.")
        try:
            yield
        finally:
            sampler.stop()
            worker.stop()
            db.close()

    app = FastAPI(title="Microscope Monitor", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
    )

    @app.get("/api/status")
    def status():
        return sampler.latest or sampler.sample()

    @app.get("/api/history")
    def history(minutes: float = 30.0):
        return {"minutes": minutes, "samples": db.history(minutes)}

    @app.get("/api/specs")
    def get_specs():
        spec = db.get_specs() or {}
        return {"specs": spec, "text": specs_mod.to_text(spec) if spec else ""}

    @app.get("/api/specs.txt", response_class=PlainTextResponse)
    def get_specs_text():
        spec = db.get_specs() or {}
        return specs_mod.to_text(spec) if spec else "specs not captured yet\n"

    @app.get("/api/failures")
    def failures():
        return {"failures": db.failures()}

    # --- queues ------------------------------------------------------------

    @app.get("/api/queue")
    def get_queue():
        return jobs.queue()

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: int):
        job = db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"no job #{job_id}")
        return job

    @app.post("/api/jobs", dependencies=guard, status_code=201)
    def create_job(body: NewAcquisition):
        try:
            return jobs.create_acquisition(body.plate, body.expected_images, body.notes)
        except JobError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from e

    @app.post("/api/jobs/{job_id}/{action}", dependencies=guard)
    def job_action(job_id: int, action: str):
        fn = {"pause": jobs.pause, "resume": jobs.resume, "cancel": jobs.cancel}.get(action)
        if fn is None:
            raise HTTPException(status_code=404, detail=f"unknown action {action!r}")
        try:
            return fn(job_id)
        except JobError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from e

    @app.post("/api/segmentation/pause", dependencies=guard)
    def segmentation_pause(body: GlobalPause):
        """The whole worker, separate from any one job's pause."""
        return {"paused": jobs.set_global_pause(body.paused)}

    @app.get("/api/config")
    def get_config():
        return {
            "input_dir": str(cfg.input_dir),
            "output_dir": str(cfg.output_dir),
            "db_path": str(cfg.db_path),
            "batch_size": cfg.batch_size,
            "poll_interval": cfg.poll_interval,
            "stable_seconds": cfg.stable_seconds,
            "telemetry_interval": cfg.telemetry_interval,
            "model": worker.model_label,
            "device": worker.device,
            "dry_run": cfg.dry_run,
            "auth_required": bool(cfg.auth_token),
            "acquisition": adapter.describe(),
        }

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
        app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

        @app.get("/")
        def index():
            return FileResponse(FRONTEND_DIST / "index.html")
    else:
        @app.get("/")
        def no_frontend():
            return PlainTextResponse(
                "Frontend not built. Run `npm install && npm run build` in frontend/, "
                "or use the Vite dev server on :5173.\n"
            )

    return app
