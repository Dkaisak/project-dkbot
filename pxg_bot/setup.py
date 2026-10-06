"""Utilidades para 'atachar' el cliente: detectar su mydata, ajustar la ruta
del agente + config, e inyectar el agente (via gdb).

Reutilizado por `tools/setup_client.py` y por el boton de la GUI web.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT = os.path.join(ROOT, "tools", "pxg_agent.lua")
INSTALL = os.path.join(ROOT, "tools", "install_agent.py")

LUA_KEYS = [
    "state_file", "cmd_file", "control_file", "status_file", "minimap_file",
    "shiny_file", "pokemon_skills_file", "ignore_file", "routes_file",
]


def find_pid(name: str):
    try:
        out = subprocess.run(["pgrep", "-f", name], capture_output=True, text=True,
                             timeout=10).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return None
    return int(out[0]) if out else None


def proc_exe(pid: int):
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return None


def proc_cwd(pid: int):
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def detect_mydata(pid: int):
    cands = []
    exe = proc_exe(pid)
    if exe:
        cands.append(os.path.join(os.path.dirname(exe), "mydata"))
    cwd = proc_cwd(pid)
    if cwd:
        cands.append(os.path.join(cwd, "mydata"))
    for c in cands:
        if os.path.isdir(c):
            return c
    return None


def rebase(path: str, old_base, new_base: str) -> str:
    if not path:
        return path
    if old_base and path.startswith(old_base):
        return new_base + path[len(old_base):]
    return os.path.join(new_base, os.path.basename(path))


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
    with open(agent_path, encoding="utf-8") as fh:
        text = fh.read()
    return re.sub(r'^local DIR = ".*"', f'local DIR = "{new_base}"',
                  text, count=1, flags=re.M)


def install_agent(pid: int, agent_path: str = AGENT):
    """Inyecta el agente via gdb. Devuelve (ok, salida)."""
    try:
        res = subprocess.run(
            [sys.executable, INSTALL, "--pid", str(pid), "--agent", agent_path],
            capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    ok = "loadfile=0" in res.stdout and "pcall=0" in res.stdout
    return ok, res.stdout.strip()


def attach(cfg_path: str, process_name: str = "pxgme-linux", pid=None,
           agent_path: str = AGENT, install: bool = True, write: bool = True) -> dict:
    """Detecta el cliente, ajusta rutas y (opcionalmente) inyecta el agente."""
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
        ok, out = install_agent(pid, agent_path)
        result["installed"] = ok
        result["install_output"] = out
    return result
