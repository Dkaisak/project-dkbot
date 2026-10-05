"""Canal de control en caliente entre la GUI y el bot.

La GUI escribe un JSON con pausa, toggles de comportamientos y parametros de
exploracion; el bot lo lee periodicamente y los aplica en memoria, sin reiniciar.
La escritura es atomica (temp + os.replace) para que el bot nunca lea un archivo
a medio escribir.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile

DEFAULT_CONTROL = {
    "paused": False,
    "behaviors": {},
    "explore": {},
    "updated": 0.0,
}


def control_path(cfg: dict) -> str:
    lua = cfg.get("lua", {}) or {}
    return lua.get("control_file") or cfg.get("control_file") or ""


def status_path(cfg: dict) -> str:
    lua = cfg.get("lua", {}) or {}
    return lua.get("status_file") or cfg.get("status_file") or ""


def read_control(path: str) -> dict:
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_control(path: str, data: dict) -> None:
    if not path:
        return
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".ctl-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        os.replace(tmp, path)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def merge_control(current: dict, patch: dict) -> dict:
    merged = deep_merge(DEFAULT_CONTROL, current or {})
    merged = deep_merge(merged, patch or {})
    return merged
