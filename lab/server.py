"""Entrypoint: `python -m lab.server`."""
from __future__ import annotations

import sys

import uvicorn

from .api import create_app
from .app import build_services
from .config import Config


def main() -> int:
    config = Config()
    token = config.ensure_owner_token()
    svc = build_services(config)
    if config.run_worker:
        svc.worker.start()
    where = f"http://{config.host}:{config.port}"
    print(f"AI Business Lab running at {where}")
    token_file = config.data_dir / "owner_token"
    print("Owner token: set via LAB_OWNER_TOKEN" if not token_file.exists() or token != token_file.read_text().strip()
          else f"Owner token: read it from {token_file} (it is never printed)")
    if config.host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: listening beyond localhost. Put it behind HTTPS (reverse proxy) before exposing it.", file=sys.stderr)
    uvicorn.run(create_app(svc), host=config.host, port=config.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
