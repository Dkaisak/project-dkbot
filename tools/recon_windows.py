#!/usr/bin/env python3
"""Recon del cliente Windows (pxgme.exe), solo lectura.

Responde las preguntas de la Fase 0 del PLAN-WINDOWS.md:
  - arquitectura del cliente (x64/x86),
  - modulos cargados y si hay una DLL de Lua (lua51.dll/luajit.dll/...),
  - exports que empiecen por 'lua' del exe principal y de las DLL de Lua,
  - directorio del exe y presencia de 'mydata'.

Salida: JSON a stdout (y a tools/agent_loader/recon_windows.json con --save).

Uso:
  python tools/recon_windows.py
  python tools/recon_windows.py --pid 1234
  python tools/recon_windows.py --process pxgme.exe --save
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pxg_bot.memory import (IS_WINDOWS, WindowsProcess, find_pid, kernel32,
                            list_processes)

LUA_DLL_HINTS = ("lua51", "lua5.1", "lua", "luajit")
DEFAULT_OUT = os.path.join(ROOT, "tools", "agent_loader", "recon_windows.json")


def _u16(data: bytes, off: int) -> int:
    return struct.unpack_from("<H", data, off)[0]


def _u32(data: bytes, off: int) -> int:
    return struct.unpack_from("<I", data, off)[0]


def _pe_info(proc: WindowsProcess, base: int) -> dict:
    """Lee cabeceras PE del modulo y la tabla de exports."""
    dos = proc.try_read_bytes(base, 0x40)
    if not dos or dos[:2] != b"MZ":
        return {"error": "sin cabecera MZ"}
    e_lfanew = _u32(dos, 0x3C)
    nt = proc.try_read_bytes(base + e_lfanew, 0x108)
    if not nt or nt[:4] != b"PE\x00\x00":
        return {"error": "sin firma PE"}
    machine = _u16(nt, 4)
    num_sections = _u16(nt, 6)
    size_opt = _u16(nt, 20)
    opt = nt[24:24 + size_opt]
    magic = _u16(opt, 0) if len(opt) >= 2 else 0
    if magic == 0x20B:
        dd_off = 112
        arch = "x64"
    elif magic == 0x10B:
        dd_off = 96
        arch = "x86"
    else:
        dd_off = 0
        arch = f"desconocida(magic=0x{magic:X})"
    exports = []
    if dd_off:
        export_rva = _u32(opt, dd_off)
        if export_rva:
            exports = _read_exports(proc, base, export_rva)
    return {
        "base": base,
        "machine": f"0x{machine:04X}",
        "arch": arch,
        "num_sections": num_sections,
        "export_count": len(exports),
        "lua_exports": sorted(e for e in exports if e.startswith("lua")),
    }


def _read_exports(proc: WindowsProcess, base: int, rva: int) -> list[str]:
    head = proc.try_read_bytes(base + rva, 40)
    if not head or len(head) < 40:
        return []
    num_names = _u32(head, 24)
    addr_names = _u32(head, 32)
    if not num_names or not addr_names or num_names > 100000:
        return []
    ptrs = proc.try_read_bytes(base + addr_names, num_names * 4)
    if not ptrs:
        return []
    names: list[str] = []
    for i in range(num_names):
        name_rva = _u32(ptrs, i * 4)
        if not name_rva:
            continue
        raw = proc.try_read_bytes(base + name_rva, 128)
        if not raw:
            continue
        end = raw.find(b"\x00")
        if end >= 0:
            raw = raw[:end]
        try:
            names.append(raw.decode("ascii"))
        except UnicodeDecodeError:
            continue
    return names


def _exe_path(proc: WindowsProcess, pid: int) -> str:
    """Ruta del ejecutable via QueryFullProcessImageNameW."""
    try:
        k = kernel32
        k.QueryFullProcessImageNameW.restype = ctypes.c_int
        size = ctypes.c_uint(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if k.QueryFullProcessImageNameW(proc.handle, 0, buf, ctypes.byref(size)):
            return buf.value
    except Exception:
        pass
    for name, (base, _size) in proc._modules.items():
        if name.endswith(".exe"):
            return name
    return ""


def _is_wow64(proc: WindowsProcess) -> bool:
    try:
        wow = ctypes.c_int(0)
        if kernel32.IsWow64Process(proc.handle, ctypes.byref(wow)):
            return bool(wow.value)
    except Exception:
        pass
    return False


def _find_mydata(exe_path: str, cwd: str) -> list[str]:
    found = []
    for cand in _mydata_candidates(exe_path, cwd):
        if cand and os.path.isdir(cand):
            found.append(os.path.abspath(cand))
    return found


def _mydata_candidates(exe_path: str, cwd: str) -> list[str]:
    cands = []
    if exe_path:
        exe_dir = os.path.dirname(exe_path)
        cands.append(os.path.join(exe_dir, "mydata"))
        cands.append(os.path.join(exe_dir, "..", "mydata"))
        cands.append(os.path.join(exe_dir, "..", "..", "mydata"))
    if cwd:
        cands.append(os.path.join(cwd, "mydata"))
    return cands


def recon(pid: int, process_name: str) -> dict:
    result: dict = {"pid": pid, "process_name": process_name, "ok": True}
    proc = WindowsProcess.by_pid(pid)
    try:
        exe_path = _exe_path(proc, pid)
        result["exe"] = exe_path
        result["arch"] = "x86" if _is_wow64(proc) else "x64"
        result["pointer_size"] = proc.pointer_size
        cwd = os.path.dirname(exe_path) if exe_path else ""
        result["mydata"] = _find_mydata(exe_path, cwd)

        modules = []
        lua_modules = []
        for name, (base, size) in sorted(proc._modules.items()):
            modules.append({"name": name, "base": base, "size": size})
            if any(h in name for h in LUA_DLL_HINTS):
                lua_modules.append({"name": name, "base": base, "size": size})
        result["modules"] = modules
        result["lua_dlls"] = lua_modules

        lua_exports: dict[str, list[str]] = {}
        for mod in lua_modules:
            info = _pe_info(proc, mod["base"])
            lua_exports[mod["name"]] = info.get("lua_exports", [])
            mod["arch"] = info.get("arch")
            mod["export_count"] = info.get("export_count")

        exe_basename = os.path.basename(exe_path).lower() if exe_path else (process_name.lower() if process_name else "")
        main_info = None
        for name, (base, size) in proc._modules.items():
            if name == exe_basename or name.endswith(".exe"):
                main_info = _pe_info(proc, base)
                main_info["name"] = name
                break
        if main_info:
            lua_exports[f"<main:{main_info['name']}>"] = main_info.get("lua_exports", [])
            result["main_module"] = main_info

        result["lua_exports"] = lua_exports
        result["strategy"] = _strategy(result)
    finally:
        proc.close()
    return result


def _strategy(info: dict) -> dict:
    lua_dlls = info.get("lua_dlls", [])
    exports = info.get("lua_exports", {})
    dll_with_gettop = []
    for mod in lua_dlls:
        exps = exports.get(mod["name"], [])
        if "lua_gettop" in exps and "luaL_loadfile" in exps:
            dll_with_gettop.append(mod["name"])
    main = info.get("main_module") or {}
    main_exports = main.get("lua_exports", [])
    if dll_with_gettop:
        return {
            "mode": "2A-dll",
            "detail": "DLL de Lua con exports -> GetProcAddress",
            "modules": dll_with_gettop,
        }
    if "lua_gettop" in main_exports and "luaL_loadfile" in main_exports:
        return {
            "mode": "2A-exe",
            "detail": "Lua estatico pero el exe exporta lua* -> GetProcAddress",
        }
    return {
        "mode": "2B-signatures",
        "detail": "Lua estatico sin exports -> firmas (revisar signatures.h)",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--process", default="pxgme.exe")
    ap.add_argument("--pid", type=int, default=None)
    ap.add_argument("--save", action="store_true", help=f"escribe {DEFAULT_OUT}")
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    if not IS_WINDOWS:
        print("recon_windows requiere Windows", file=sys.stderr)
        return 2

    pid = args.pid or find_pid(args.process)
    if pid is None:
        procs = list_processes(args.process)
        print(json.dumps({
            "ok": False,
            "error": f"proceso '{args.process}' no encontrado",
            "procesos_vistos": procs,
        }, indent=2))
        return 1

    try:
        info = recon(pid, args.process)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 1

    text = json.dumps(info, indent=2, ensure_ascii=False)
    print(text)
    if args.save:
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
