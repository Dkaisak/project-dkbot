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
from .config import load_config, save_config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _tail(path: str, n: int) -> list[str]:
    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            lines = handle.readlines()
        return [line.rstrip("\n") for line in lines[-max(1, n):]]
    except OSError:
        return []


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

    def _apply_cfg(self, cfg: dict) -> None:
        self.cfg = cfg
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

    def attach_client(self) -> dict:
        """Detecta el cliente, ajusta rutas e inyecta el agente (boton GUI)."""
        from . import setup

        with self.lock:
            process = self.cfg.get("process_name") or setup.DEFAULT_PROCESS
            res = setup.attach(self.cfg_path, process_name=process, install=True)
            if res.get("ok"):
                self._apply_cfg(load_config(self.cfg_path))
                self._minimap = None
            return res

    # --- proceso del bot ---
    def _pid_alive(self, pid: int) -> bool:
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

    def start_bot(self) -> dict:
        with self.lock:
            if self.bot_pid():
                return {"ok": False, "error": "el bot ya esta corriendo"}
            if self._log_handle is None and self.log_file:
                os.makedirs(os.path.dirname(self.log_file), exist_ok=True)
                self._log_handle = open(self.log_file, "ab", buffering=0)
            cmd = [sys.executable, "-u", os.path.join(ROOT, "main.py"),
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
            "connected": bool(state.get("connected", False)),
            "paused": bool(status.get("paused", False) or control.get("paused", False)),
            "behavior": playing or ("break" if status.get("chosen") == "break" else "idle"),
            "humanizer": status.get("humanizer", ""),
            "counters": status.get("counters", {}),
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
            "ts": time.time(),
        }

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
        for uid, c in seen.items():
            if not uid or uid == "0":
                continue
            name = str(c.get("name", ""))
            creatures.append({
                "id": uid, "name": name, "outfit": c.get("outfit"),
                "hp": c.get("hp"), "x": c.get("x"), "y": c.get("y"), "z": c.get("z"),
                "monster": bool(c.get("monster")), "player": bool(c.get("player")),
                "npc": bool(c.get("npc")),
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
        elif path == "/api/attach":
            self._json(self.app.attach_client())
        elif path == "/api/bot":
            action = body.get("action")
            if action == "start":
                self._json(self.app.start_bot())
            elif action == "stop":
                self._json(self.app.stop_bot())
            else:
                self._json({"ok": False, "error": "accion invalida"}, 400)
        else:
            self._json({"ok": False, "error": "not found"}, 404)


def serve(cfg_path: str, host: str | None = None, port: int | None = None,
          open_browser: bool = True) -> int:
    app = Dashboard(cfg_path)
    if host:
        app.host = host
    if port:
        app.port = port
    Handler.app = app
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
        httpd.server_close()
    return 0


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="pxg-gui", description="GUI web local para el bot")
    parser.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--open", action="store_true", help="abrir el navegador")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    auto_open = args.open or bool(cfg.get("ui", {}).get("auto_open", True))
    return serve(args.config, host=args.host, port=args.port, open_browser=auto_open)


if __name__ == "__main__":
    sys.exit(main())
