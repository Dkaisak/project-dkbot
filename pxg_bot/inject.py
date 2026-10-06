"""Inyector de la DLL del agente en pxgme.exe (Windows, ctypes/stdlib).

Nucleo reutilizable por `tools/inject_windows.py` (CLI) y por `pxg_bot.setup`
(boton "Attach" de la GUI), para que funcione tambien dentro de un .exe
congelado (sin lanzar subprocesos de Python).
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes

from . import paths
from .memory import find_pid

DEFAULT_PROCESS = "pxgme.exe"
DEFAULT_DLL = os.path.join(paths.APP_DIR, "tools", "agent_loader", "pxg_agent_loader.dll")
DEFAULT_AGENT = os.path.join(paths.APP_DIR, "tools", "pxg_agent.lua")

IS_WINDOWS = sys.platform == "win32"
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if IS_WINDOWS else None

PROCESS_CREATE_THREAD = 0x0002
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04


def _setup() -> None:
    if not IS_WINDOWS:
        return
    k = kernel32
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.VirtualAllocEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
                                 wintypes.DWORD, wintypes.DWORD]
    k.VirtualAllocEx.restype = ctypes.c_void_p
    k.VirtualFreeEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
                                wintypes.DWORD]
    k.WriteProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    k.GetModuleHandleW.restype = wintypes.HMODULE
    k.GetProcAddress.argtypes = [wintypes.HMODULE, wintypes.LPCSTR]
    k.GetProcAddress.restype = ctypes.c_void_p
    k.CreateRemoteThread.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
                                     ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.DWORD)]
    k.CreateRemoteThread.restype = wintypes.HANDLE
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.WaitForSingleObject.restype = wintypes.DWORD


def detect_mydata(pid: int):
    try:
        from . import setup
        return setup.detect_mydata(pid)
    except Exception:
        return None


def write_config(dll_path: str, agent: str, mydata: str, log: str, suspend: bool) -> str:
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(dll_path)), "pxg_agent_dll.json")
    cfg = {
        "agent": os.path.abspath(agent),
        "dir": os.path.abspath(mydata),
        "log": os.path.abspath(log),
        "suspend": bool(suspend),
    }
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    return cfg_path


def inject(pid: int, dll_path: str) -> dict:
    access = (PROCESS_CREATE_THREAD | PROCESS_QUERY_INFORMATION | PROCESS_VM_OPERATION
              | PROCESS_VM_WRITE | PROCESS_VM_READ)
    h = kernel32.OpenProcess(access, False, pid)
    if not h:
        return {"ok": False, "error": f"OpenProcess fallo (err {ctypes.get_last_error()})"}

    result = {"ok": False, "pid": pid, "dll": dll_path}
    try:
        dll_abs = os.path.abspath(dll_path)
        path_bytes = (dll_abs + "\x00").encode("utf-16-le")
        addr = kernel32.VirtualAllocEx(h, None, len(path_bytes), MEM_COMMIT | MEM_RESERVE,
                                       PAGE_READWRITE)
        if not addr:
            result["error"] = f"VirtualAllocEx fallo (err {ctypes.get_last_error()})"
            return result
        written = ctypes.c_size_t(0)
        if not kernel32.WriteProcessMemory(h, addr, path_bytes, len(path_bytes),
                                           ctypes.byref(written)):
            result["error"] = f"WriteProcessMemory fallo (err {ctypes.get_last_error()})"
            kernel32.VirtualFreeEx(h, addr, 0, MEM_RELEASE)
            return result
        k32 = kernel32.GetModuleHandleW("kernel32.dll")
        load = kernel32.GetProcAddress(k32, b"LoadLibraryW")
        if not load:
            result["error"] = "GetProcAddress(LoadLibraryW) fallo"
            kernel32.VirtualFreeEx(h, addr, 0, MEM_RELEASE)
            return result
        thread_id = wintypes.DWORD(0)
        th = kernel32.CreateRemoteThread(h, None, 0, ctypes.c_void_p(load), addr, 0,
                                         ctypes.byref(thread_id))
        if not th:
            result["error"] = f"CreateRemoteThread fallo (err {ctypes.get_last_error()})"
            kernel32.VirtualFreeEx(h, addr, 0, MEM_RELEASE)
            return result
        kernel32.WaitForSingleObject(th, 15000)
        kernel32.CloseHandle(th)
        kernel32.VirtualFreeEx(h, addr, 0, MEM_RELEASE)
        result.update(ok=True, thread_id=thread_id.value, inyectado=True)
        return result
    finally:
        kernel32.CloseHandle(h)


def verify_state(state_file: str, timeout: float = 8.0) -> bool:
    try:
        before = os.path.getmtime(state_file) if os.path.exists(state_file) else 0
    except OSError:
        before = 0
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if os.path.exists(state_file) and os.path.getmtime(state_file) > before:
                return True
        except OSError:
            pass
        time.sleep(0.25)
    return False


def inject_agent(pid: int, dll: str = None, agent: str = None, mydata: str = None,
                 log: str = None, suspend: bool = True, verify: bool = True) -> dict:
    """Escribe la config de la DLL e inyecta. Devuelve el resultado."""
    dll = dll or DEFAULT_DLL
    agent = agent or DEFAULT_AGENT
    if not IS_WINDOWS:
        return {"ok": False, "error": "solo Windows"}
    _setup()
    if not os.path.isfile(dll):
        return {"ok": False, "error": f"no existe la DLL {dll}"}
    mydata = mydata or detect_mydata(pid)
    if not mydata:
        return {"ok": False, "error": "no se detecto mydata"}
    log = log or os.path.join(mydata, "pxg_bot.log")
    cfg_path = write_config(dll, agent, mydata, log, suspend)
    res = inject(pid, dll)
    res["config"] = cfg_path
    res["mydata"] = mydata
    if res.get("ok") and verify:
        res["state_actualizado"] = verify_state(os.path.join(mydata, "pxg_bot_state.json"))
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pxg-inject", description="Inyecta el agente en pxgme.exe")
    ap.add_argument("--process", default=DEFAULT_PROCESS)
    ap.add_argument("--pid", type=int, default=None)
    ap.add_argument("--dll", default=DEFAULT_DLL)
    ap.add_argument("--agent", default=DEFAULT_AGENT)
    ap.add_argument("--dir", default=None, help="mydata del cliente")
    ap.add_argument("--log", default=None)
    ap.add_argument("--suspend", dest="suspend", action="store_true", default=True,
                    help="suspende los otros hilos al cargar (por defecto)")
    ap.add_argument("--no-suspend", dest="suspend", action="store_false")
    ap.add_argument("--no-verify", action="store_true")
    args = ap.parse_args(argv)

    if not IS_WINDOWS:
        print(json.dumps({"ok": False, "error": "solo Windows"}))
        return 2
    pid = args.pid or find_pid(args.process)
    if pid is None:
        print(json.dumps({"ok": False, "error": f"proceso '{args.process}' no encontrado"}))
        return 1
    res = inject_agent(pid, args.dll, args.agent, args.dir, args.log,
                       suspend=args.suspend, verify=not args.no_verify)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0 if res.get("ok") else 1
