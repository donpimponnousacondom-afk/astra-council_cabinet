from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import shutil
import sqlite3
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Hortator council runtime")
    parser.add_argument("--data-dir", default=os.getenv("HORTATOR_DATA_DIR", "data"))
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Run the API, Discord clients and built dashboard in one process")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    sub.add_parser("init", help="Initialize configuration and encrypted credential storage")
    sub.add_parser("doctor", help="Show configuration readiness without contacting Discord/providers")
    sub.add_parser("password", help="Set a new dashboard password interactively")
    backup = sub.add_parser("backup", help="Create a consistent SQLite backup and a separate encryption key")
    backup.add_argument("destination")
    args = parser.parse_args()
    directory = Path(args.data_dir).resolve()
    os.environ["HORTATOR_DATA_DIR"] = str(directory)
    if args.command == "serve":
        import uvicorn
        print(f"Dashboard: http://{args.host}:{args.port}")
        print(f"First-run password file: {directory / 'initial-password'} (unless HORTATOR_ADMIN_PASSWORD is set)")
        uvicorn.run("hortator.app:app", host=args.host, port=args.port, workers=1, proxy_headers=False)
        return
    if args.command == "backup":
        destination = Path(args.destination).resolve()
        destination.mkdir(parents=True, exist_ok=False)
        destination.chmod(0o700)
        with sqlite3.connect(directory / "council.sqlite3") as source, sqlite3.connect(destination / "council.sqlite3") as target:
            source.backup(target)
        (destination / "council.sqlite3").chmod(0o600)
        key = os.getenv("HORTATOR_MASTER_KEY") or (directory / "master.key").read_text()
        (destination / "master.key").write_text(key)
        (destination / "master.key").chmod(0o600)
        if (directory / "artifacts").exists():
            shutil.copytree(directory / "artifacts", destination / "artifacts")
        print(f"Backup saved to {destination}. Store its encryption key separately from the database.")
        return
    from .app import Kernel
    from .security import password_hash
    k = Kernel(directory)
    try:
        if args.command == "password":
            password = getpass.getpass("New dashboard password (12+ characters): ")
            if len(password) < 12 or password != getpass.getpass("Repeat password: "):
                raise SystemExit("Password was too short or did not match")
            k.vault.put("auth/password", password_hash(password))
            k.store.execute("DELETE FROM auth_sessions")
            (directory / "initial-password").unlink(missing_ok=True)
            print("Dashboard password updated; existing sessions revoked.")
        elif args.command == "doctor":
            for bot in k.store.list("bots"):
                print(bot["name"] + ": " + ("; ".join(k.service.readiness(bot)) or "Configuration ready (external connectivity unverified)"))
        else:
            print(f"Council initialized at {directory}")
            print(f"Dashboard owner: 1482143139828596916. Initial password: {directory / 'initial-password'}")
            print("All sample bots are disabled. Build the dashboard and run: uv run hortator serve")
    finally:
        asyncio.run(k.close())


if __name__ == "__main__":
    main()
