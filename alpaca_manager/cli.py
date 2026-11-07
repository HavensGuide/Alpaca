"""Command-line entry point: ``alpaca-manager`` / ``python -m alpaca_manager``."""

from __future__ import annotations

import argparse
import logging
import os
import webbrowser
from threading import Timer


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Alpaca Manager web app.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Interface to bind (default 127.0.0.1 - this computer only).")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--demo", action="store_true",
                        help="Add a simulated demo account on first start (no keys needed).")
    parser.add_argument("--data-dir", help="Where to keep the database (default ~/.alpaca_manager).")
    parser.add_argument("--no-browser", action="store_true", help="Don't open a browser tab.")
    args = parser.parse_args()

    if args.demo:
        os.environ["ALPACA_MANAGER_DEMO"] = "1"
    if args.data_dir:
        os.environ["ALPACA_MANAGER_DATA_DIR"] = args.data_dir

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: the app has no login. Binding to a non-local address lets anyone "
              "on the network place trades with your accounts.")

    import uvicorn

    from .app import create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    app = create_app()
    url = f"http://{'localhost' if args.host in ('127.0.0.1', '::1') else args.host}:{args.port}"
    print(f"Alpaca Manager running at {url}")
    if not args.no_browser:
        Timer(1.5, lambda: webbrowser.open(url)).start()
    # A single worker process: the scheduler must only run once.
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
