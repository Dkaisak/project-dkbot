"""Localiza un objeto Player/LocalPlayer por el nombre embebido en memoria.

Busca la cadena del nombre y, para cada aparicion, comprueba si hay un
puntero a una vtable (dentro del modulo principal) justo antes: eso indica
que la cadena vive dentro de un objeto C++ (std::string SSO o buffer).
"""
from __future__ import annotations

import argparse
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pxg_bot.memory import find_pid, open_process
from tools.pxg_recon import exe_path, maps


def module_range(pid: int, exe: str) -> tuple[int, int]:
    lo, hi = None, 0
    for entry in maps(pid):
        if entry["path"] == exe or entry["path"].endswith(os.path.basename(exe)):
            lo = entry["start"] if lo is None else min(lo, entry["start"])
            hi = max(hi, entry["end"])
    return (lo or 0, hi)


def search_name(proc, name: str, module_lo: int, module_hi: int, max_hits: int = 200):
    needle = name.encode("utf-8")
    results = []
    for entry in maps(proc.pid):
        if "r" not in entry["perms"] or "w" not in entry["perms"]:
            continue
        address = entry["start"]
        while address < entry["end"]:
            size = min(0x400000, entry["end"] - address)
            data = proc.try_read_bytes(address, size)
            if data:
                start = 0
                while True:
                    index = data.find(needle, start)
                    if index < 0:
                        break
                    string_addr = address + index
                    base = _find_object_base(proc, string_addr, module_lo, module_hi)
                    results.append((string_addr, base, string_addr - base if base else None))
                    if len(results) >= max_hits:
                        return results
                    start = index + 1
            address += size
    return results


def _find_object_base(proc, string_addr: int, module_lo: int, module_hi: int, back: int = 0x400):
    window = proc.try_read_bytes(string_addr - back, back)
    if not window:
        return None
    for off in range(0, back - 8, 8):
        value = struct.unpack_from("<Q", window, off)[0]
        if module_lo <= value < module_hi:
            return string_addr - back + off
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    parser.add_argument("--pid", type=int, default=None)
    parser.add_argument("--process", default="pxgme-linux")
    args = parser.parse_args()

    pid = args.pid or find_pid(args.process)
    proc = open_process(pid=pid)
    exe = exe_path(pid)
    lo, hi = module_range(pid, exe)
    print(f"pid={pid} modulo=0x{lo:X}-0x{hi:X}")
    hits = search_name(proc, args.name, lo, hi)
    print(f"apariciones de {args.name!r}: {len(hits)}")
    for string_addr, base, offset in hits[:30]:
        if base is not None:
            vptr = proc.try_read_pointer(base) or 0
            print(f"  str@0x{string_addr:X} vptr?@0x{base:X}=0x{vptr:X} offset_name=0x{offset:X}")
        else:
            print(f"  str@0x{string_addr:X} (sin vtable cerca)")
    proc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
