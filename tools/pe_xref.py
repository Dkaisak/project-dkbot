#!/usr/bin/env python3
"""Utilidades de análisis de PE x64 para derivar firmas de LuaJIT en pxgme.exe.

- `strings`: localiza literales y sus VA (imagen).
- `xrefs`: busca referencias RIP-relativas (lea/mov) a una VA en .text.
- `calls`: lista los `call rel32` de la funcion que contiene una VA dada.

Uso:
  python tools/pe_xref.py str  "D:\\...\\pxgme.exe" "assets/init.klua"
  python tools/pe_xref.py xref "D:\\...\\pxgme.exe" 0x140ee57f9
  python tools/pe_xref.py func "D:\\...\\pxgme.exe" 0x140fb5600   # disasm alrededor
"""
from __future__ import annotations

import sys

import capstone
import pefile

TEXT_CHAR = 0x20000000


def open_pe(path: str):
    pe = pefile.PE(path, fast_load=True)
    ib = pe.OPTIONAL_HEADER.ImageBase
    return pe, ib


def off_to_va(pe, ib, off: int):
    for s in pe.sections:
        if s.PointerToRawData <= off < s.PointerToRawData + s.SizeOfRawData:
            return ib + s.VirtualAddress + (off - s.PointerToRawData)
    return None


def va_to_off(pe, ib, va: int):
    rva = va - ib
    for s in pe.sections:
        if s.VirtualAddress <= rva < s.VirtualAddress + max(s.Misc_VirtualSize, s.SizeOfRawData):
            if s.PointerToRawData:
                return s.PointerToRawData + (rva - s.VirtualAddress)
    return None


def find_text(pe):
    for s in pe.sections:
        if s.Characteristics & TEXT_CHAR:
            return s
    return None


def iter_text(path):
    pe, ib = open_pe(path)
    text = find_text(pe)
    data = open(path, "rb").read()[text.PointerToRawData:text.PointerToRawData + text.SizeOfRawData]
    return pe, ib, capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64), data, ib + text.VirtualAddress


def cmd_str(path: str, needle: str) -> int:
    raw = open(path, "rb").read()
    nb = needle.encode()
    pe, ib = open_pe(path)
    start = 0
    found = 0
    while True:
        i = raw.find(nb, start)
        if i < 0:
            break
        found += 1
        va = off_to_va(pe, ib, i)
        print(f"0x{i:X} -> VA {hex(va) if va else '?'}")
        start = i + 1
    print(f"{found} ocurrencias")
    return 0


def cmd_xref(path: str, target: int) -> int:
    pe, ib, md, data, text_va = iter_text(path)
    md.detail = True
    hits = []
    for insn in md.disasm(data, text_va):
        for op in insn.operands:
            if op.type == capstone.x86.X86_OP_MEM and op.mem.base == capstone.x86.X86_REG_RIP:
                if insn.address + insn.size + op.mem.disp == target:
                    hits.append((insn.address, insn.mnemonic, insn.op_str))
    for a, m, o in hits:
        print(f"0x{a:X}: {m} {o}")
    print(f"{len(hits)} xrefs a 0x{target:X}")
    return 0


def _pdata_funcs(pe):
    """Lista de (start_rva, end_rva) de .pdata (x64 RUNTIME_FUNCTION)."""
    sec = None
    for s in pe.sections:
        if s.Name.rstrip(b"\x00") == b".pdata":
            sec = s
            break
    if not sec:
        return []
    raw = sec.get_data()
    funcs = []
    for i in range(0, len(raw) - 11, 12):
        start, end, unwind = __import__("struct").unpack_from("<III", raw, i)
        if start and end and end > start:
            funcs.append((start, end))
    return funcs


def cmd_str_refs(path: str, start: int, end: int) -> int:
    """Lista todos los literales (lea rip-rel) referenciados en [start,end)."""
    pe = pefile.PE(path, fast_load=True)
    ib = pe.OPTIONAL_HEADER.ImageBase
    full = open(path, "rb").read()

    def va_to_off(va):
        rva = va - ib
        for s in pe.sections:
            if s.VirtualAddress <= rva < s.VirtualAddress + max(s.Misc_VirtualSize, s.SizeOfRawData):
                return s.PointerToRawData + (rva - s.VirtualAddress) if s.PointerToRawData else None
        return None

    off = va_to_off(start)
    chunk = full[off: off + (end - start)]
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = True
    seen = {}
    for insn in md.disasm(chunk, start):
        for op in insn.operands:
            if op.type == capstone.x86.X86_OP_MEM and op.mem.base == capstone.x86.X86_REG_RIP:
                tgt = insn.address + insn.size + op.mem.disp
                to = va_to_off(tgt)
                if to is None:
                    continue
                raw = full[to:to + 80]
                s = raw.split(b"\x00")[0]
                try:
                    txt = s.decode("ascii")
                except UnicodeDecodeError:
                    continue
                if txt and all(32 <= ord(c) < 127 or c in "\t" for c in txt):
                    seen[tgt] = (txt, insn.address)
    for tgt, (txt, at) in sorted(seen.items(), key=lambda kv: kv[1][1]):
        print(f"0x{at:X} -> 0x{tgt:X}: {txt!r}")
    print(f"{len(seen)} literales")
    return 0


def cmd_xref_pdata(path: str, target: int) -> int:
    """Desensambla funcion a funcion (guiado por .pdata) buscando xrefs."""
    pe = pefile.PE(path, fast_load=True)
    ib = pe.OPTIONAL_HEADER.ImageBase
    full = open(path, "rb").read()

    def rva_to_off(rva):
        for s in pe.sections:
            if s.VirtualAddress <= rva < s.VirtualAddress + max(s.Misc_VirtualSize, s.SizeOfRawData):
                return s.PointerToRawData + (rva - s.VirtualAddress) if s.PointerToRawData else None
        return None

    funcs = _pdata_funcs(pe)
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = True
    total = 0
    hits = []
    for start, end in funcs:
        off = rva_to_off(start)
        if off is None:
            continue
        chunk = full[off: off + (end - start)]
        total += 1
        for insn in md.disasm(chunk, ib + start):
            for op in insn.operands:
                if op.type == capstone.x86.X86_OP_MEM and op.mem.base == capstone.x86.X86_REG_RIP:
                    if insn.address + insn.size + op.mem.disp == target:
                        hits.append((start, insn.address, insn.mnemonic, insn.op_str))
    for fn, a, m, o in hits:
        print(f"func 0x{ib+fn:X} @0x{a:X}: {m} {o}")
    print(f"{len(hits)} xrefs a 0x{target:X} en {total} funciones .pdata")
    return 0



def cmd_dump(path: str, va: int, before: int = 0, after: int = 0x120) -> int:
    """Desensambla alrededor de una VA."""
    pe, ib = open_pe(path)
    off = va_to_off(pe, ib, va)
    if off is None:
        print("VA fuera de sección"); return 1
    data = open(path, "rb").read()
    chunk = data[off - before: off + after]
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = True
    for insn in md.disasm(chunk, va - before):
        mark = "  <== " if insn.address == va else "      "
        print(f"{mark}0x{insn.address:X}: {insn.mnemonic} {insn.op_str}")
    return 0


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    mode = sys.argv[1]
    if mode == "str":
        return cmd_str(sys.argv[2], sys.argv[3])
    if mode == "xref":
        return cmd_xref(sys.argv[2], int(sys.argv[3], 16))
    if mode == "xrefp":
        return cmd_xref_pdata(sys.argv[2], int(sys.argv[3], 16))
    if mode == "strrefs":
        return cmd_str_refs(sys.argv[2], int(sys.argv[3], 16), int(sys.argv[4], 16))
    if mode == "dump":
        before = int(sys.argv[4], 0) if len(sys.argv) > 4 else 0
        after = int(sys.argv[5], 0) if len(sys.argv) > 5 else 0x120
        return cmd_dump(sys.argv[2], int(sys.argv[3], 16), before, after)
    print("modo desconocido")
    return 1


if __name__ == "__main__":
    sys.exit(main())
