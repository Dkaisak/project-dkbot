#!/usr/bin/env python3
"""CLI: inyecta el agente Lua en pxgme.exe (Windows).

El nucleo esta en `pxg_bot.inject` (reutilizado por la GUI). Ver
`tools/agent_loader/README.md`.

Uso:
  python tools/inject_windows.py
  python tools/inject_windows.py --pid 1234 --dll <dll> --agent <lua> --dir <mydata>
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pxg_bot.inject import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
