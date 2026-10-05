"""Movimiento del puntero fisico via XTest (libXtst).

Se usa UNICAMENTE para el revive: el juego aplica el item sobre el "Thing" que
hay bajo el cursor real, y no hay forma de fijar la posicion del cursor desde
Lua. El movimiento del personaje sigue siendo 100% por el agente Lua.
"""
from __future__ import annotations

import ctypes
import os
import threading

_LOCK = threading.Lock()
_CACHE = None


def _load():
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    x11 = ctypes.CDLL("libX11.so.6")
    xtst = ctypes.CDLL("libXtst.so.6")
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XFlush.argtypes = [ctypes.c_void_p]
    xtst.XTestFakeMotionEvent.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong,
    ]
    display = x11.XOpenDisplay(os.environ.get("DISPLAY", ":1").encode())
    _CACHE = (x11, xtst, display)
    return _CACHE


def move(x: int, y: int) -> bool:
    with _LOCK:
        try:
            x11, xtst, display = _load()
        except OSError:
            return False
        if not display:
            return False
        xtst.XTestFakeMotionEvent(display, -1, int(x), int(y), 0)
        x11.XFlush(display)
        return True
