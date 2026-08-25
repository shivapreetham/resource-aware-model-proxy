"""CLI entry point: ``ramp --config ramp.yaml`` (or ``python -m ramp``)."""
from __future__ import annotations

import argparse
import logging

import uvicorn

from . import __version__
from .backend import make_backend
from .config import Config
from .controller import Controller
from .monitor import ResourceMonitor
from .server import create_app


def main() -> None:
    p = argparse.ArgumentParser(
        prog="ramp",
        description="RAMP - RAM-Aware Model Proxy: elastic local LLM daemon",
    )
    p.add_argument("--config", "-c", required=True, help="path to ramp.yaml")
    p.add_argument("--log-level", default="info")
    p.add_argument("--version", action="version", version=f"ramp {__version__}")
    args = p.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    cfg = Config.load(args.config)
    controller = Controller(
        cfg, make_backend(cfg), ResourceMonitor(cfg.ema_alpha, disk_path=cfg.disk_path)
    )
    app = create_app(controller, cfg)

    logging.getLogger("ramp").info(
        "serving on http://%s:%d with %d tier(s), backend=%s",
        cfg.listen_host, cfg.listen_port, len(cfg.tiers), cfg.backend,
    )
    uvicorn.run(app, host=cfg.listen_host, port=cfg.listen_port, log_level=args.log_level)


if __name__ == "__main__":
    main()
