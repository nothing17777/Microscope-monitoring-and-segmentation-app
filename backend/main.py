"""Entry point: python -m backend.main [flags]"""
import logging

import uvicorn

from .app import create_app
from .config import load_config


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s",
    )
    cfg = load_config()
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
