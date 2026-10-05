#!/usr/bin/env python3
"""Ejecuta un chunk Lua dentro del cliente (via gdb) y lee la global _G.PXG_DUMP.

Uso: python3 tools/lua_probe.py <archivo.lua> [--process pxgme-linux]
"""
from __future__ import annotations

import argparse
import os
import re
import struct
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pxg_bot.memory import find_pid, open_process

MASK = (1 << 47) - 1
LJ_TSTR = (~4) & 0xFFFFFFFF


def inject(lua_path: str, pid: int):
    commands = [
        "set pagination off", "set confirm off",
        "break lua_gettop", "continue",
        "set $L = $rdi", 'printf "L=%p\\n", $L',
        "delete breakpoints",
        f'set $r1 = (int)luaL_loadfile($L, "{lua_path}")', 'printf "loadfile=%d\\n", $r1',
        "set $r2 = (int)lua_pcall($L, 0, 0, 0)", 'printf "pcall=%d\\n", $r2',
        "detach",
    ]
    cmd = ["gdb", "-q", "-p", str(pid), "-batch"]
    for line in commands:
        cmd += ["-ex", line]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout
    m = re.search(r"L=0x([0-9a-fA-F]+)", out)
    if not m:
        print(out[-400:])
        return None
    return int(m.group(1), 16)


def read_global_string(proc, L: int, name: str):
    def q(a):
        return struct.unpack("<Q", proc.read_bytes(a, 8))[0]

    def u32(a):
        return struct.unpack("<I", proc.read_bytes(a, 4))[0]

    def dec(v):
        it64 = v - (1 << 64) if v >= (1 << 63) else v
        return (it64 >> 47) & 0xFFFFFFFF, v & MASK

    env = q(L + 72)
    node = q(env + 40)
    hmask = u32(env + 52)
    for i in range(hmask + 1):
        np_ = node + i * 24
        try:
            kt, kp = dec(q(np_ + 8))
            if kt == LJ_TSTR and proc.read_cstring(kp + 24, u32(kp + 20)) == name:
                vt, vp = dec(q(np_))
                if vt == LJ_TSTR:
                    return proc.read_bytes(vp + 24, u32(vp + 20)).decode("latin-1")
                return None
        except Exception:
            continue
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("lua")
    ap.add_argument("--process", default="pxgme-linux")
    args = ap.parse_args()
    pid = find_pid(args.process)
    if pid is None:
        print("cliente no encontrado"); return 1
    L = inject(args.lua, pid)
    if not L:
        return 1
    proc = open_process(pid=pid)
    try:
        text = read_global_string(proc, L, "PXG_DUMP")
        print(text if text is not None else "(PXG_DUMP no definido)")
    finally:
        proc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
