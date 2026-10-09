"""GUI web local para el bot, usando solo la libreria estandar.

Levanta un servidor HTTP en 127.0.0.1 que sirve un dashboard (estado, controles,
comandos, mapa) y permite arrancar/parar el bot como subproceso.

Uso:
  python3 main.py gui
  python3 -m pxg_bot.webui --port 8765 --open
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import control as control_io
from . import paths
from .config import load_config, save_config

ROOT = paths.APP_DIR
WEB_DIR = os.path.join(paths.BUNDLE_DIR, "pxg_bot", "web")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
}


def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _tail(path: str, n: int) -> list[str]:
    """Ultimas `n` lineas leyendo SOLO el final del fichero.

    Antes se hacia `readlines()` (cargaba todo en memoria): con un log de cientos
    de MB la pestana Registro se arrastraba. Ahora se lee hacia atras por bloques
    hasta juntar `n` lineas."""
    if not path:
        return []
    try:
        n = max(1, int(n))
    except (TypeError, ValueError):
        n = 1
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            chunk = 64 * 1024
            data = b""
            pos = size
            while pos > 0:
                step = min(chunk, pos)
                pos -= step
                handle.seek(pos)
                data = handle.read(step) + data
                if data.count(b"\n") > n:
                    break
    except OSError:
        return []
    text = data.decode("utf-8", "ignore")
    return text.splitlines()[-n:]


def _size(path: str):
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def _tail_jsonl(path: str, n: int) -> list[dict]:
    """Ultimas `n` lineas JSON de un fichero .jsonl (lee solo el final)."""
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 128 * 1024))
            data = handle.read().decode("utf-8", "ignore")
    except OSError:
        return []
    out = []
    for line in [ln for ln in data.splitlines() if ln.strip()][-max(1, n):]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _count_enemies(state: dict) -> int:
    seen = set()
    monsters = 0
    for tile in state.get("nearby", []) or []:
        for c in tile.get("creatures", []) or []:
            if c.get("monster"):
                key = (c.get("name"), c.get("x"), c.get("y"))
                if key not in seen:
                    seen.add(key)
                    monsters += 1
    for b in state.get("battle", []) or []:
        if not b.get("own"):
            key = (b.get("name"), b.get("x"), b.get("y"))
            if key not in seen:
                seen.add(key)
                monsters += 1
    return monsters


class Dashboard:
    def __init__(self, cfg_path: str):
        self.cfg_path = cfg_path
        self._minimap = None
        self.proc: subprocess.Popen | None = None
        self._log_handle = None
        self.lock = threading.Lock()
        self._apply_cfg(load_config(cfg_path))
        self._ui_stop = threading.Event()
        self._tg_listener = None
        self._start_ui_watch()

    def _apply_cfg(self, cfg: dict) -> None:
        self.cfg = cfg
        self._clan_cache = None
        self.process_name = str(cfg.get("process_name", "") or "")
        ui = cfg.get("ui", {})
        lua = cfg.get("lua", {})
        self.host = ui.get("host", "127.0.0.1")
        self.port = int(ui.get("port", 8765))
        self.log_file = ui.get("log_file", "")
        self.state_file = lua.get("state_file", "")
        self.cmd_file = lua.get("cmd_file", "")
        self.control_file = lua.get("control_file", "")
        self.status_file = lua.get("status_file", "")
        self.walkmap_file = os.path.join(os.path.dirname(self.state_file), "pxg_walkmap.txt")
        self.world_file = os.path.join(os.path.dirname(self.state_file), "pxg_world.json")
        self.minimap_file = lua.get("minimap_file", "")
        self.routes_file = lua.get("routes_file", "")
        self.pokemon_skills_file = lua.get("pokemon_skills_file", "")
        self.ignore_file = lua.get("ignore_file", "")
        self.pid_file = os.path.join(os.path.dirname(self.status_file), "pxg_bot.pid") if self.status_file else ""

    def attach_client(self, process_name: str | None = None) -> dict:
        """Detecta el cliente, ajusta rutas e inyecta el agente (boton GUI).

        `process_name` (opcional): ejecutable del cliente elegido en la GUI
        (`pxgme.exe` en Windows, `pxgme-linux` en Linux). Cadena vacia = modo
        automatico (segun la plataforma). Si se pasa (aunque sea vacio) se guarda
        en config.json antes de atachar, para alternar Windows/Linux sin editar
        el fichero a mano.
        """
        from . import setup

        with self.lock:
            if process_name is not None:
                self._save_process_name(str(process_name).strip())
                self._apply_cfg(load_config(self.cfg_path))
            chosen = self.process_name or setup.DEFAULT_PROCESS
            res = setup.attach(self.cfg_path, process_name=chosen, install=True)
            if res.get("ok"):
                self._apply_cfg(load_config(self.cfg_path))
                self._minimap = None
            return res

    def _save_process_name(self, name: str) -> bool:
        """Escribe `process_name` (y `module_name`) en config.json preservando la
        estructura del fichero (no reescribe offsets/_doc como `patch_config`)."""
        try:
            with open(self.cfg_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        # admitir una ruta pegada: el cliente se localiza por nombre de exe
        name = str(name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
        data["process_name"] = name
        if name:
            data["module_name"] = name
        try:
            tmp = self.cfg_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)
            os.replace(tmp, self.cfg_path)
            return True
        except OSError:
            return False

    def set_process_name(self, name: str) -> dict:
        """Guarda el ejecutable del cliente elegido (sin atachar)."""
        with self.lock:
            ok = self._save_process_name(str(name or "").strip())
            if ok:
                self._apply_cfg(load_config(self.cfg_path))
            return {"ok": ok, "process_name": self.process_name}

    def processes(self) -> dict:
        """Lista procesos que parecen clientes de PokeXGames (para el selector).

        Enumera todos los procesos y filtra por nombre en local: en Windows
        `list_processes(query)` solo hace match exacto, asi que no vale pasarle
        un fragmento.
        """
        from .memory import list_processes

        needles = ("pxg", "pokexgames")
        found = []
        seen = set()
        for pid, exe in list_processes(None):
            low = exe.lower()
            if not any(n in low for n in needles):
                continue
            if exe in seen:
                continue
            seen.add(exe)
            found.append({"pid": pid, "name": exe})
        found.sort(key=lambda p: p["name"].lower())
        return {"processes": found, "current": self.process_name}

    # --- telegram ---
    def telegram(self) -> dict:
        """Config de avisos + estado en vivo (del status_file) + estado de comandos."""
        cfg = load_config(self.cfg_path)
        tg = cfg.get("telegram", {}) or {}
        status = _read_json(self.status_file).get("telegram") or {}
        cmds = {"running": bool(self._tg_listener is not None and self._tg_listener.is_alive())}
        if self._tg_listener is not None:
            cmds["offset"] = self._tg_listener._offset
            cmds["log"] = list(self._tg_listener.log[-5:])
        return {"config": tg, "status": status, "commands": cmds}

    def _save_telegram(self, patch: dict) -> bool:
        """Escribe la seccion `telegram` en config.json conservando el resto."""
        try:
            with open(self.cfg_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        from .config import deep_merge
        from .notify import DEFAULT_TELEGRAM

        current = data.get("telegram")
        if not isinstance(current, dict):
            current = {}
        data["telegram"] = deep_merge(deep_merge(DEFAULT_TELEGRAM, current), patch or {})
        try:
            tmp = self.cfg_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)
            os.replace(tmp, self.cfg_path)
            return True
        except OSError:
            return False

    def patch_telegram(self, body: dict) -> dict:
        """Guarda la config y la aplica en caliente (canal de control)."""
        with self.lock:
            if not self._save_telegram(body or {}):
                return {"ok": False, "error": "no se pudo guardar config.json"}
            self._apply_cfg(load_config(self.cfg_path))
            tg = self.cfg.get("telegram", {}) or {}
            current = control_io.read_control(self.control_file)
            current["telegram"] = tg
            current["updated"] = time.time()
            control_io.write_control(self.control_file, current)
            return {"ok": True, "config": tg}

    def test_telegram(self, body: dict) -> dict:
        """Envia un mensaje de prueba (usa los valores del body o de config)."""
        from .notify import send_message

        body = body or {}
        cfg = load_config(self.cfg_path).get("telegram", {}) or {}
        token = str(body.get("bot_token") or cfg.get("bot_token") or "")
        chat = str(body.get("chat_id") or cfg.get("chat_id") or "")
        parse = str(body.get("parse_mode") or cfg.get("parse_mode", "HTML") or "")
        ok, err = send_message(
            token, chat, "✅ Prueba de ShinyBot: los avisos por Telegram funcionan.", parse)
        return {"ok": ok, "error": err}

    # --- whatsapp (CallMeBot, solo salida) ---
    def whatsapp(self) -> dict:
        cfg = load_config(self.cfg_path)
        wa = cfg.get("whatsapp", {}) or {}
        status = _read_json(self.status_file).get("whatsapp") or {}
        return {"config": wa, "status": status}

    def _save_whatsapp(self, patch: dict) -> bool:
        """Escribe la seccion `whatsapp` en config.json conservando el resto."""
        try:
            with open(self.cfg_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        from .config import deep_merge
        from .whatsapp import DEFAULT_WHATSAPP

        current = data.get("whatsapp")
        if not isinstance(current, dict):
            current = {}
        data["whatsapp"] = deep_merge(deep_merge(DEFAULT_WHATSAPP, current), patch or {})
        try:
            tmp = self.cfg_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)
            os.replace(tmp, self.cfg_path)
            return True
        except OSError:
            return False

    def patch_whatsapp(self, body: dict) -> dict:
        """Guarda la config y la aplica en caliente (canal de control)."""
        with self.lock:
            if not self._save_whatsapp(body or {}):
                return {"ok": False, "error": "no se pudo guardar config.json"}
            self._apply_cfg(load_config(self.cfg_path))
            wa = self.cfg.get("whatsapp", {}) or {}
            current = control_io.read_control(self.control_file)
            current["whatsapp"] = wa
            current["updated"] = time.time()
            control_io.write_control(self.control_file, current)
            return {"ok": True, "config": wa}

    def test_whatsapp(self, body: dict) -> dict:
        """Envia un WhatsApp de prueba (usa los valores del body o de config)."""
        from .whatsapp import send_whatsapp

        body = body or {}
        cfg = dict(load_config(self.cfg_path).get("whatsapp", {}) or {})
        for k in ("provider", "phone", "token", "phone_number_id", "api_version", "apikey"):
            if body.get(k):
                cfg[k] = body[k]
        ok, err = send_whatsapp(
            cfg, "✅ Prueba de ShinyBot: los avisos por WhatsApp funcionan.")
        return {"ok": ok, "error": err}

    # --- licencia ---
    def license(self) -> dict:
        cfg = load_config(self.cfg_path)
        status = _read_json(self.status_file).get("license") or {}
        return {"config": cfg.get("license", {}) or {}, "status": status}

    def patch_license(self, body: dict) -> dict:
        key = str((body or {}).get("key", "") or "").strip().upper()
        try:
            with open(self.cfg_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {"ok": False, "error": "no se pudo leer config.json"}
        if not isinstance(data, dict):
            return {"ok": False, "error": "config invalido"}
        data.setdefault("license", {})["key"] = key
        try:
            tmp = self.cfg_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)
            os.replace(tmp, self.cfg_path)
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        self._apply_cfg(load_config(self.cfg_path))
        return {"ok": True, "key": key}

    # --- comandos entrantes por Telegram (long-poll) ---
    def start_telegram_commands(self) -> None:
        """Arranca el listener de comandos (una sola vez)."""
        if self._tg_listener is not None:
            return
        from .telegram_commands import TelegramCommandListener

        offset_path = ""
        if self.status_file:
            offset_path = os.path.join(os.path.dirname(self.status_file),
                                       "pxg_telegram_offset.json")
        self._tg_listener = TelegramCommandListener(self, offset_path)
        self._tg_listener.start()

    def stop_telegram_commands(self) -> None:
        if self._tg_listener is not None:
            self._tg_listener.stop()
            self._tg_listener = None

    # --- proceso del bot ---
    def _pid_alive(self, pid: int) -> bool:
        if paths.IS_FROZEN or sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes
                k = ctypes.WinDLL("kernel32", use_last_error=True)
                k.OpenProcess.restype = wintypes.HANDLE
                h = k.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
                if not h:
                    return False
                code = wintypes.DWORD()
                ok = k.GetExitCodeProcess(h, ctypes.byref(code))
                k.CloseHandle(h)
                return bool(ok) and code.value == 259  # STILL_ACTIVE
            except Exception:
                return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def bot_pid(self) -> int | None:
        if self.proc is not None and self.proc.poll() is None:
            return self.proc.pid
        if self.pid_file:
            try:
                with open(self.pid_file, "r", encoding="utf-8") as handle:
                    pid = int(handle.read().strip())
                if pid > 0 and self._pid_alive(pid):
                    return pid
            except (OSError, ValueError):
                pass
        return None

    def client_pid(self):
        """PID del cliente (para la captura de pantalla)."""
        try:
            from .memory import find_pid
            return find_pid(self.process_name)
        except Exception:
            return None

    def send_client_key(self, key: str) -> dict:
        """Manda una tecla nativa (SendInput) al cliente (p. ej. fly up/down).

        El vuelo (fly up/down) no se puede disparar por Lua (los eventos
        sinteticos no lo activan), asi que se manda input nativo; requiere el
        cliente en primer plano (se enfoca antes)."""
        if sys.platform != "win32":
            return {"ok": False, "error": "solo Windows"}
        pid = self.client_pid()
        if not pid:
            return {"ok": False, "error": "cliente no encontrado"}
        try:
            import ctypes
            from .input import WinInput, find_window_for_pid
            hwnd = find_window_for_pid(pid)
            if hwnd:
                try:
                    ctypes.WinDLL("user32", use_last_error=True).SetForegroundWindow(hwnd)
                except Exception:
                    pass
            WinInput(pid, {}).press(str(key))
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}

    def _rotate_log(self) -> None:
        """Rota el log si supera `ui.log_max_mb` (mueve el actual a `.1`)."""
        if not self.log_file:
            return
        try:
            max_mb = float((self.cfg.get("ui", {}) or {}).get("log_max_mb", 20) or 0)
        except (TypeError, ValueError):
            max_mb = 20
        if max_mb <= 0:
            return
        try:
            if os.path.getsize(self.log_file) < max_mb * 1024 * 1024:
                return
        except OSError:
            return
        backup = self.log_file + ".1"
        try:
            try:
                os.remove(backup)
            except OSError:
                pass
            os.replace(self.log_file, backup)
        except OSError:
            pass

    def clear_log(self) -> dict:
        """Vacia el registro (mejor con el bot parado)."""
        with self.lock:
            if self._log_handle is not None:
                try:
                    self._log_handle.close()
                except OSError:
                    pass
                self._log_handle = None
            try:
                if self.log_file:
                    os.makedirs(os.path.dirname(self.log_file), exist_ok=True)
                    with open(self.log_file, "w", encoding="utf-8"):
                        pass
            except OSError as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True}

    def start_bot(self) -> dict:
        with self.lock:
            if self.bot_pid():
                return {"ok": False, "error": "el bot ya esta corriendo"}
            if self.log_file:
                # cerrar el handle previo, rotar si toca, y reabrir en append
                if self._log_handle is not None:
                    try:
                        self._log_handle.close()
                    except OSError:
                        pass
                    self._log_handle = None
                os.makedirs(os.path.dirname(self.log_file), exist_ok=True)
                self._rotate_log()
                self._log_handle = open(self.log_file, "ab", buffering=0)
            if paths.IS_FROZEN:
                cmd = [sys.executable, "--config", self.cfg_path, "run", "--verbose"]
            else:
                cmd = [sys.executable, "-u", os.path.join(paths.BUNDLE_DIR, "main.py"),
                       "--config", self.cfg_path, "run", "--verbose"]
            try:
                self.proc = subprocess.Popen(
                    cmd, cwd=ROOT, stdout=self._log_handle, stderr=self._log_handle,
                    stdin=subprocess.DEVNULL, start_new_session=True,
                )
            except OSError as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "pid": self.proc.pid}

    def stop_bot(self) -> dict:
        with self.lock:
            if self.cmd_file:
                try:
                    with open(self.cmd_file, "a", encoding="utf-8") as handle:
                        handle.write("stop\n")
                except OSError:
                    pass
            pid = self.bot_pid()
            if pid is None:
                return {"ok": True, "stopped": False}
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            deadline = time.time() + 3.0
            while time.time() < deadline:
                if not self._pid_alive(pid):
                    break
                time.sleep(0.1)
            if self._pid_alive(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            if self.proc and self.proc.pid == pid:
                self.proc = None
            return {"ok": True, "stopped": True, "pid": pid}

    # --- boton in-game (supervisor) ---
    def _start_ui_watch(self) -> None:
        """Hilo que vigila la peticion del boton in-game (pxg_bot_ui.txt)."""
        if not self.status_file:
            return
        path = os.path.join(os.path.dirname(self.status_file), "pxg_bot_ui.txt")
        threading.Thread(target=self._ui_watch_loop, args=(path,),
                         daemon=True, name="ui-watch").start()

    def _ui_watch_loop(self, path: str) -> None:
        while not self._ui_stop.is_set():
            cmd = ""
            try:
                if os.path.exists(path):
                    try:
                        with open(path, "r", encoding="utf-8") as handle:
                            cmd = handle.read().strip().lower()
                    finally:
                        try:
                            os.remove(path)
                        except OSError:
                            pass
            except OSError:
                cmd = ""
            if cmd in ("start", "stop", "toggle"):
                try:
                    if cmd == "toggle":
                        if self.bot_pid():
                            self.stop_bot()
                        else:
                            self.start_bot()
                    elif cmd == "start":
                        self.start_bot()
                    else:
                        self.stop_bot()
                except Exception:
                    pass
            self._ui_stop.wait(1.0)

    # --- lecturas ---
    def state(self) -> dict:
        state = _read_json(self.state_file)
        status = _read_json(self.status_file)
        control = control_io.read_control(self.control_file)
        pid = self.bot_pid()
        playing = "play" if status.get("chosen") and status.get("chosen") != "idle" else ""
        if status.get("chosen") and status["chosen"] not in ("idle", "break"):
            playing = status["chosen"]
        return {
            "running": pid is not None,
            "pid": pid,
            "process_name": self.process_name,
            "connected": bool(state.get("connected", False)),
            "paused": bool(status.get("paused", False) or control.get("paused", False)),
            "behavior": playing or ("break" if status.get("chosen") == "break" else "idle"),
            "humanizer": status.get("humanizer", ""),
            "counters": status.get("counters", {}),
            "shiny_last": status.get("shiny_last"),
            "capture_pending": status.get("capture_pending", []),
            "uptime": status.get("uptime", 0),
            "loop": status.get("loop", 0),
            "name": state.get("name", ""),
            "x": state.get("x"), "y": state.get("y"), "z": state.get("z"),
            "hp": state.get("hp"), "maxhp": state.get("maxhp"), "hppct": state.get("hppct"),
            "mp": state.get("mp"), "maxmp": state.get("maxmp"),
            "level": state.get("level"),
            "is_walking": bool(state.get("is_walking", False)),
            "nav": state.get("nav_result", ""),
            "last_cmd": state.get("last_cmd", ""),
            "enemies": _count_enemies(state),
            "moves": state.get("moves", []),
            "active_pokemon": state.get("active_pokemon", ""),
            "party": state.get("party", []),
            "bag": state.get("bag", []),
            "defeated": state.get("defeated", []),
            "battle": state.get("battle", []),
            "nearby": state.get("nearby", []),
            "server_msgs": state.get("server_msgs", []),
            "control": control,
            "llm": status.get("llm") or {},
            "telemetry": self._telemetry_status(status),
            "telegram": status.get("telegram") or {},
            "ts": time.time(),
        }

    def _telemetry_status(self, status: dict) -> dict:
        tel = dict(status.get("telemetry") or {})
        if not tel:
            return {"enabled": False}
        base = tel.get("dir") or (os.path.dirname(self.state_file) if self.state_file else "")
        tel["trace_bytes"] = _size(os.path.join(base, "pxg_trace.jsonl")) if base else None
        tel["deaths_bytes"] = _size(os.path.join(base, "pxg_deaths.jsonl")) if base else None
        return tel

    def loot_items(self) -> dict:
        """Tabla aprendida nombre->id de los items (de los mensajes de botin)."""
        base = os.path.dirname(self.state_file) if self.state_file else ""
        path = os.path.join(base, "pxg_loot_items.json") if base else ""
        data = _read_json(path)
        items = data.get("items") if isinstance(data, dict) else None
        return {"items": items or {}}

    def deaths(self, tail: int = 10) -> dict:
        status = _read_json(self.status_file)
        tel = status.get("telemetry") or (self.cfg.get("telemetry") or {})
        base = tel.get("dir") or (os.path.dirname(self.state_file) if self.state_file else "")
        path = os.path.join(base, "pxg_deaths.jsonl") if base else ""
        keys = ("ts", "index", "signals", "level", "exp", "player_hp_pct", "chosen", "pos")
        recs = [{k: r.get(k) for k in keys} for r in _tail_jsonl(path, tail)]
        return {"deaths": recs}

    def players(self) -> dict:
        """Jugadores (no self) vistos por el agente, con distancia al personaje."""
        state = _read_json(self.state_file)
        px, py, pz = state.get("x"), state.get("y"), state.get("z")
        out = []
        seen = set()

        def add(c: dict) -> None:
            if not c.get("player"):
                return
            if (c.get("name") == state.get("name")
                    and (c.get("x"), c.get("y"), c.get("z")) == (px, py, pz)):
                return
            key = (c.get("name"), c.get("x"), c.get("y"))
            if key in seen:
                return
            seen.add(key)
            dist = None
            if px is not None and c.get("x") is not None:
                dist = max(abs(int(c["x"]) - int(px)), abs(int(c.get("y", 0)) - int(py)))
            out.append({"name": c.get("name", "?"), "x": c.get("x"), "y": c.get("y"),
                        "z": c.get("z"), "dist": dist, "skull": c.get("skull")})

        for c in state.get("creatures", []) or []:
            add(c)
        for tile in state.get("nearby", []) or []:
            for c in tile.get("creatures", []) or []:
                add(c)
        out.sort(key=lambda r: (r["dist"] is None, r["dist"] if r["dist"] is not None else 0))
        return {"players": out}

    def log_tail(self, n: int = 15) -> dict:
        return {"lines": _tail(self.log_file, n)}

    def minimap(self):
        if self._minimap is None and self.minimap_file:
            from .otmm import Minimap

            self._minimap = Minimap(self.minimap_file)
        if self._minimap is not None:
            self._minimap.ensure()
        return self._minimap

    def otmm_info(self) -> dict:
        mm = self.minimap()
        if mm is None:
            return {"ready": False, "error": "sin minimap_file"}
        state = _read_json(self.state_file)
        info = mm.info()
        if state.get("x") is not None:
            info["check"] = mm.check(int(state["x"]), int(state["y"]), int(state["z"]))
        return info

    def otmm_region(self, z: int, x0: int, y0: int, x1: int, y1: int, mode: str):
        mm = self.minimap()
        if mm is None or not mm.ready:
            return None
        x1 = min(x1, x0 + 512)
        y1 = min(y1, y0 + 512)
        return mm.region_rgb(x0, y0, x1, y1, z, mode)

    def route(self) -> dict:
        control = control_io.read_control(self.control_file)
        route = control.get("route") if isinstance(control, dict) else None
        if not route:
            data = load_config(self.cfg_path)
            rc = data.get("route", {})
            route = {"waypoints": rc.get("waypoints", []), "loop": rc.get("loop", True),
                     "ping_pong": rc.get("ping_pong", False)}
        route.setdefault("pokemon", "")
        route.setdefault("pokemon_slot", None)
        return route

    def save_route(self, route: dict) -> dict:
        with self.lock:
            current = control_io.read_control(self.control_file)
            current["route"] = {
                "waypoints": route.get("waypoints", []),
                "loop": bool(route.get("loop", True)),
                "ping_pong": bool(route.get("ping_pong", False)),
                "enabled": bool(route.get("enabled", True)),
                "pokemon": str(route.get("pokemon", "") or ""),
                "pokemon_slot": route.get("pokemon_slot"),
            }
            current["updated"] = time.time()
            control_io.write_control(self.control_file, current)
            return {"ok": True, "route": current["route"]}

    # --- rutas guardadas con nombre ---
    def routes(self) -> dict:
        data = _read_json(self.routes_file)
        return data.get("routes", {}) if isinstance(data, dict) else {}

    def _write_pokemon_skills(self, data: dict) -> None:
        if not self.pokemon_skills_file:
            return
        try:
            os.makedirs(os.path.dirname(self.pokemon_skills_file), exist_ok=True)
            tmp = self.pokemon_skills_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)
            os.replace(tmp, self.pokemon_skills_file)
        except OSError:
            pass

    def pokemon_skills(self) -> dict:
        data = _read_json(self.pokemon_skills_file)
        if not isinstance(data, dict):
            data = {}
        pokemon = data.get("pokemon", {}) or {}
        order = data.get("order", {}) or {}
        lure_order = data.get("lure_order", {}) or {}
        # capturar las skills del pokemon ACTIVO desde el estado del agente
        state = _read_json(self.state_file)
        name = str(state.get("active_pokemon", "") or "")
        moves = state.get("moves", []) or []
        if name and moves:
            skills = dict(pokemon.get(name, {}))
            changed = False
            for m in moves:
                k = str(m.get("key", ""))
                if not k:
                    continue
                entry = {"name": str(m.get("name", "")), "aoe": bool(m.get("aoe")),
                         "effect": str(m.get("effect", ""))}
                if skills.get(k) != entry:
                    skills[k] = entry
                    changed = True
            if skills and changed:
                pokemon[name] = skills
                self._write_pokemon_skills({"pokemon": pokemon, "order": order,
                                            "lure_order": lure_order})
        return {"pokemon": pokemon, "order": order, "lure_order": lure_order}

    def save_pokemon_lure_order(self, body: dict) -> dict:
        name = (body.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "sin nombre"}
        order = [str(k) for k in (body.get("order") or [])]
        with self.lock:
            data = _read_json(self.pokemon_skills_file)
            if not isinstance(data, dict):
                data = {}
            data.setdefault("pokemon", {})
            data.setdefault("order", {})
            data.setdefault("lure_order", {})
            # lista vacia = sin combo propio (el lure usa la logica por defecto)
            if order:
                data["lure_order"][name] = order
            else:
                data["lure_order"].pop(name, None)
            try:
                os.makedirs(os.path.dirname(self.pokemon_skills_file), exist_ok=True)
                tmp = self.pokemon_skills_file + ".tmp"
                with open(tmp, "w", encoding="utf-8") as handle:
                    json.dump(data, handle, indent=2, ensure_ascii=False)
                os.replace(tmp, self.pokemon_skills_file)
            except OSError as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "lure_order": data["lure_order"]}

    def delete_pokemon_skill(self, body: dict) -> dict:
        """Borra un Pokemon del catalogo (pokemon + order + lure_order)."""
        name = (body.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "sin nombre"}
        with self.lock:
            data = _read_json(self.pokemon_skills_file)
            if not isinstance(data, dict):
                data = {}
            pokemon = data.get("pokemon", {}) or {}
            order = data.get("order", {}) or {}
            lure_order = data.get("lure_order", {}) or {}
            pokemon.pop(name, None)
            order.pop(name, None)
            lure_order.pop(name, None)
            self._write_pokemon_skills({"pokemon": pokemon, "order": order,
                                        "lure_order": lure_order})
            return {"ok": True, "name": name}

    def save_pokemon_order(self, body: dict) -> dict:
        name = (body.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "sin nombre"}
        order = [str(k) for k in (body.get("order") or [])]
        with self.lock:
            data = _read_json(self.pokemon_skills_file)
            if not isinstance(data, dict):
                data = {}
            data.setdefault("pokemon", {})
            data.setdefault("order", {})
            data["order"][name] = order
            try:
                os.makedirs(os.path.dirname(self.pokemon_skills_file), exist_ok=True)
                tmp = self.pokemon_skills_file + ".tmp"
                with open(tmp, "w", encoding="utf-8") as handle:
                    json.dump(data, handle, indent=2, ensure_ascii=False)
                os.replace(tmp, self.pokemon_skills_file)
            except OSError as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "order": data["order"]}

    def _write_ignore(self, names: set, ids: set) -> dict:
        out = {"names": sorted(names), "ids": sorted(ids)}
        try:
            os.makedirs(os.path.dirname(self.ignore_file), exist_ok=True)
            tmp = self.ignore_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(out, handle, indent=2, ensure_ascii=False)
            os.replace(tmp, self.ignore_file)
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, **out}

    def _clans(self):
        table = getattr(self, "_clan_cache", None)
        if table is None:
            from .clans import ClanTable

            path = (self.cfg.get("lua", {}) or {}).get("clans_file", "")
            table = ClanTable(path)
            self._clan_cache = table
        table.maybe_reload()
        return table

    def ignore(self) -> dict:
        data = _read_json(self.ignore_file)
        if not isinstance(data, dict):
            data = {}
        names = {str(n) for n in (data.get("names") or [])}
        ids = {str(i) for i in (data.get("ids") or [])}
        state = _read_json(self.state_file)
        seen = {}
        for c in (state.get("creatures") or []):
            seen[str(c.get("id", ""))] = c
        for tile in (state.get("nearby") or []):
            for c in (tile.get("creatures") or []):
                seen[str(c.get("id", ""))] = c
        for b in (state.get("battle") or []):
            seen.setdefault(str(b.get("id", "")), b)
        creatures = []
        clans = self._clans()
        for uid, c in seen.items():
            if not uid or uid == "0":
                continue
            name = str(c.get("name", ""))
            skull = c.get("skull")
            creatures.append({
                "id": uid, "name": name, "outfit": c.get("outfit"),
                "hp": c.get("hp"), "x": c.get("x"), "y": c.get("y"), "z": c.get("z"),
                "monster": bool(c.get("monster")), "player": bool(c.get("player")),
                "npc": bool(c.get("npc")),
                "skull": skull,
                "clan": clans.clan_for(skull) if skull else "",
                "ignored": name in names or uid in ids,
            })
        creatures.sort(key=lambda c: (not c["monster"], c["name"]))
        return {"names": sorted(names), "ids": sorted(ids), "creatures": creatures}

    def patch_ignore(self, body: dict) -> dict:
        with self.lock:
            data = _read_json(self.ignore_file)
            if not isinstance(data, dict):
                data = {}
            names = {str(n) for n in (data.get("names") or [])}
            ids = {str(i) for i in (data.get("ids") or [])}
            name = str(body.get("name") or "").strip()
            uid = str(body.get("id") or "").strip()
            if body.get("action") == "remove":
                if name:
                    names.discard(name)
                if uid:
                    ids.discard(uid)
            else:
                if name:
                    names.add(name)
                if uid and uid != "0":
                    ids.add(uid)
            return self._write_ignore(names, ids)

    def _write_routes(self, routes: dict) -> None:
        if not self.routes_file:
            return
        os.makedirs(os.path.dirname(self.routes_file), exist_ok=True)
        tmp = self.routes_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"routes": routes}, handle, indent=2)
        os.replace(tmp, self.routes_file)

    def save_named_route(self, body: dict) -> dict:
        name = (body.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "nombre vacio"}
        with self.lock:
            routes = self.routes()
            routes[name] = {
                "waypoints": body.get("waypoints", []),
                "loop": bool(body.get("loop", True)),
                "ping_pong": bool(body.get("ping_pong", False)),
                "pokemon": str(body.get("pokemon", "") or ""),
                "pokemon_slot": body.get("pokemon_slot"),
            }
            self._write_routes(routes)
            return {"ok": True, "routes": routes}

    def delete_named_route(self, body: dict) -> dict:
        name = (body.get("name") or "").strip()
        with self.lock:
            routes = self.routes()
            routes.pop(name, None)
            self._write_routes(routes)
            return {"ok": True, "routes": routes}

    def world(self, z: int, window: int = 160) -> dict:
        state = _read_json(self.state_file)
        px, py = state.get("x"), state.get("y")
        control = control_io.read_control(self.control_file)
        explore = control.get("explore", {}) if isinstance(control, dict) else {}

        data = _read_json(self.world_file)
        if data and (data.get("known") or data.get("patrol")):
            tiles = [[int(x), int(y), 1] for x, y in data.get("known", [])]
            tiles += [[int(x), int(y), 0] for x, y in data.get("blocked", [])]
            return {
                "z": data.get("z", z),
                "player": [px, py],
                "tiles": tiles,
                "home": data.get("home") or explore.get("home"),
                "radius": data.get("radius", explore.get("radius")),
                "patrol": data.get("patrol", []),
            }

        tiles = []
        try:
            with open(self.walkmap_file, "r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    parts = line.strip().split(",")
                    if len(parts) != 4:
                        continue
                    try:
                        x, y, tz, w = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
                    except ValueError:
                        continue
                    if tz != z:
                        continue
                    if px is not None and py is not None:
                        if max(abs(x - px), abs(y - py)) > window:
                            continue
                    tiles.append([x, y, w])
        except OSError:
            pass
        return {
            "z": z, "player": [px, py], "tiles": tiles,
            "home": explore.get("home"), "radius": explore.get("radius"), "patrol": [],
        }

    def config(self) -> dict:
        data = load_config(self.cfg_path)
        return data

    def patch_config(self, patch: dict) -> dict:
        with self.lock:
            data = load_config(self.cfg_path)
            from .config import deep_merge
            merged = deep_merge(data, patch or {})
            save_config(merged, self.cfg_path)
            self.cfg = load_config(self.cfg_path)
            return {"ok": True}

    def patch_control(self, patch: dict) -> dict:
        with self.lock:
            current = control_io.read_control(self.control_file)
            merged = control_io.merge_control(current, patch or {})
            merged["updated"] = time.time()
            control_io.write_control(self.control_file, merged)
            return {"ok": True, "control": merged}

    def command(self, cmd: str) -> dict:
        cmd = (cmd or "").strip()
        if not cmd:
            return {"ok": False, "error": "comando vacio"}
        if not self.cmd_file:
            return {"ok": False, "error": "sin cmd_file"}
        try:
            with open(self.cmd_file, "a", encoding="utf-8") as handle:
                handle.write(cmd + "\n")
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "cmd": cmd}


class Handler(BaseHTTPRequestHandler):
    app: Dashboard = None  # type: ignore

    def log_message(self, *args):  # silenciar log de peticiones
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data: dict, code: int = 200) -> None:
        self._send(code, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def _static(self, rel: str) -> None:
        path = os.path.normpath(os.path.join(WEB_DIR, rel.lstrip("/")))
        if not path.startswith(WEB_DIR) or not os.path.isfile(path):
            self._send(404, b"not found", "text/plain")
            return
        ext = os.path.splitext(path)[1].lower()
        with open(path, "rb") as handle:
            body = handle.read()
        self._send(200, body, CONTENT_TYPES.get(ext, "application/octet-stream"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path in ("/", "/index.html"):
            self._static("index.html")
        elif path == "/api/state":
            self._json(self.app.state())
        elif path == "/api/process":
            self._json(self.app.processes())
        elif path == "/api/telegram":
            self._json(self.app.telegram())
        elif path == "/api/whatsapp":
            self._json(self.app.whatsapp())
        elif path == "/api/license":
            self._json(self.app.license())
        elif path == "/api/config":
            self._json(self.app.config())
        elif path == "/api/log":
            n = int((query.get("tail", ["200"])[0]) or 200)
            self._json({"lines": _tail(self.app.log_file, n)})
        elif path == "/api/world":
            state = _read_json(self.app.state_file)
            z = int(query.get("z", [str(state.get("z", 0))])[0])
            self._json(self.app.world(z))
        elif path == "/api/otmm/info":
            self._json(self.app.otmm_info())
        elif path == "/api/otmm":
            try:
                z = int(query.get("z", ["0"])[0])
                x0 = int(query.get("x0", ["0"])[0])
                y0 = int(query.get("y0", ["0"])[0])
                x1 = int(query.get("x1", [str(x0 + 255)])[0])
                y1 = int(query.get("y1", [str(y0 + 255)])[0])
            except ValueError:
                self._send(400, b"bad params", "text/plain")
                return
            mode = query.get("mode", ["color"])[0]
            region = self.app.otmm_region(z, x0, y0, x1, y1, mode)
            if not region:
                self._send(404, b"no minimap", "text/plain")
                return
            w, h, buf = region
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(buf)))
            self.send_header("X-Width", str(w))
            self.send_header("X-Height", str(h))
            self.send_header("X-X0", str(x0))
            self.send_header("X-Y0", str(y0))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(buf)
        elif path == "/api/route":
            self._json(self.app.route())
        elif path == "/api/routes":
            self._json({"routes": self.app.routes()})
        elif path == "/api/pokemon_skills":
            self._json(self.app.pokemon_skills())
        elif path == "/api/ignore":
            self._json(self.app.ignore())
        elif path == "/api/deaths":
            try:
                n = int((query.get("tail", ["10"])[0]) or 10)
            except ValueError:
                n = 10
            self._json(self.app.deaths(n))
        elif path == "/api/loot_items":
            self._json(self.app.loot_items())
        else:
            self._static(path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._body()
        if path == "/api/control":
            self._json(self.app.patch_control(body))
        elif path == "/api/command":
            self._json(self.app.command(body.get("cmd", "")))
        elif path == "/api/config":
            self._json(self.app.patch_config(body))
        elif path == "/api/route":
            self._json(self.app.save_route(body))
        elif path == "/api/routes":
            self._json(self.app.save_named_route(body))
        elif path == "/api/routes/delete":
            self._json(self.app.delete_named_route(body))
        elif path == "/api/pokemon_skills/order":
            self._json(self.app.save_pokemon_order(body))
        elif path == "/api/pokemon_skills/lure_order":
            self._json(self.app.save_pokemon_lure_order(body))
        elif path == "/api/pokemon_skills/delete":
            self._json(self.app.delete_pokemon_skill(body))
        elif path == "/api/ignore":
            self._json(self.app.patch_ignore(body))
        elif path == "/api/process":
            self._json(self.app.set_process_name(body.get("process_name", "")))
        elif path == "/api/telegram":
            self._json(self.app.patch_telegram(body))
        elif path == "/api/telegram/test":
            self._json(self.app.test_telegram(body))
        elif path == "/api/whatsapp":
            self._json(self.app.patch_whatsapp(body))
        elif path == "/api/whatsapp/test":
            self._json(self.app.test_whatsapp(body))
        elif path == "/api/license":
            self._json(self.app.patch_license(body))
        elif path == "/api/attach":
            self._json(self.app.attach_client(body.get("process_name")))
        elif path == "/api/bot":
            action = body.get("action")
            if action == "start":
                self._json(self.app.start_bot())
            elif action == "stop":
                self._json(self.app.stop_bot())
            else:
                self._json({"ok": False, "error": "accion invalida"}, 400)
        elif path == "/api/log/clear":
            self._json(self.app.clear_log())
        else:
            self._json({"ok": False, "error": "not found"}, 404)


def serve(cfg_path: str, host: str | None = None, port: int | None = None,
          open_browser: bool = True) -> int:
    paths.ensure_app_files()
    app = Dashboard(cfg_path)
    if host:
        app.host = host
    if port:
        app.port = port
    Handler.app = app
    app.start_telegram_commands()
    httpd = ThreadingHTTPServer((app.host, app.port), Handler)
    url = f"http://{app.host}:{app.port}/"
    print(f"GUI del bot en {url}")
    print("Ctrl+C para salir (el bot, si esta corriendo, sigue activo).")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nGUI detenida")
    finally:
        app.stop_telegram_commands()
        httpd.server_close()
    return 0


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="pxg-gui", description="GUI web local para el bot")
    parser.add_argument("--config", default=paths.default_config_path())
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--open", action="store_true", help="abrir el navegador")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    auto_open = args.open or bool(cfg.get("ui", {}).get("auto_open", True))
    return serve(args.config, host=args.host, port=args.port, open_browser=auto_open)


if __name__ == "__main__":
    sys.exit(main())
