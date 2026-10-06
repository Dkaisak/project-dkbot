"""Movimiento del cursor fisico, multi-plataforma.

Se usa para acciones que dependen del "Thing" bajo el puntero (revive, order):
desde Lua no se puede fijar la posicion del cursor. En Linux va por XTest
(libXtst); en Windows por la API Win32 (SetCursorPos).
"""
from __future__ import annotations

import sys


if sys.platform == "win32":
    def move(x: int, y: int) -> bool:
        try:
            import ctypes
            return bool(ctypes.windll.user32.SetCursorPos(int(x), int(y)))
        except Exception:
            return False
else:
    from . import x11mouse

    def move(x: int, y: int) -> bool:
        return x11mouse.move(x, y)
