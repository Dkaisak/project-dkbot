"""Rutas de la aplicación, con soporte para ejecutable congelado (PyInstaller).

- Sin congelar: todo vive en el repo.
- Congelado (onefile/onedir): el codigo/datos empaquetados estan en
  `sys._MEIPASS` (BUNDLE_DIR), pero los ficheros que el usuario edita o que hay
  que escribir (config.json, agente .lua, DLL) viven JUNTO AL EXE (APP_DIR).

`ensure_app_files()` copia esos ficheros escribibles del bundle junto al exe la
primera vez, para que `attach`/inyeccion/edicion de config funcionen.
"""
from __future__ import annotations

import os
import shutil
import sys

IS_FROZEN = bool(getattr(sys, "frozen", False))

# Directorio de recursos empaquetados (solo lectura en onefile).
BUNDLE_DIR = getattr(sys, "_MEIPASS", None) or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))

# Directorio de la app (junto al exe si esta congelado; el repo si no).
APP_DIR = os.path.dirname(os.path.abspath(sys.executable)) if IS_FROZEN else BUNDLE_DIR

# Ficheros que deben existir junto al exe (escribibles).
_WRITABLE = [
    "config.json",
    os.path.join("tools", "pxg_agent.lua"),
    os.path.join("tools", "agent_loader", "pxg_agent_loader.dll"),
]


def ensure_app_files() -> None:
    """En modo congelado, copia config/agente/DLL junto al exe si faltan."""
    if not IS_FROZEN:
        return
    for rel in _WRITABLE:
        dst = os.path.join(APP_DIR, rel)
        if os.path.exists(dst):
            continue
        src = os.path.join(BUNDLE_DIR, rel)
        if os.path.exists(src):
            os.makedirs(os.path.dirname(dst) or APP_DIR, exist_ok=True)
            try:
                shutil.copy2(src, dst)
            except OSError:
                pass


def default_config_path() -> str:
    """Ruta por defecto de config.json (junto al exe si congelado)."""
    if IS_FROZEN:
        return os.path.join(APP_DIR, "config.json")
    return "config.json"
