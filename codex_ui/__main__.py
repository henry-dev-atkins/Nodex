from __future__ import annotations

import argparse
import os
import threading
import time

import uvicorn

from backend.app.main import create_app
from backend.app.settings import load_settings
from backend.app.util import open_browser


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m codex_ui")
    subparsers = parser.add_subparsers(dest="command", required=True)

    dev_parser = subparsers.add_parser("dev", help="Start the local Codex UI wrapper server")
    dev_parser.add_argument("--host", default=None)
    dev_parser.add_argument("--port", type=int, default=None)
    dev_parser.add_argument("--no-browser", action="store_true")

    args = parser.parse_args()
    if args.command != "dev":
        parser.error(f"Unsupported command: {args.command}")

    if args.host:
        os.environ["CODEX_UI_HOST"] = args.host
    if args.port:
        os.environ["CODEX_UI_PORT"] = str(args.port)
    if args.no_browser:
        os.environ["CODEX_UI_OPEN_BROWSER"] = "0"

    settings = load_settings()
    app = create_app()
    url = f"http://{settings.host}:{settings.port}/"

    if settings.launch_browser and not args.no_browser:
        threading.Thread(target=_delayed_open, args=(url,), daemon=True).start()

    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


def _delayed_open(url: str) -> None:
    time.sleep(1.0)
    open_browser(url)


if __name__ == "__main__":
    main()
