"""Movimiento del puntero fisico via XTest (libXtst).

Se usa UNICAMENTE para el revive: el juego aplica el item sobre el "Thing" que
hay bajo el cursor real, y no hay forma de fijar la posicion del cursor desde
Lua. El movimiento del personaje sigue siendo 100% por el agente Lua.

El display NO se toma del entorno del bot (que puede haberse lanzado con un
DISPLAY viejo, p. ej. antes de una sesion xrdp), sino del proceso del cliente:
se lee /proc/<pid>/environ y se prueban sus DISPLAY/XAUTHORITY. Asi el revive
funciona aunque la GUI/bot se hayan arrancado desde otra sesion.
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading

_LOCK = threading.Lock()
_CACHE = None
_WARNED = False
_IO_SET = False


def _on_io_error(_display):
    # Xlib llama a exit() por defecto ante un error de E/S (p. ej. xrdp se cae).
    # Lo interceptamos para invalidar el display y NO matar el bot.
    global _CACHE
    _CACHE = None
    return 0


_IO_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)(_on_io_error)

# nombres de proceso del cliente a buscar en /proc (override con PXG_CLIENT_NAMES)
_DEFAULT_NAMES = ("pxgme-linux", "PokeXGames.exe", "pxgme")


def _client_names() -> tuple[str, ...]:
    raw = os.environ.get("PXG_CLIENT_NAMES", "")
    if raw:
        return tuple(n.strip() for n in raw.split(",") if n.strip())
    return _DEFAULT_NAMES


def _read_proc_env(pid: int) -> dict:
    try:
        with open(f"/proc/{pid}/environ", "rb") as handle:
            raw = handle.read()
    except OSError:
        return {}
    env = {}
    for item in raw.split(b"\0"):
        if b"=" in item:
            key, value = item.split(b"=", 1)
            env[key.decode("latin-1")] = value.decode("latin-1")
    return env


def _client_pids(names: tuple[str, ...]) -> list[int]:
    wanted = set(names)
    pids = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return pids
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/comm", "r", encoding="latin-1") as handle:
                comm = handle.read().strip()
        except OSError:
            continue
        if comm in wanted:
            pids.append(int(entry))
    return pids


def _candidate_displays() -> list[tuple[str, str | None]]:
    """Pares (DISPLAY, XAUTHORITY) a probar, en orden de preferencia."""
    home_auth = os.path.join(os.path.expanduser("~"), ".Xauthority")
    cands: list[tuple[str, str | None]] = []

    forced = os.environ.get("PXG_X11_DISPLAY")
    if forced:
        cands.append((forced, os.environ.get("PXG_X11_XAUTHORITY") or home_auth))

    for pid in _client_pids(_client_names()):
        env = _read_proc_env(pid)
        disp = env.get("DISPLAY")
        if not disp:
            continue
        auth = env.get("XAUTHORITY")
        if auth:
            cands.append((disp, auth))
        # el ~/.Xauthority del CLIENTE (no el del bot: pueden ser usuarios distintos)
        chome = env.get("HOME")
        if chome:
            cands.append((disp, os.path.join(chome, ".Xauthority")))
        cands.append((disp, home_auth))
        cands.append((disp, None))        # deja que libX11 decida

    disp = os.environ.get("DISPLAY")
    if disp:
        cands.append((disp, os.environ.get("XAUTHORITY")))
        cands.append((disp, None))

    # dedupe preservando orden
    seen = set()
    out = []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _try_open(x11, display_name: str, xauthority: str | None):
    """Abre un display aislando DISPLAY/XAUTHORITY (libX11 los lee de getenv)."""
    old_disp = os.environ.get("DISPLAY")
    old_auth = os.environ.get("XAUTHORITY")
    try:
        os.environ["DISPLAY"] = display_name
        if xauthority:
            os.environ["XAUTHORITY"] = xauthority
        else:
            os.environ.pop("XAUTHORITY", None)
        return x11.XOpenDisplay(display_name.encode())
    finally:
        if old_disp is None:
            os.environ.pop("DISPLAY", None)
        else:
            os.environ["DISPLAY"] = old_disp
        if old_auth is None:
            os.environ.pop("XAUTHORITY", None)
        else:
            os.environ["XAUTHORITY"] = old_auth


def _load():
    global _CACHE, _WARNED, _IO_SET
    if _CACHE is not None and _CACHE[2]:
        return _CACHE
    x11 = ctypes.CDLL("libX11.so.6")
    xtst = ctypes.CDLL("libXtst.so.6")
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XFlush.argtypes = [ctypes.c_void_p]
    xtst.XTestFakeMotionEvent.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong,
    ]
    if not _IO_SET:
        try:
            x11.XSetIOErrorHandler.argtypes = [_IO_CB.__class__]
            x11.XSetIOErrorHandler(_IO_CB)
            _IO_SET = True
        except (AttributeError, OSError):
            pass
    display = None
    opened = None
    for name, auth in _candidate_displays():
        display = _try_open(x11, name, auth)
        if display:
            opened = (name, auth)
            break
    # solo se cachea un display valido; si falla, se reintenta en el proximo revive
    _CACHE = (x11, xtst, display)
    if not display and not _WARNED:
        _WARNED = True
        print(
            "[x11mouse] no se pudo abrir ningun display X; el revive no funcionara "
            f"(candidatos: {_candidate_displays()})",
            file=sys.stderr,
        )
    return _CACHE


def move(x: int, y: int) -> bool:
    global _CACHE
    with _LOCK:
        try:
            x11, xtst, display = _load()
        except OSError:
            return False
        if not display:
            return False
        res = xtst.XTestFakeMotionEvent(display, -1, int(x), int(y), 0)
        x11.XFlush(display)
        if not res:
            # el display dejo de servir (reconexion xrdp, etc.): re-resolver
            _CACHE = None
            return False
        return True
