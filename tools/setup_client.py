#!/usr/bin/env python3
"""Detecta el 'mydata' del cliente y ajusta la ruta del agente + config.json.

Uso:
  python3 tools/setup_client.py                 # detecta pxgme-linux
  python3 tools/setup_client.py --pid 1234      # cliente concreto
  python3 tools/setup_client.py --dry-run       # muestra que haria, sin escribir
  python3 tools/setup_client.py --install       # ademas inyecta el agente
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pxg_bot import setup


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--process", default=setup.DEFAULT_PROCESS)
    ap.add_argument("--pid", type=int, default=None)
    ap.add_argument("--agent", default=setup.AGENT)
    ap.add_argument("--install", action="store_true", help="inyecta el agente al terminar")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    res = setup.attach(
        args.config, args.process, args.pid, args.agent,
        install=args.install and not args.dry_run,
        write=not args.dry_run,
    )
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
