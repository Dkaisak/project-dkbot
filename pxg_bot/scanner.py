from __future__ import annotations

import struct
from typing import Optional

from .memory import Process

VALUE_FORMATS = {
    "i8": "<b", "i32": "<i", "u32": "<I", "i16": "<h", "u16": "<H",
    "u8": "<B", "f32": "<f", "f64": "<d",
}

MAX_REGION = 0x4000000


def scan_value(process: Process, value, vtype: str = "i32", max_hits: int = 200) -> list[int]:
    fmt = VALUE_FORMATS[vtype]
    target = struct.pack(fmt, value)
    return _scan_bytes(process, target, max_hits)


def scan_bytes(process: Process, needle: bytes, max_hits: int = 200) -> list[int]:
    return _scan_bytes(process, needle, max_hits)


def _scan_bytes(process: Process, needle: bytes, max_hits: int) -> list[int]:
    hits: list[int] = []
    for base, size, _protect in process.regions(readable_only=True):
        if size > MAX_REGION:
            continue
        data = process.try_read_bytes(base, size)
        if not data:
            continue
        start = 0
        while len(hits) < max_hits:
            index = data.find(needle, start)
            if index < 0:
                break
            hits.append(base + index)
            start = index + 1
    return hits


def refine(process: Process, addresses: list[int], value, vtype: str = "i32") -> list[int]:
    fmt = VALUE_FORMATS[vtype]
    target = struct.pack(fmt, value)
    return [a for a in addresses if process.try_read_bytes(a, len(target)) == target]


def find_pointers_to(process: Process, target: int, max_offset: int = 0x400, max_hits: int = 64) -> list[tuple[int, int]]:
    ptr_size = process.pointer_size
    fmt = "<I" if ptr_size == 4 else "<Q"
    lo, hi = target, target + max_offset
    results: list[tuple[int, int]] = []
    for base, size, _protect in process.regions(readable_only=True):
        if size > MAX_REGION:
            continue
        data = process.try_read_bytes(base, size)
        if not data:
            continue
        step = ptr_size
        for off in range(0, len(data) - step + 1, step):
            value = struct.unpack_from(fmt, data, off)[0]
            if lo <= value <= hi:
                results.append((base + off, value))
                if len(results) >= max_hits:
                    return results
    return results


def pointer_scan(process: Process, target: int, levels: int = 3, max_offset: int = 0x400) -> list[list[int]]:
    chains: list[list[int]] = []

    def walk(address: int, chain: list[int], depth: int) -> None:
        if depth > levels:
            return
        for location, _value in find_pointers_to(process, address, max_offset=max_offset, max_hits=16):
            module_base = _module_containing(process, location)
            if module_base is not None:
                chains.append([module_base, location] + chain)
                continue
            walk(location, [location] + chain, depth + 1)

    walk(target, [], 1)
    return chains


def _module_containing(process: Process, address: int) -> Optional[int]:
    if hasattr(process, "module_containing"):
        return process.module_containing(address)
    for name, (base, size) in process._modules.items():
        if base <= address < base + size:
            return base
    return None


def dump(process: Process, address: int, size: int = 128) -> str:
    data = process.try_read_bytes(address, size)
    if not data:
        return ""
    lines = []
    for offset in range(0, len(data), 16):
        chunk = data[offset : offset + 16]
        hexpart = " ".join(f"{b:02X}" for b in chunk)
        asciipart = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{address + offset:016X}  {hexpart:<47}  {asciipart}")
    return "\n".join(lines)
