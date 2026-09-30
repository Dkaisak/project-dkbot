from __future__ import annotations

from typing import Optional

from .memory import Process


def parse_pattern(pattern: str) -> tuple[bytes, bytes]:
    mask = []
    data = []
    for token in pattern.split():
        if token in ("?", "??", "**"):
            data.append(0)
            mask.append(0)
        else:
            data.append(int(token, 16))
            mask.append(1)
    return bytes(data), bytes(mask)


def find_pattern(data: bytes, pattern: str, start: int = 0, max_hits: int = 1) -> list[int]:
    sig, mask = parse_pattern(pattern)
    n = len(sig)
    hits: list[int] = []
    if n == 0 or n > len(data):
        return hits
    limit = len(data) - n
    i = start
    while i <= limit:
        j = 0
        while j < n and (mask[j] == 0 or data[i + j] == sig[j]):
            j += 1
        if j == n:
            hits.append(i)
            if len(hits) >= max_hits:
                return hits
            i += n
        else:
            i += 1
    return hits


def scan_module(
    process: Process,
    module_name: str,
    pattern: str,
    max_hits: int = 1,
    chunk_size: int = 0x400000,
) -> list[int]:
    base, size = process.module(module_name)
    hits: list[int] = []
    sig_len = len(pattern.split())
    overlap = max(0, sig_len - 1)
    offset = 0
    while offset < size:
        to_read = min(chunk_size, size - offset)
        data = process.try_read_bytes(base + offset, to_read)
        if data:
            for hit in find_pattern(data, pattern, 0, max_hits - len(hits)):
                abs_hit = base + offset + hit
                hits.append(abs_hit)
                if len(hits) >= max_hits:
                    return hits
        offset += to_read - overlap
        if to_read <= overlap:
            break
    return hits


def scan_regions(
    process: Process,
    pattern: str,
    max_hits: int = 5,
    max_region_size: int = 0x8000000,
) -> list[int]:
    hits: list[int] = []
    for base, size, _protect in process.regions(readable_only=True):
        if size > max_region_size:
            continue
        data = process.try_read_bytes(base, size)
        if not data:
            continue
        for hit in find_pattern(data, pattern, 0, max_hits - len(hits)):
            hits.append(base + hit)
            if len(hits) >= max_hits:
                return hits
    return hits


def resolve_rip_relative(match_address: int, operand_offset: int, process: Process) -> int:
    import struct

    disp = struct.unpack("<i", process.read_bytes(match_address + operand_offset, 4))[0]
    return match_address + operand_offset + 4 + disp
