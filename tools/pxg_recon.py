"""Reconocimiento RTTI sobre el cliente pxgme-linux en vivo.

Resuelve, a partir de los nombres de tipo RTTI (ej. '11LocalPlayer'),
la direccion del type_info y de la vtable correspondiente, y opcionalmente
busca instancias (objetos cuyo primer puntero apunta a esa vtable).
"""
from __future__ import annotations

import argparse
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pxg_bot.memory import find_pid, open_process


def exe_path(pid: int) -> str:
    return os.readlink(f"/proc/{pid}/exe")


def maps(pid: int):
    entries = []
    with open(f"/proc/{pid}/maps", "r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            parts = line.split()
            if len(parts) < 5:
                continue
            start_s, end_s = parts[0].split("-")
            entries.append({
                "start": int(start_s, 16),
                "end": int(end_s, 16),
                "perms": parts[1],
                "path": parts[-1] if len(parts) >= 6 else "",
            })
    return entries


def read_segments(proc, predicate, chunk: int = 0x1000000):
    segments = []
    for entry in maps(proc.pid):
        if not predicate(entry):
            continue
        if "r" not in entry["perms"]:
            continue
        address, end = entry["start"], entry["end"]
        while address < end:
            size = min(chunk, end - address)
            data = proc.try_read_bytes(address, size)
            if data:
                segments.append((address, data))
            address += size
    return segments


def find_bytes(segments, needle: bytes):
    hits = []
    for base, data in segments:
        start = 0
        while True:
            index = data.find(needle, start)
            if index < 0:
                break
            hits.append(base + index)
            start = index + 1
    return hits


def find_pointer(segments, value: int, align8: bool = True):
    needle = struct.pack("<Q", value)
    hits = []
    for base, data in segments:
        start = 0
        while True:
            index = data.find(needle, start)
            if index < 0:
                break
            address = base + index
            if not align8 or address % 8 == 0:
                hits.append(address)
            start = index + 1
    return hits


def read_u64(segments, address: int):
    for base, data in segments:
        if base <= address < base + len(data) - 7:
            return struct.unpack_from("<Q", data, address - base)[0]
    return None


def resolve_class(segments, cls: str):
    name = f"{len(cls)}{cls}".encode()
    name_addrs = find_bytes(segments, name + b"\x00")
    result = {"class": cls, "name_addrs": name_addrs, "typeinfos": [], "vtables": []}
    for name_addr in name_addrs:
        for name_slot in find_pointer(segments, name_addr):
            typeinfo = name_slot - 8
            result["typeinfos"].append(typeinfo)
            for ti_slot in find_pointer(segments, typeinfo):
                vtable = ti_slot - 8
                offset_to_top = read_u64(segments, vtable)
                result["vtables"].append((vtable, offset_to_top))
    return result


def find_instances(proc, vtable: int, chunk: int = 0x800000):
    needle = struct.pack("<Q", vtable)
    hits = []
    scanned = 0
    for entry in maps(proc.pid):
        if "w" not in entry["perms"]:
            continue
        address = entry["start"]
        while address < entry["end"]:
            size = min(chunk, entry["end"] - address)
            data = proc.try_read_bytes(address, size)
            if data:
                scanned += len(data)
                start = 0
                while True:
                    index = data.find(needle, start)
                    if index < 0:
                        break
                    hits.append(address + index)
                    start = index + 1
            address += size
    return hits, scanned


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, default=None)
    parser.add_argument("--process", default="pxgme-linux")
    parser.add_argument("--instances", action="store_true",
                        help="busca objetos cuyo vptr apunte a la vtable")
    parser.add_argument("--classes", nargs="+", default=[
        "LocalPlayer", "Player", "Creature", "Thing", "Game", "Map", "ProtocolGame", "OTProtocol",
    ])
    args = parser.parse_args()

    pid = args.pid or find_pid(args.process)
    if pid is None:
        print(f"proceso '{args.process}' no encontrado", file=sys.stderr)
        return 1
    proc = open_process(pid=pid)
    print(f"pid={pid} exe={exe_path(pid)}")

    exe = exe_path(pid)
    module = read_segments(proc, lambda e: e["path"] == exe or e["path"].endswith(os.path.basename(exe)))
    total = sum(len(d) for _, d in module)
    print(f"segmentos del modulo: {len(module)} ({total} bytes)")

    for cls in args.classes:
        info = resolve_class(module, cls)
        if not info["name_addrs"]:
            print(f"[{cls}] sin nombre RTTI")
            continue
        vtables = sorted({v for v, _ in info["vtables"]})
        print(f"[{cls}] typeinfo={[hex(t) for t in set(info['typeinfos'])]} "
              f"vtables={[hex(v) for v in vtables]}")
        if args.instances:
            for vtable in vtables:
                hits, scanned = find_instances(proc, vtable)
                if hits:
                    print(f"    instancias ({scanned/1e6:.0f} MB): {[hex(h) for h in hits[:10]]}")
    proc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
