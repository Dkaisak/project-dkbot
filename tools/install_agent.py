#!/usr/bin/env python3
"""Instala/actualiza el agente Lua dentro del cliente PXG via gdb (solo lectura + ejecucion protegida)."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT = os.path.join(ROOT, "tools", "pxg_agent.lua")


def find_pid(name: str):
    out = subprocess.run(["pgrep", "-f", name], capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--process", default="pxgme-linux")
    parser.add_argument("--pid", type=int, default=None)
    parser.add_argument("--agent", default=AGENT)
    args = parser.parse_args()

    pid = args.pid or find_pid(args.process)
    if pid is None:
        print(f"proceso '{args.process}' no encontrado", file=sys.stderr)
        return 1

    commands = [
        "set pagination off",
        "set confirm off",
        "break lua_gettop",
        "continue",
        "set $L = $rdi",
        "delete breakpoints",
        f'set $r1 = (int)luaL_loadfile($L, "{args.agent}")',
        "printf \"loadfile=%d\\n\", $r1",
        "set $r2 = (int)lua_pcall($L, 0, 0, 0)",
        "printf \"pcall=%d\\n\", $r2",
        "detach",
    ]
    cmd = ["gdb", "-q", "-p", str(pid), "-batch"]
    for line in commands:
        cmd += ["-ex", line]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    tail = "\n".join(result.stdout.strip().splitlines()[-6:])
    print(f"pid={pid}\n{tail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
