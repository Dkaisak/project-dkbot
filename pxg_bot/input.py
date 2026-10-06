from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

from .memory import IS_WINDOWS
from .models import Vec3

# NOTA: en Linux/pxg no se usa inyeccion de teclado/mouse por X11 (nada de
# xdotool/XTest). El input lo realiza el agente Lua dentro del cliente
# (g_game.walk, g_gameActions.processMouseAction). Este modulo solo conserva
# el backend nativo de Windows y utilidades para test.

if IS_WINDOWS:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
else:
    user32 = None

VK_NAMES = {
    "BACK": 0x08, "TAB": 0x09, "ENTER": 0x0D, "ESC": 0x1B, "SPACE": 0x20,
    "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28,
    "HOME": 0x24, "END": 0x23, "PGUP": 0x21, "PGDN": 0x22, "INS": 0x2D,
}
for _i in range(1, 25):
    VK_NAMES[f"F{_i}"] = 0x70 + (_i - 1)
for _i in range(10):
    VK_NAMES[f"NUM{_i}"] = 0x60 + _i
for _ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    VK_NAMES[_ch] = ord(_ch)
for _d in "0123456789":
    VK_NAMES[_d] = ord(_d)

EXTENDED_KEYS = {0x25, 0x26, 0x27, 0x28, 0x2D, 0x24, 0x23, 0x21, 0x22}

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
INPUT_KEYBOARD = 1
INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class InputError(RuntimeError):
    pass


def _make_input_structs():
    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG), ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class _InputUnion(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _InputUnion)]

    return KEYBDINPUT, MOUSEINPUT, INPUT


if IS_WINDOWS:
    KEYBDINPUT, MOUSEINPUT, INPUT = _make_input_structs()
    user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
    user32.MapVirtualKeyW.restype = wintypes.UINT
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def find_window_for_pid(pid: int) -> int:
    if not IS_WINDOWS:
        return 0
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _lparam):
        wpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid and user32.IsWindowVisible(hwnd):
            rect = wintypes.RECT()
            user32.GetClientRect(hwnd, ctypes.byref(rect))
            if rect.right - rect.left > 100 and rect.bottom - rect.top > 100:
                found.append(hwnd)
                return False
        return True

    user32.EnumWindows(_enum, 0)
    return found[0] if found else 0


class BaseInput:
    def move(self, direction: int) -> None:
        raise NotImplementedError

    def walk_to(self, target: Vec3) -> None:
        pass

    def walk_path(self, waypoints: list, z: int) -> None:
        pass

    def turn(self, direction: int) -> None:
        pass

    def hotkey(self, key: str) -> None:
        pass

    def middle_click(self, target: Vec3) -> None:
        pass

    def click_slot(self, slot: int) -> None:
        pass

    def call_slot(self, slot: int) -> None:
        pass

    def pokestop(self, method: str = "func", arg: str = "") -> None:
        pass

    def revive(self, slot: int, item_id: int = 2269) -> None:
        pass

    def ball(self, item_id: int, target: Vec3) -> None:
        pass

    def order(self, target: Vec3) -> None:
        pass

    def set_fight_mode(self, mode: int) -> None:
        pass

    def stand_near(self, target: Vec3) -> None:
        pass

    def stand_near_many(self, bodies: list, z: int) -> None:
        pass

    def mouse_to_tile(self, target: Vec3, context: dict) -> None:
        pass

    def click_at(self, target: Vec3, context: dict) -> None:
        pass

    def loot(self) -> None:
        pass

    def press(self, key: str, duration: float = 0.03) -> None:
        raise NotImplementedError

    def click_tile(self, target: Vec3, context: dict) -> None:
        pass

    def say(self, text: str) -> None:
        pass

    def stop(self) -> None:
        pass


class MockInput(BaseInput):
    def __init__(self, verbose: bool = False):
        self.actions: list[tuple] = []
        self.verbose = verbose

    def _log(self, *action) -> None:
        self.actions.append(action)
        if self.verbose:
            print("  input:", *action)

    def move(self, direction: int) -> None:
        self._log("move", direction)

    def press(self, key: str, duration: float = 0.03) -> None:
        self._log("press", key)

    def click_tile(self, target: Vec3, context: dict) -> None:
        self._log("click_tile", (target.x, target.y, target.z))

    def say(self, text: str) -> None:
        self._log("say", text)


class WinInput(BaseInput):
    """Input nativo de Windows (SendInput / PostMessage)."""

    def __init__(self, pid: int, settings: dict):
        if not IS_WINDOWS:
            raise InputError("WinInput requiere Windows")
        self.pid = pid
        self.settings = settings
        self.hwnd = find_window_for_pid(pid)
        self.background = bool(settings.get("background_input", False))
        keymap = settings.get("keymap", {})
        self.move_keys = keymap.get("move", {
            0: "NUM8", 1: "NUM9", 2: "NUM6", 3: "NUM3",
            4: "NUM2", 5: "NUM1", 6: "NUM4", 7: "NUM7",
        })
        self.tile_size = int(settings.get("tile_size", 32))
        self.origin = (
            int(settings.get("player_screen_x", 480)),
            int(settings.get("player_screen_y", 360)),
        )
        self.say_key = keymap.get("say", "ENTER")

    def _vk(self, key: str) -> int:
        name = str(key).upper()
        if name in VK_NAMES:
            return VK_NAMES[name]
        if len(name) == 1:
            return ord(name)
        raise InputError(f"tecla desconocida: {key}")

    def _send_scan(self, vk: int, keyup: bool) -> None:
        scan = user32.MapVirtualKeyW(vk, 0)
        flags = KEYEVENTF_SCANCODE
        if vk in EXTENDED_KEYS:
            flags |= KEYEVENTF_EXTENDEDKEY
        if keyup:
            flags |= KEYEVENTF_KEYUP
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.u.ki = KEYBDINPUT(0, scan, flags, 0, 0)
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    def _post_key(self, vk: int, keyup: bool) -> None:
        scan = user32.MapVirtualKeyW(vk, 0)
        lparam = 1 | (scan << 16)
        if vk in EXTENDED_KEYS:
            lparam |= 1 << 24
        msg = WM_KEYUP if keyup else WM_KEYDOWN
        if keyup:
            lparam |= 0xC0000000
        user32.PostMessageW(self.hwnd, msg, vk, lparam)

    def _key(self, vk: int, duration: float = 0.03) -> None:
        if self.background and self.hwnd:
            self._post_key(vk, False)
            time.sleep(duration)
            self._post_key(vk, True)
        else:
            self._send_scan(vk, False)
            time.sleep(duration)
            self._send_scan(vk, True)

    def press(self, key: str, duration: float = 0.03) -> None:
        self._key(self._vk(key), duration)

    def move(self, direction: int) -> None:
        key = self.move_keys.get(direction) or self.move_keys.get(str(direction))
        if key is None:
            return
        self.press(key, float(self.settings.get("move_key_duration", 0.02)))

    def _tile_to_client(self, target: Vec3, context: dict) -> tuple[int, int]:
        player = context.get("player")
        if player is None:
            return self.origin
        sx = self.origin[0] + (target.x - player.pos.x) * self.tile_size + self.tile_size // 2
        sy = self.origin[1] + (target.y - player.pos.y) * self.tile_size + self.tile_size // 2
        return int(sx), int(sy)

    def click_tile(self, target: Vec3, context: dict) -> None:
        sx, sy = self._tile_to_client(target, context)
        if self.background and self.hwnd:
            lparam = (sy << 16) | (sx & 0xFFFF)
            user32.PostMessageW(self.hwnd, WM_LBUTTONDOWN, 1, lparam)
            user32.PostMessageW(self.hwnd, WM_LBUTTONUP, 0, lparam)
            return
        if not self.hwnd:
            return
        point = wintypes.POINT(sx, sy)
        user32.ClientToScreen(self.hwnd, ctypes.byref(point))
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        nx = int(point.x * 65535 / max(1, sw))
        ny = int(point.y * 65535 / max(1, sh))
        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.u.mi = MOUSEINPUT(nx, ny, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, 0, 0)
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        time.sleep(0.01)
        inp.u.mi = MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTDOWN, 0, 0)
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        time.sleep(0.01)
        inp.u.mi = MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTUP, 0, 0)
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    def say(self, text: str) -> None:
        self.press(self.say_key, 0.03)
        for ch in text:
            self.press(ch, 0.01)
        self.press("ENTER", 0.03)

    def stop(self) -> None:
        pass


def create_input(pid: int, settings: dict) -> BaseInput:
    backend = settings.get("input_backend", "lua")
    if backend == "mock":
        return MockInput()
    if IS_WINDOWS:
        return WinInput(pid, settings)
    raise InputError(
        "En Linux el input lo realiza el agente Lua (input_backend='lua'). "
        "No se usa xdotool/X11."
    )
