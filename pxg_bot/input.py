from __future__ import annotations

import ctypes
import os
import random
import shutil
import subprocess
import time
from ctypes import wintypes

from .memory import IS_LINUX, IS_WINDOWS
from .models import Vec3

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
WM_MOUSEMOVE = 0x0200
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

X_KEYSYM_NAMES = {
    "ENTER": "Return", "ESC": "Escape", "SPACE": "space", "TAB": "Tab",
    "BACK": "BackSpace", "LEFT": "Left", "UP": "Up", "RIGHT": "Right", "DOWN": "Down",
    "HOME": "Home", "END": "End", "PGUP": "Prior", "PGDN": "Next", "INS": "Insert",
    "DEL": "Delete",
}
for _i in range(1, 25):
    X_KEYSYM_NAMES[f"F{_i}"] = f"F{_i}"
for _i in range(10):
    X_KEYSYM_NAMES[f"NUM{_i}"] = f"KP_{_i}"


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

    def mouse_to_tile(self, target: Vec3, context: dict) -> None:
        pass

    def click_at(self, target: Vec3, context: dict) -> None:
        pass

    def loot(self) -> None:
        pass

    def press(self, key: str, duration: float = 0.03) -> None:
        raise NotImplementedError

    def click_tile(self, target: Vec3, context: dict) -> None:
        raise NotImplementedError

    def say(self, text: str) -> None:
        raise NotImplementedError

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
        self.kd_jitter = float(settings.get("humanizer", {}).get("key_duration_jitter_pct", 0)) / 100.0

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
        duration = float(self.settings.get("move_key_duration", 0.02))
        if self.kd_jitter > 0:
            duration *= random.uniform(1.0 - self.kd_jitter, 1.0 + self.kd_jitter)
        self.press(key, duration)

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


def _read_proc_env(pid: int) -> dict:
    out = {}
    try:
        with open(f"/proc/{pid}/environ", "rb") as handle:
            for item in handle.read().split(b"\x00"):
                if b"=" in item:
                    k, v = item.split(b"=", 1)
                    out[k.decode("latin-1")] = v.decode("latin-1")
    except OSError:
        pass
    return out


class LinuxInput(BaseInput):
    """Input sintético en X11 (XWayland incluido) vía XTest o xdotool."""

    def __init__(self, pid: int, settings: dict):
        if not IS_LINUX:
            raise InputError("LinuxInput requiere Linux")
        self.pid = pid
        self.settings = settings
        env = _read_proc_env(pid)
        for key in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY"):
            if env.get(key):
                os.environ[key] = env[key]
        requested = settings.get("input_backend", "auto")
        self.backend = self._pick_backend(requested)
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
        self.window_id = self._find_window()
        self.kd_jitter = float(settings.get("humanizer", {}).get("key_duration_jitter_pct", 0)) / 100.0
        self.focus_game = bool(settings.get("focus_game", False))
        self.key_method = settings.get("key_method", "window")
        self._display = None
        if self.backend == "xlib":
            from Xlib import display as xdisplay

            self._display = xdisplay.Display()

    def _pick_backend(self, requested: str) -> str:
        candidates = [requested] if requested in ("xlib", "xdotool") else ["xlib", "xdotool"]
        for candidate in candidates:
            if candidate == "xlib":
                try:
                    import Xlib  # noqa: F401
                    import Xlib.ext.xtest  # noqa: F401

                    return "xlib"
                except ImportError:
                    continue
            if candidate == "xdotool":
                if shutil.which("xdotool"):
                    return "xdotool"
                continue
        raise InputError(
            "No hay backend de input X11 disponible. Instala python-xlib "
            "(pip install python-xlib) o xdotool (apt install xdotool)."
        )

    def _find_window(self) -> int:
        if self.backend != "xdotool":
            return 0
        title = self.settings.get("window_name") or "PokeXGames"
        for args in (["search", "--pid", str(self.pid)],
                     ["search", "--name", title],
                     ["search", "--class", title]):
            out = subprocess.run(["xdotool", *args], capture_output=True, text=True)
            ids = [int(x) for x in out.stdout.split() if x.isdigit()]
            if ids:
                return ids[-1]
        return 0
        if self.backend == "xlib" and self._display is not None:
            root = self._display.screen().root
            for window in root.query_tree().children:
                try:
                    geom = window.get_geometry()
                    if geom.width > 200 and geom.height > 200:
                        pid_prop = window.get_full_property(
                            self._display.intern_atom("_NET_WM_PID"), 0
                        )
                        if pid_prop and pid_prop.value[0] == self.pid:
                            return window.id
                except Exception:
                    continue
        return 0

    def focus(self) -> None:
        if self.backend == "xdotool" and self.window_id:
            subprocess.run(["xdotool", "windowactivate", "--sync", str(self.window_id)],
                           capture_output=True)
            return
        if self.backend == "xlib" and self.window_id:
            window = self._display.create_resource_object("window", self.window_id)
            window.raise_window()
            window.set_input_focus(1, self._display.get_input_focus().time)
            self._display.sync()

    def _keysym_name(self, key: str) -> str:
        if key == "|":
            return "bar"
        name = str(key).upper()
        if name in X_KEYSYM_NAMES:
            return X_KEYSYM_NAMES[name]
        return str(key).lower()

    def _key(self, key: str, duration: float = 0.03) -> None:
        name = self._keysym_name(key)
        if self.backend == "xdotool":
            if not self.window_id:
                self.window_id = self._find_window()
                if not self.window_id:
                    return
            if self.key_method == "window":
                subprocess.run(
                    ["xdotool", "key", "--window", str(self.window_id), "--clearmodifiers", name],
                    capture_output=True,
                )
                time.sleep(duration)
                return
            if self._active_window() != self.window_id:
                return
            subprocess.run(["xdotool", "key", "--clearmodifiers", name], capture_output=True)
            time.sleep(duration)
            return
        from Xlib import X, XK
        from Xlib.ext import xtest

        keysym = XK.string_to_keysym(name)
        keycode = self._display.keysym_to_keycode(keysym)
        if keycode == 0:
            raise InputError(f"tecla sin keycode en X11: {key}")
        xtest.fake_input(self._display, X.KeyPress, keycode)
        self._display.sync()
        time.sleep(duration)
        xtest.fake_input(self._display, X.KeyRelease, keycode)
        self._display.sync()

    def _active_window(self) -> int:
        out = subprocess.run(["xdotool", "getactivewindow"], capture_output=True, text=True)
        try:
            return int(out.stdout.strip())
        except ValueError:
            return 0

    def window_origin(self) -> tuple[int, int]:
        return self._window_pos()

    def window_rect(self) -> tuple[int, int, int, int]:
        if self.backend == "xdotool" and self.window_id:
            out = subprocess.run(
                ["xdotool", "getwindowgeometry", "--shell", str(self.window_id)],
                capture_output=True, text=True,
            )
            values = {}
            for line in out.stdout.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    values[k.strip()] = v.strip()
            try:
                return (int(values.get("X", 0)), int(values.get("Y", 0)),
                        int(values.get("WIDTH", 0)), int(values.get("HEIGHT", 0)))
            except ValueError:
                pass
        wx, wy = self._window_pos()
        return wx, wy, 0, 0

    def mouse_move(self, x: int, y: int) -> None:
        rx, ry, rw, rh = self.window_rect()
        if rw and rh:
            x = min(max(int(x), rx), rx + rw - 1)
            y = min(max(int(y), ry), ry + rh - 1)
        subprocess.run(["xdotool", "mousemove", str(int(x)), str(int(y))], capture_output=True)

    def mouse_click(self, x: int, y: int, button: int = 1) -> None:
        self.mouse_move(x, y)
        subprocess.run(["xdotool", "click", str(int(button))], capture_output=True)

    def press(self, key: str, duration: float = 0.03) -> None:
        self._key(key, duration)

    def move(self, direction: int) -> None:
        key = self.move_keys.get(direction) or self.move_keys.get(str(direction))
        if key is None:
            return
        duration = float(self.settings.get("move_key_duration", 0.02))
        if self.kd_jitter > 0:
            duration *= random.uniform(1.0 - self.kd_jitter, 1.0 + self.kd_jitter)
        self.press(key, duration)

    def _window_pos(self) -> tuple[int, int]:
        if self.backend == "xdotool" and self.window_id:
            out = subprocess.run(
                ["xdotool", "getwindowgeometry", "--shell", str(self.window_id)],
                capture_output=True, text=True,
            )
            values = {}
            for line in out.stdout.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    values[k.strip()] = v.strip()
            return int(values.get("X", 0)), int(values.get("Y", 0))
        if self.backend == "xlib" and self.window_id:
            window = self._display.create_resource_object("window", self.window_id)
            geom = window.get_geometry()
            coords = window.translate_coords(self._display.screen().root, 0, 0)
            return -coords.x, -coords.y
        return 0, 0

    def _tile_to_screen(self, target: Vec3, context: dict) -> tuple[int, int]:
        player = context.get("player")
        wx, wy = self._window_pos()
        if player is None:
            return wx + self.origin[0], wy + self.origin[1]
        sx = wx + self.origin[0] + (target.x - player.pos.x) * self.tile_size + self.tile_size // 2
        sy = wy + self.origin[1] + (target.y - player.pos.y) * self.tile_size + self.tile_size // 2
        return int(sx), int(sy)

    def click_tile(self, target: Vec3, context: dict) -> None:
        sx, sy = self._tile_to_screen(target, context)
        if self.backend == "xdotool":
            subprocess.run(["xdotool", "mousemove", "--sync", str(sx), str(sy)],
                           capture_output=True)
            subprocess.run(["xdotool", "click", "1"], capture_output=True)
            return
        from Xlib import X
        from Xlib.ext import xtest

        xtest.fake_input(self._display, X.MotionNotify, x=sx, y=sy)
        self._display.sync()
        time.sleep(0.01)
        xtest.fake_input(self._display, X.ButtonPress, 1)
        self._display.sync()
        time.sleep(0.01)
        xtest.fake_input(self._display, X.ButtonRelease, 1)
        self._display.sync()

    def say(self, text: str) -> None:
        self.press(self.say_key, 0.03)
        for ch in text:
            self.press(ch, 0.01)
        self.press("ENTER", 0.03)

    def stop(self) -> None:
        if self._display is not None:
            self._display.close()
            self._display = None


def create_input(pid: int, settings: dict) -> BaseInput:
    backend = settings.get("input_backend", "auto")
    if backend == "mock":
        return MockInput()
    if IS_WINDOWS:
        return WinInput(pid, settings)
    if IS_LINUX:
        return LinuxInput(pid, settings)
    raise InputError(f"plataforma sin backend de input: {os.name}")