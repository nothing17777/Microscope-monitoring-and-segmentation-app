"""FastAPI app: status, history, specs, failures, and a 2s WebSocket push."""
import asyncio
import contextlib
import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import specs as specs_mod
from .config import Config
from .db import Database
from .telemetry import Sampler
from .worker import Worker

log = logging.getLogger("app")

FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"


def create_app(cfg: Config) -> FastAPI:
    cfg.input_dir.mkdir(parents=True, exist_ok=True)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)

    db = Database(cfg.db_path)
    worker = Worker(cfg, db)
    sampler = Sampler(cfg, db, worker)

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
