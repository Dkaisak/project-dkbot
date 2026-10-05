"""Lector del minimapa de OTClient (.otmm).

Formato (OTClient src/client/minimap.cpp):
  u32 "OTMM", u16 start, u16 version=1, u32 flags, string description
  en `start`: repeticion de  u16 x, u16 y, u8 z, u16 len, len bytes zlib
  cada bloque descomprimido son 64*64*3 bytes: (flags, color, speed) por tile.

Flags del tile: 1=WasSeen, 2=NotPathable, 4=NotWalkable.

No carga los 44M de tiles: construye un indice de bloques y descomprime bajo
demanda con cache LRU. Las coordenadas son las mismas del juego (x,y,z).
"""
from __future__ import annotations

import json
import mmap
import os
import struct
import threading
import time
from collections import OrderedDict
from typing import Optional

MMBLOCK = 64
TILE_BYTES = 3
BLOCK_BYTES = MMBLOCK * MMBLOCK * TILE_BYTES
MAX_Z = 15

FLAG_SEEN = 1
FLAG_NOTPATH = 2
FLAG_NOTWALK = 4


def from8bit(color: int) -> tuple[int, int, int]:
    """Color 8-bit de OTClient a RGB 24-bit (esquema 3-3-2)."""
    r = ((color >> 5) & 0x07) * 255 // 7
    g = ((color >> 2) & 0x07) * 255 // 7
    b = (color & 0x03) * 255 // 3
    return r, g, b


class Minimap:
    def __init__(self, path: str, index_cache: Optional[str] = None, cache_blocks: int = 768):
        self.path = path
        self.index_cache = index_cache or (path + ".index.json")
        self.blocks: dict[int, dict[tuple[int, int], tuple[int, int]]] = {}
        self.bbox: dict[int, list[int]] = {}
        self._mm: Optional[mmap.mmap] = None
        self._fh = None
        self._lock = threading.Lock()
        self._lru: "OrderedDict[tuple[int, int, int], bytes]" = OrderedDict()
        self._lru_max = cache_blocks
        self._mtime = 0.0
        self.ready = False
        self.error = ""

    # --- carga ---
    def ensure(self) -> bool:
        if not self.path or not os.path.exists(self.path):
            self.error = "minimap.otmm no encontrado"
            return False
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return False
        if self._mm is not None and mtime == self._mtime:
            return True
        with self._lock:
            self._close_locked()
            try:
                self._fh = open(self.path, "rb")
                self._mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)
            except OSError as exc:
                self.error = str(exc)
                return False
            self._mtime = mtime
            if not self._load_index():
                self._build_index()
                self._save_index()
            self.ready = True
            return True

    def _close_locked(self) -> None:
        if self._mm is not None:
            try:
                self._mm.close()
            except (OSError, ValueError):
                pass
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
        self._mm = None
        self._fh = None
        self._lru.clear()

    def _load_index(self) -> bool:
        try:
            with open(self.index_cache, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if data.get("mtime") != self._mtime or data.get("size") != os.path.getsize(self.path):
                return False
            self.blocks = {}
            self.bbox = {}
            for zs, entries in data.get("blocks", {}).items():
                z = int(zs)
                self.blocks[z] = {(int(bx), int(by)): (int(off), int(ln)) for bx, by, off, ln in entries}
            for zs, bb in data.get("bbox", {}).items():
                self.bbox[int(zs)] = [int(v) for v in bb]
            return bool(self.blocks)
        except (OSError, ValueError, KeyError):
            return False

    def _save_index(self) -> None:
        try:
            data = {
                "mtime": self._mtime,
                "size": os.path.getsize(self.path),
                "blocks": {str(z): [[bx, by, off, ln] for (bx, by), (off, ln) in m.items()]
                           for z, m in self.blocks.items()},
                "bbox": {str(z): bb for z, bb in self.bbox.items()},
            }
            tmp = self.index_cache + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.replace(tmp, self.index_cache)
        except OSError:
            pass

    def _build_index(self) -> None:
        mm = self._mm
        size = len(mm)
        sig, start, ver, _flags = struct.unpack_from("<IHHI", mm, 0)
        if sig != 0x4D4D544F:
            raise ValueError("no es un archivo OTMM")
        off = 12
        (slen,) = struct.unpack_from("<H", mm, off)
        off += 2 + slen
        off = start if 0 < start < size else off
        blocks: dict[int, dict[tuple[int, int], tuple[int, int]]] = {}
        bbox: dict[int, list[int]] = {}
        while off + 5 <= size:
            x, y, z = struct.unpack_from("<HHB", mm, off)
            if z > MAX_Z:
                break
            off += 5
            (ln,) = struct.unpack_from("<H", mm, off)
            off += 2
            if off + ln > size:
                break
            blocks.setdefault(z, {})[(x // MMBLOCK, y // MMBLOCK)] = (off, ln)
            bb = bbox.get(z)
            if bb is None:
                bbox[z] = [x, y, x + MMBLOCK - 1, y + MMBLOCK - 1]
            else:
                bb[0] = min(bb[0], x)
                bb[1] = min(bb[1], y)
                bb[2] = max(bb[2], x + MMBLOCK - 1)
                bb[3] = max(bb[3], y + MMBLOCK - 1)
            off += ln
        self.blocks = blocks
        self.bbox = bbox

    # --- acceso ---
    def _tiles(self, z: int, bx: int, by: int) -> Optional[bytes]:
        entry = self.blocks.get(z, {}).get((bx, by))
        if entry is None:
            return None
        off, ln = entry
        key = (z, bx, by)
        cached = self._lru.get(key)
        if cached is not None:
            self._lru.move_to_end(key)
            return cached
        import zlib

        try:
            raw = zlib.decompress(self._mm[off:off + ln])
        except (zlib.error, ValueError, IndexError, TypeError):
            # el archivo puede estar reescribiendose por el cliente; ignorar
            return None
        self._lru[key] = raw
        if len(self._lru) > self._lru_max:
            self._lru.popitem(last=False)
        return raw

    def tile(self, x: int, y: int, z: int):
        if self._mm is None or z not in self.blocks:
            return None
        bx, by = (x // MMBLOCK) * MMBLOCK, (y // MMBLOCK) * MMBLOCK
        raw = self._tiles(z, bx // MMBLOCK, by // MMBLOCK)
        if raw is None:
            return None
        i = (((y - by) % MMBLOCK) * MMBLOCK + ((x - bx) % MMBLOCK)) * TILE_BYTES
        return raw[i], raw[i + 1], raw[i + 2]

    def seen(self, x: int, y: int, z: int) -> bool:
        t = self.tile(x, y, z)
        return bool(t and (t[0] & FLAG_SEEN))

    def walkable(self, x: int, y: int, z: int) -> bool:
        t = self.tile(x, y, z)
        return bool(t and (t[0] & FLAG_SEEN) and not (t[0] & FLAG_NOTWALK))

    def pathable(self, x: int, y: int, z: int) -> bool:
        t = self.tile(x, y, z)
        return bool(t and (t[0] & FLAG_SEEN) and not (t[0] & FLAG_NOTWALK) and not (t[0] & FLAG_NOTPATH))

    def bounds(self, z: int):
        return self.bbox.get(z)

    def info(self) -> dict:
        return {
            "ready": self.ready,
            "error": self.error,
            "path": self.path,
            "floors": len(self.blocks),
            "blocks": sum(len(m) for m in self.blocks.values()),
            "bbox": self.bbox,
        }

    def check(self, x: int, y: int, z: int) -> dict:
        t = self.tile(x, y, z)
        if not t:
            return {"seen": False, "walkable": False, "pathable": False, "tile": None}
        return {
            "seen": bool(t[0] & FLAG_SEEN),
            "walkable": self.walkable(x, y, z),
            "pathable": self.pathable(x, y, z),
            "tile": list(t),
        }

    # --- render ---
    def region_rgb(self, x0: int, y0: int, x1: int, y1: int, z: int, mode: str = "color") -> tuple[int, int, bytes]:
        w = max(0, x1 - x0 + 1)
        h = max(0, y1 - y0 + 1)
        buf = bytearray(w * h * 3)
        bx0, by0 = x0 // MMBLOCK, y0 // MMBLOCK
        bx1, by1 = x1 // MMBLOCK, y1 // MMBLOCK
        for bxy in range(by0, by1 + 1):
            for bxx in range(bx0, bx1 + 1):
                raw = self._tiles(z, bxx, bxy)
                if raw is None:
                    continue
                base_x, base_y = bxx * MMBLOCK, bxy * MMBLOCK
                for ly in range(MMBLOCK):
                    gy = base_y + ly
                    if gy < y0 or gy > y1:
                        continue
                    row = (gy - y0) * w
                    off = (ly * MMBLOCK) * TILE_BYTES
                    for lx in range(MMBLOCK):
                        gx = base_x + lx
                        if gx < x0 or gx > x1:
                            continue
                        i = off + lx * TILE_BYTES
                        fl, color, _sp = raw[i], raw[i + 1], raw[i + 2]
                        p = (row + (gx - x0)) * 3
                        if not (fl & FLAG_SEEN) or color == 255:
                            buf[p], buf[p + 1], buf[p + 2] = 10, 13, 17
                        elif mode == "walk":
                            if fl & FLAG_NOTWALK:
                                buf[p], buf[p + 1], buf[p + 2] = 60, 30, 34
                            elif fl & FLAG_NOTPATH:
                                buf[p], buf[p + 1], buf[p + 2] = 70, 60, 30
                            else:
                                buf[p], buf[p + 1], buf[p + 2] = 34, 70, 45
                        else:
                            r, g, b = from8bit(color)
                            buf[p], buf[p + 1], buf[p + 2] = r, g, b
        return w, h, bytes(buf)
