"""Utilidades para 'atachar' el cliente: detectar su mydata, ajustar la ruta del
agente + config, e inyectar el agente.

- Linux: inyeccion via gdb (`tools/install_agent.py`).
- Windows: inyeccion via DLL (`tools/inject_windows.py` + `agent_loader`).

Reutilizado por `tools/setup_client.py` y por el boton de la GUI web.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys

from . import paths

ROOT = paths.APP_DIR
AGENT = os.path.join(ROOT, "tools", "pxg_agent.lua")
INSTALL = os.path.join(paths.BUNDLE_DIR, "tools", "install_agent.py")
INJECT_WINDOWS = os.path.join(paths.BUNDLE_DIR, "tools", "inject_windows.py")
DLL = os.path.join(ROOT, "tools", "agent_loader", "pxg_agent_loader.dll")

IS_WINDOWS = sys.platform == "win32"
DEFAULT_PROCESS = "pxgme.exe" if IS_WINDOWS else "pxgme-linux"

LUA_KEYS = [
    "state_file", "cmd_file", "control_file", "status_file", "minimap_file",
    "shiny_file", "pokemon_skills_file", "ignore_file", "routes_file",
]

# Linea del agente que fija la ruta por defecto (se reescribe en el attach de
# Linux; en Windows la DLL inyecta _G.PXG_DIR).
_AGENT_DIR_RE = re.compile(r'^local DIR = _G\.PXG_DIR or ".*"', re.M)


def find_pid(name: str):
    from .memory import find_pid as _find
    return _find(name)


def _query_exe_windows(pid: int):
    try:
        from .memory import kernel32
    except Exception:
        return None
    try:
        kernel32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            size = ctypes.c_uint32(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            ok = kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
            return buf.value if ok else None
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def proc_exe(pid: int):
    if IS_WINDOWS:
        return _query_exe_windows(pid)
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return None


def proc_cwd(pid: int):
    if IS_WINDOWS:
        # En Windows el cwd no es fiable / no accesible sin PEB; usar el dir del
        # ejecutable (el cliente resuelve 'assets/' relativo a su instalacion).
        exe = proc_exe(pid)
        return os.path.dirname(exe) if exe else None
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def _mydata_candidates(exe, cwd):
    cands = []
    if exe:
        exe_dir = os.path.dirname(exe)
        cands.append(os.path.join(exe_dir, "mydata"))
        parent = os.path.dirname(exe_dir.rstrip("\\/"))
        if parent:
            cands.append(os.path.join(parent, "mydata"))
    if cwd:
        cands.append(os.path.join(cwd, "mydata"))
    return cands


def detect_mydata(pid: int):
    exe = proc_exe(pid)
    cwd = proc_cwd(pid)
    for c in _mydata_candidates(exe, cwd):
        if c and os.path.isdir(c):
            return c
    return None


def rebase(path: str, old_base, new_base: str) -> str:
    if not path:
        return path
    norm = path.replace("\\", "/")
    if old_base:
        old = old_base.replace("\\", "/")
        if norm.startswith(old):
            return (new_base + norm[len(old):]).replace("\\", "/")
    return os.path.join(new_base, os.path.basename(norm)).replace("\\", "/")


def update_config(cfg_path: str, new_base: str, exe=None) -> dict:
    """Reescribe las rutas de `lua.*`/`ui.log_file` al nuevo mydata. Devuelve el cfg."""
    with open(cfg_path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    old_base = os.path.dirname((cfg.get("lua") or {}).get("state_file", "")) or None
    lua = cfg.setdefault("lua", {})
    for k in LUA_KEYS:
        lua[k] = rebase(lua.get(k, ""), old_base, new_base)
    ui = cfg.setdefault("ui", {})
    ui["log_file"] = rebase(ui.get("log_file", ""), old_base, new_base)
    if exe:
        cfg["process_name"] = os.path.basename(exe)
    tmp = cfg_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, cfg_path)
    return cfg


def update_agent_dir(agent_path: str, new_base: str) -> str:
    # Barras normales: Lua 5.1 no admite '\U' etc. en literales de cadena.
    forward = new_base.replace("\\", "/")
    with open(agent_path, encoding="utf-8") as fh:
        text = fh.read()
    return _AGENT_DIR_RE.sub(f'local DIR = _G.PXG_DIR or "{forward}"', text, count=1)


def _python_interpreter() -> str:
    """Interprete Python real para lanzar scripts auxiliares (Linux).

    Congelado con PyInstaller, `sys.executable` es el propio binario `dkbot`,
    que no puede ejecutar `tools/install_agent.py`; se usa el `python3` del
    sistema (con `python` como alternativa).
    """
    if not paths.IS_FROZEN:
        return sys.executable
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            return found
    return sys.executable


def install_agent(pid: int, agent_path: str = AGENT, mydata: str = None):
    """Inyecta el agente. Devuelve (ok, salida).

    Windows -> DLL (en proceso, `pxg_bot.inject`); Linux -> gdb.
    """
    if IS_WINDOWS:
        if not os.path.isfile(DLL):
            return False, (
                f"falta la DLL {DLL}; compilala con tools/agent_loader/build_windows.bat "
                f"(o mingw32-make) antes de inyectar"
            )
        from . import inject as inject_mod

        res = inject_mod.inject_agent(pid, dll=DLL, agent=agent_path, mydata=mydata,
                                      log=(os.path.join(mydata, "pxg_bot.log") if mydata else None),
                                      suspend=True, verify=True)
        ok = bool(res.get("ok"))
        return ok, json.dumps(res, ensure_ascii=False)
    try:
        res = subprocess.run(
            [_python_interpreter(), INSTALL, "--pid", str(pid), "--agent", agent_path],
            capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    ok = "loadfile=0" in res.stdout and "pcall=0" in res.stdout
    return ok, res.stdout.strip()


def attach(cfg_path: str, process_name: str = None, pid=None,
           agent_path: str = AGENT, install: bool = True, write: bool = True) -> dict:
    """Detecta el cliente, ajusta rutas y (opcionalmente) inyecta el agente."""
    process_name = process_name or DEFAULT_PROCESS
    pid = pid or find_pid(process_name)
    if pid is None:
        return {"ok": False, "error": f"proceso '{process_name}' no encontrado"}
    exe = proc_exe(pid)
    new_base = detect_mydata(pid)
    if not new_base:
        return {"ok": False, "error": "no se encontro 'mydata' cerca del cliente",
                "pid": pid, "exe": exe}
    result = {"ok": True, "pid": pid, "exe": exe, "mydata": new_base}
    if write:
        # LEER el agente ANTES de abrir en "w" (que lo trunca)
        new_agent = update_agent_dir(agent_path, new_base)
        shutil.copy2(agent_path, agent_path + ".bak")
        shutil.copy2(cfg_path, cfg_path + ".bak")
        cfg = update_config(cfg_path, new_base, exe)
        with open(agent_path, "w", encoding="utf-8") as fh:
            fh.write(new_agent)
        result["process_name"] = cfg.get("process_name")
    if install:
        ok, out = install_agent(pid, agent_path, mydata=new_base)
        result["installed"] = ok
        result["install_output"] = out
    return result
