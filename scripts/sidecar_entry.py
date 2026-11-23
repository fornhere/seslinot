"""PyInstaller bağımsız `seslinot-server` sidecar giriş noktası."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from seslinot.config import load_config
from seslinot.server import run_server


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--sd-record":
        from seslinot.audio._sd_record import main as sd_main

        return sd_main(sys.argv[2:])

    p = argparse.ArgumentParser(prog="seslinot-server")
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("--host", type=str, default="127.0.0.1")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--token", type=str, default="")
    p.add_argument("--no-daemon", action="store_true")
    args = p.parse_args()
    cfg = load_config(args.config)
    return run_server(
        cfg,
        host=args.host,
        port=args.port,
        token=args.token or None,
        config_path=args.config,
        no_daemon=bool(args.no_daemon),
    )


if __name__ == "__main__":
    raise SystemExit(main())
