#!/usr/bin/env python3
"""Deriva direcciones de funciones LuaJIT en pxgme.exe usando pxgme-linux (con
.dynsym) como oraculo.

Ambos binarios compilan el mismo LuaJIT con gcc; solo cambia la ABI (SysV vs
Microsoft). El codigo de una funcion se parece mucho salvo prologo/registros.
Metodo: votacion de k-mers. Se toman todas las ventanas de K bytes del codigo
Linux y se buscan en el .text de Windows; el desplazamiento Windows = (match - i
en Linux) que recibe mas votos y cubre mas rango es el inicio de la funcion.

Salida: tools/agent_loader/lua_offsets.json con VA/RVA y una firma (bytes con
comodines en los bytes que difieren) por funcion.

Uso: python tools/derive_signatures.py
"""
from __future__ import annotations

import argparse
import json
import struct
import sys

import pefile

LUA_FUNCS = [
    "lua_gettop", "lua_pcall", "lua_call", "lua_load",
    "luaL_loadfilex", "luaL_loadbufferx", "luaL_loadstring",
    "lua_pushstring", "lua_pushlstring", "lua_pushnumber", "lua_pushinteger",
    "lua_pushboolean", "lua_pushcclosure", "lua_setfield", "lua_getfield",
    "lua_tostring", "lua_type", "lua_newstate", "luaL_newstate", "lua_close",
    "luaL_openlibs", "lua_error", "luaL_error", "lua_getmetatable",
    "lua_settop", "lua_gettop", "lua_next", "lua_rawget", "lua_rawset",
]


def parse_elf(path: str):
    d = open(path, "rb").read()
    e_shoff, = struct.unpack_from("<Q", d, 0x28)
    e_shentsize, = struct.unpack_from("<H", d, 0x3A)
    e_shnum, = struct.unpack_from("<H", d, 0x3C)
    e_shstrndx, = struct.unpack_from("<H", d, 0x3E)
    secs = []
    for i in range(e_shnum):
        secs.append(struct.unpack_from("<IIQQQQIIQQ", d, e_shoff + i * e_shentsize))
    shstr = secs[e_shstrndx]

    def shname(idx):
        o = shstr[4] + idx
        e = d.find(b"\x00", o)
        return d[o:e].decode()

    by_name = {shname(secs[i][0]): secs[i] for i in range(len(secs))}
    return d, secs, by_name


def elf_symbols(d, by_name, wanted):
    dynsym = by_name[".dynsym"]
    doff, dsize, dent = dynsym[4], dynsym[5], dynsym[9]
    saddr = by_name[".dynstr"][4]
    out = {}
    for k in range(dsize // dent):
        st_name, st_info, st_other, st_shndx, st_value, st_size = struct.unpack_from(
            "<IBBHQQ", d, doff + k * dent)
        so = saddr + st_name
        e = d.find(b"\x00", so)
        nm = d[so:e].decode("latin-1")
        if nm in wanted:
            out[nm] = (st_value, st_size, st_shndx)
    return out


def va_to_bytes(d, secs, va, size):
    for (name, typ, flags, addr, offset, sz, link, info, align, entsize) in secs:
        if addr <= va < addr + sz and offset:
            return d[offset + (va - addr): offset + (va - addr) + size]
    return b""


def vote_function(code: bytes, text: bytes, k: int):
    """Devuelve (start_in_text, span, votes) o None."""
    if len(code) < k:
        return None
    votes: dict[int, list[int]] = {}
    for i in range(0, len(code) - k + 1):
        win = code[i:i + k]
        j = text.find(win)
        while j >= 0:
            start = j - i
            if start >= 0:
                votes.setdefault(start, []).append(i)
            j = text.find(win, j + 1)
    if not votes:
        return None
    best = max(votes.items(), key=lambda kv: (len(kv[1]), max(kv[1]) - min(kv[1])))
    start, is_ = best
    span = max(is_) - min(is_) + k
    return start, span, len(is_)


def build_signature(code: bytes, text: bytes, start: int, k: int = 6):
    """Genera firma (hex con '??' en bytes divergentes) alineando code con text."""
    n = len(code)
    win = text[start:start + n]
    sig = []
    fixed = 0
    for a, b in zip(code, win):
        if a == b:
            sig.append(f"{a:02X}")
            fixed += 1
        else:
            sig.append("??")
    return " ".join(sig), fixed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--linux", default=r"D:\Users\Dkaisak\AppData\Local\Programs\PokeXGames\pxgme-linux")
    ap.add_argument("--win", default=r"D:\Users\Dkaisak\AppData\Local\Programs\PokeXGames\pxgme.exe")
    ap.add_argument("--k", type=int, default=14)
    ap.add_argument("--out", default="tools/agent_loader/lua_offsets.json")
    args = ap.parse_args()

    ld, lsecs, lby = parse_elf(args.linux)
    syms = elf_symbols(ld, lby, set(LUA_FUNCS))

    pe = pefile.PE(args.win, fast_load=True)
    ib = pe.OPTIONAL_HEADER.ImageBase
    text = [s for s in pe.sections if s.Characteristics & 0x20000000][0]
    wd = open(args.win, "rb").read()
    text_data = wd[text.PointerToRawData: text.PointerToRawData + text.SizeOfRawData]
    text_va = ib + text.VirtualAddress

    results = {}
    for fn, (va, size, shndx) in sorted(syms.items()):
        code = va_to_bytes(ld, lsecs, va, size)
        if not code:
            continue
        r = vote_function(code, text_data, args.k)
        if not r:
            print(f"{fn:22} sin coincidencia")
            continue
        start, span, votes = r
        cover = span / max(1, len(code))
        win_va = text_va + start
        win_file = text.PointerToRawData + start
        sig, fixed = build_signature(code, text_data, start)
        good = span >= args.k and votes >= 2 and cover >= 0.3
        results[fn] = {
            "linux_va": hex(va),
            "size": size,
            "win_va": hex(win_va),
            "win_rva": hex(win_va - ib),
            "win_file_offset": hex(win_file),
            "votes": votes,
            "span": span,
            "cover": round(cover, 2),
            "fixed_bytes": fixed,
            "confidence": "alta" if good else "baja",
            "signature": sig,
        }
        print(f"{fn:22} linux={hex(va)} size={size:<4} -> win={hex(win_va)} "
              f"votes={votes:<4} span={span:<4} cover={cover:.2f} {'OK' if good else '?'}")

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)
    print(f"\nescrito {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
