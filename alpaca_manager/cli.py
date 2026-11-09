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
    parser.add_argument("--check-keys", action="store_true",
                        help="Show what is stored for each account and test the keys with Alpaca, then exit.")
    args = parser.parse_args()

    if args.demo:
        os.environ["ALPACA_MANAGER_DEMO"] = "1"
    if args.data_dir:
        os.environ["ALPACA_MANAGER_DATA_DIR"] = args.data_dir

    if args.check_keys:
        check_keys()
        return

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


def check_keys() -> None:
    """Read-only report of stored credentials. Never prints the secret itself."""
    from sqlalchemy import select

    from .broker import AlpacaBroker, BrokerError
    from .config import Settings
    from .db import Account, make_session_factory
    from .vault import SecretBox

    settings = Settings()
    db_file = settings.data_dir / "alpaca_manager.db"
    print(f"Database: {db_file} ({'found' if db_file.exists() else 'NOT FOUND'})")
    if not db_file.exists():
        return
    vault = SecretBox(settings.data_dir)
    with make_session_factory(settings.db_url)() as s:
        accounts = list(s.scalars(select(Account).order_by(Account.id)))
    if not accounts:
        print("No accounts saved.")
    for a in accounts:
        print(f"\n#{a.id} {a.name!r}  type={a.mode}  data feed={a.data_feed}")
        if a.mode == "demo":
            print("  demo account - no keys needed")
            continue
        key = a.api_key or ""
        print(f"  key ID: {key[:4] + '...' + key[-2:] if len(key) > 6 else '(missing)'} ({len(key)} chars)")
        try:
            secret = vault.decrypt(a.secret_enc) if a.secret_enc else ""
            print(f"  secret: {'stored, decrypts OK' if secret else 'MISSING'} ({len(secret)} chars)")
        except ValueError as exc:
            print(f"  secret: CANNOT DECRYPT - {exc}")
            continue
        if not secret:
            continue
        try:
            info = AlpacaBroker(key, secret, paper=a.mode == "paper", data_feed=a.data_feed).get_account()
            print(f"  Alpaca: OK - account {info['account_number']}, status {info['status']}")
        except BrokerError as exc:
            print(f"  Alpaca: FAILED - {exc}")


if __name__ == "__main__":
    main()
