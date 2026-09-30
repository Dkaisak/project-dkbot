from __future__ import annotations

from typing import Any, Callable, Optional

from .memory import MemoryReadError, Process
from .signatures import resolve_rip_relative, scan_module


class OffsetError(RuntimeError):
    pass


def hexint(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return default
    if text.lower().startswith("0x"):
        text = text[2:]
    return int(text, 16)


def resolve_base(process: Process, node: dict, resolver: Optional[Callable[[str], int]] = None) -> int:
    if "from" in node:
        if resolver is None:
            raise OffsetError("nodo con 'from' sin resolvedor disponible")
        loc = resolver(node["from"]) + hexint(node.get("add", 0))
    else:
        mode = node.get("mode", "static")
        if mode == "static":
            loc = hexint(node.get("address", 0))
        elif mode == "signature":
            module = node["module"]
            hits = scan_module(process, module, node["signature"], max_hits=1)
            if not hits:
                raise OffsetError(f"firma no encontrada: {node['signature']}")
            match = hits[0] + hexint(node.get("match_add", 0))
            operand_offset = hexint(node.get("operand_offset", 0))
            if node.get("rip_relative"):
                loc = resolve_rip_relative(match, operand_offset, process)
            else:
                loc = match + operand_offset
            loc += hexint(node.get("add", 0))
        else:
            raise OffsetError(f"modo desconocido: {mode}")

    for off in node.get("chain", []) or []:
        loc = process.read_pointer(loc + hexint(off))
    deref = int(node.get("deref", 1))
    for _ in range(deref):
        loc = process.read_pointer(loc)
    return loc


class Offsets:
    def __init__(self, process: Process, offsets: dict):
        self.proc = process
        self.cfg = offsets
        self._cache: dict[str, int] = {}

    def base(self, name: str) -> int:
        if name in self._cache:
            return self._cache[name]
        node = self.cfg.get(name)
        if node is None:
            raise OffsetError(f"base '{name}' no definida")
        addr = resolve_base(self.proc, node, resolver=self.base)
        if addr:
            self._cache[name] = addr
        return addr

    def invalidate(self) -> None:
        self._cache.clear()


TYPE_READERS = {
    "u8": lambda p, a: p.read_u8(a),
    "u16": lambda p, a: p.read_u16(a),
    "u32": lambda p, a: p.read_u32(a),
    "i16": lambda p, a: p.read(a, "<h"),
    "i32": lambda p, a: p.read_i32(a),
    "f32": lambda p, a: p.read_f32(a),
    "f64": lambda p, a: p.read_f64(a),
    "ptr": lambda p, a: p.read_pointer(a),
    "bool": lambda p, a: bool(p.read_u8(a)),
}


def read_field(process: Process, base: int, spec: Any, default: Any = 0) -> Any:
    if spec is None:
        return default
    try:
        if isinstance(spec, (int, str)):
            return process.read_i32(base + hexint(spec))
        offset = hexint(spec.get("offset", 0))
        ftype = spec.get("type", "i32")
        if ftype == "str":
            return process.read_cstring(base + offset, spec.get("len", 32))
        if ftype in ("str_utf16", "wstr"):
            return process.read_wstring(base + offset, spec.get("len", 32))
        reader = TYPE_READERS.get(ftype)
        if reader is None:
            return default
        return reader(process, base + offset)
    except (MemoryReadError, OSError):
        return default
