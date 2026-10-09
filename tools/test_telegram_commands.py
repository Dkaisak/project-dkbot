#!/usr/bin/env python3
"""Tests de los comandos de Telegram (sin red ni cliente).

Cubre: parseo de aliases es/en/pt, teclados inline, texto de estado, lista de
permitidos y dispatch + confirmacion (con send_message/_api simulados).

Uso: python3 tools/test_telegram_commands.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pxg_bot import telegram_commands as tg  # noqa: E402


def test_parse_aliases() -> None:
    assert tg.parse_command("/start")[0] == "start"
    assert tg.parse_command("/iniciar")[0] == "start"
    assert tg.parse_command("/começar")[0] == "start"
    assert tg.parse_command("/comecar")[0] == "start"
    assert tg.parse_command("/detener")[0] == "stop"
    assert tg.parse_command("/pausar")[0] == "pause"
    assert tg.parse_command("/reanudar")[0] == "resume"
    assert tg.parse_command("/panico")[0] == "panic"
    assert tg.parse_command("/estado")[0] == "status"
    assert tg.parse_command("/ajuda")[0] == "help"
    assert tg.parse_command("/ruta")[0] == "route"
    assert tg.parse_command("/rota")[0] == "route"
    assert tg.parse_command("/rutas")[0] == "routes"
    assert tg.parse_command("/rotas")[0] == "routes"
    assert tg.parse_command("/seguir")[0] == "follow"
    assert tg.parse_command("/pararuta")[0] == "stoproute"
    assert tg.parse_command("/pararrota")[0] == "stoproute"
    assert tg.parse_command("/donde")[0] == "where"
    assert tg.parse_command("/onde")[0] == "where"
    assert tg.parse_command("/teleportar")[0] == "teleport"
    assert tg.parse_command("/muertes")[0] == "deaths"
    assert tg.parse_command("/mortes")[0] == "deaths"
    assert tg.parse_command("/contadores")[0] == "counters"
    assert tg.parse_command("/jugadores")[0] == "players"
    assert tg.parse_command("/registro")[0] == "log"
    assert tg.parse_command("/comportamiento")[0] == "behavior"
    assert tg.parse_command("/combate")[0] == "combat"
    assert tg.parse_command("/explorar")[0] == "explore"
    assert tg.parse_command("/saquear")[0] == "loot"
    assert tg.parse_command("/recuperacion")[0] == "recovery"
    assert tg.parse_command("/humanizar")[0] == "humanizer"
    assert tg.parse_command("/pantalla")[0] == "screenshot"
    assert tg.parse_command("/atachar")[0] == "attach"
    assert tg.parse_command("/volar")[0] == "fly"
    assert tg.parse_command("/voar")[0] == "fly"
    assert tg.parse_command("/subir")[0] == "flyup"
    assert tg.parse_command("/bajar")[0] == "flydown"
    assert tg.parse_command("/descer")[0] == "flydown"
    # sufijo @Bot
    assert tg.parse_command("/start@MiBot")[0] == "start"
    # idioma detectado
    assert tg.parse_command("/start")[1] == "en"
    assert tg.parse_command("/iniciar")[1] == "es"
    assert tg.parse_command("/começar")[1] == "pt"
    # invalidos
    assert tg.parse_command("hola") is None
    assert tg.parse_command("/foo") is None
    assert tg.parse_command("") is None
    print("OK telegram: parseo de aliases es/en/pt")


def test_keyboards() -> None:
    kb = tg.build_status_keyboard("es")
    assert "cmd:es:start" in kb and "cmd:es:panic" in kb and "inline_keyboard" in kb
    kb2 = tg.build_confirm_keyboard("pt", "stop")
    assert "confirm:pt:stop:yes" in kb2 and "confirm:pt:stop:no" in kb2
    print("OK telegram: teclados inline")


def test_status_text() -> None:
    st = {"running": True, "pid": 123, "connected": True, "hp": 10, "maxhp": 10,
          "hppct": 100, "x": 1, "y": 2, "z": 7, "behavior": "route",
          "counters": {"kill": 3, "loot": 2}}
    txt = tg.status_text(st, "es")
    assert "pid 123" in txt and "route" in txt and "kill=3" in txt
    txt_en = tg.status_text(st, "en")
    assert "Status" in txt_en
    print("OK telegram: texto de estado")


def test_allowlist() -> None:
    tg_cfg = {"chat_id": "-100"}
    ok = tg.TelegramCommandListener._allowed
    assert ok("-100", "5", tg_cfg, {}) is True
    assert ok("-200", "5", tg_cfg, {}) is False
    assert ok("-200", "5", tg_cfg, {"allowed_chat_ids": ["-200"]}) is True
    assert ok("-200", "5", tg_cfg, {"allowed_chat_ids": ["-200"],
                                    "allowed_user_ids": ["9"]}) is False
    assert ok("-200", "9", tg_cfg, {"allowed_chat_ids": ["-200"],
                                    "allowed_user_ids": ["9"]}) is True
    print("OK telegram: lista de permitidos")


class FakeApp:
    def __init__(self) -> None:
        self.cfg = {"telegram": {"chat_id": "-100",
                                 "commands": {"enabled": True, "confirm_destructive": True}},
                    "behaviors": {"recovery": {"teleport": {"pokemon_slot": 1},
                                               "fly": {"pokemon_slot": 2}}}}
        self.calls: list = []

    def start_bot(self):
        self.calls.append("start")
        return {"ok": True, "pid": 42}

    def stop_bot(self):
        self.calls.append("stop")
        return {"ok": True, "stopped": True}

    def patch_control(self, patch):
        self.calls.append(("control", patch))
        return {"ok": True}

    def state(self):
        return {"running": True, "pid": 42, "connected": True, "hp": 1, "maxhp": 1,
                "hppct": 100, "x": 1, "y": 2, "z": 7, "behavior": "idle", "counters": {}}

    def routes(self):
        return {"Venusaur Out": {"waypoints": [[1, 2, 6], [3, 4, 6]], "loop": True,
                                 "pokemon": "Shiny fearow", "pokemon_slot": 2}}

    def save_route(self, route):
        self.calls.append(("route", route))
        return {"ok": True}

    def deaths(self, tail=10):
        return {"deaths": [{"index": 1, "signals": ["exp_drop"], "level": 150, "pos": [1, 2, 6]}]}

    def players(self):
        return {"players": [{"name": "Rival", "dist": 3}]}

    def log_tail(self, n=15):
        return {"lines": ["line1", "line2"]}

    def command(self, cmd):
        self.calls.append(("command", cmd))
        return {"ok": True, "cmd": cmd}

    def patch_config(self, patch):
        self.calls.append(("config", patch))
        return {"ok": True}

    def attach_client(self):
        self.calls.append("attach")
        return {"ok": True}

    def client_pid(self):
        return 1234

    def send_client_key(self, key):
        self.calls.append(("key", key))
        return {"ok": True}


def test_dispatch_and_confirm() -> None:
    sent: list = []
    orig_send = tg.send_message
    orig_api = tg._api
    tg.send_message = lambda token, chat, text, **kw: (
        sent.append((text, kw.get("reply_markup"))) or (True, ""))
    tg._api = lambda *a, **k: None
    try:
        app = FakeApp()
        listener = tg.TelegramCommandListener(app, "")
        cmds = app.cfg["telegram"]["commands"]
        # /start -> ejecuta start_bot y responde
        listener._dispatch("tok", "-100", "start", "es", [], cmds)
        assert app.calls == ["start"], app.calls
        assert sent and "iniciado" in sent[-1][0].lower(), sent
        # /stop -> pide confirmacion, NO ejecuta
        app.calls.clear()
        listener._dispatch("tok", "-100", "stop", "es", [], cmds)
        assert app.calls == [], "stop no debe ejecutar sin confirmar"
        assert "DETENER" in sent[-1][0]
        assert "confirm:es:stop:yes" in (sent[-1][1] or "")
        # callback yes -> ejecuta
        listener._handle_callback("tok", "-100", "confirm:es:stop:yes", cmds)
        assert app.calls == ["stop"], app.calls
        # callback no -> cancelado (no ejecuta)
        app.calls.clear()
        listener._handle_callback("tok", "-100", "confirm:es:stop:no", cmds)
        assert app.calls == []
        # boton inline pause
        listener._handle_callback("tok", "-100", "cmd:es:pause", cmds)
        assert ("control", {"paused": True}) in app.calls, app.calls
        # /panic con confirmacion
        app.calls.clear()
        listener._dispatch("tok", "-100", "panic", "es", [], cmds)
        assert app.calls == []
        listener._handle_callback("tok", "-100", "confirm:es:panic:yes", cmds)
        assert "stop" in app.calls and ("control", {"paused": True}) in app.calls
    finally:
        tg.send_message = orig_send
        tg._api = orig_api
    print("OK telegram: dispatch + confirmacion")


def test_route_commands() -> None:
    sent: list = []
    orig_send = tg.send_message
    orig_api = tg._api
    tg.send_message = lambda token, chat, text, **kw: (
        sent.append((text, kw.get("reply_markup"))) or (True, ""))
    tg._api = lambda *a, **k: None
    try:
        app = FakeApp()
        listener = tg.TelegramCommandListener(app, "")
        cmds = app.cfg["telegram"]["commands"]
        # /routes lista con botones
        listener._dispatch("t", "-1", "routes", "es", [], cmds)
        assert "Venusaur Out" in sent[-1][0], sent[-1]
        assert "route:es:Venusaur Out" in (sent[-1][1] or "")
        # /route <nombre> carga y aplica
        app.calls.clear()
        listener._dispatch("t", "-1", "route", "es", ["Venusaur", "Out"], cmds)
        assert app.calls and app.calls[-1][0] == "route", app.calls
        assert app.calls[-1][1]["enabled"] is True
        assert len(app.calls[-1][1]["waypoints"]) == 2
        # ruta inexistente
        listener._dispatch("t", "-1", "route", "es", ["Nope"], cmds)
        assert "Nope" in sent[-1][0]
        # /route sin nombre -> uso
        listener._dispatch("t", "-1", "route", "es", [], cmds)
        assert "route" in sent[-1][0].lower()
        # /follow y /stoproute
        app.calls.clear()
        listener._dispatch("t", "-1", "follow", "es", [], cmds)
        assert ("control", {"behaviors": {"route": {"enabled": True}}}) in app.calls
        listener._dispatch("t", "-1", "stoproute", "es", [], cmds)
        assert ("control", {"behaviors": {"route": {"enabled": False}}}) in app.calls
        # callback de boton de ruta
        app.calls.clear()
        listener._handle_callback("t", "-1", "route:es:Venusaur Out", cmds)
        assert app.calls and app.calls[-1][0] == "route", app.calls
    finally:
        tg.send_message = orig_send
        tg._api = orig_api
    print("OK telegram: comandos de rutas")


def test_info_commands() -> None:
    sent: list = []
    orig_send = tg.send_message
    orig_api = tg._api
    tg.send_message = lambda token, chat, text, **kw: (
        sent.append((text, kw.get("reply_markup"))) or (True, ""))
    tg._api = lambda *a, **k: None
    try:
        app = FakeApp()
        listener = tg.TelegramCommandListener(app, "")
        cmds = app.cfg["telegram"]["commands"]
        listener._dispatch("t", "-1", "where", "es", [], cmds)
        assert "Posición" in sent[-1][0]
        listener._dispatch("t", "-1", "counters", "es", [], cmds)
        assert "Contadores" in sent[-1][0]
        listener._dispatch("t", "-1", "deaths", "es", [], cmds)
        assert "muertes" in sent[-1][0].lower() and "#1" in sent[-1][0]
        listener._dispatch("t", "-1", "players", "es", [], cmds)
        assert "Rival" in sent[-1][0]
        listener._dispatch("t", "-1", "log", "es", ["5"], cmds)
        assert "line2" in sent[-1][0]
        # teleport -> comando al agente con slot del config
        app.calls.clear()
        listener._dispatch("t", "-1", "teleport", "es", ["outland", "north"], cmds)
        assert ("command", "teleport 1 outland north") in app.calls, app.calls
        # sin destino -> uso
        listener._dispatch("t", "-1", "teleport", "es", [], cmds)
        assert "teleport" in sent[-1][0].lower()
    finally:
        tg.send_message = orig_send
        tg._api = orig_api
    print("OK telegram: comandos de info/navegacion")


def test_extras_commands() -> None:
    sent: list = []
    photos: list = []
    orig = (tg.send_message, tg.send_photo, tg._api, tg.capture_screen)
    tg.send_message = lambda token, chat, text, **kw: (
        sent.append((text, kw.get("reply_markup"))) or (True, ""))
    tg.send_photo = lambda token, chat, photo, **kw: (
        photos.append((photo, kw.get("caption"))) or (True, ""))
    tg._api = lambda *a, **k: None
    tg.capture_screen = lambda pid=None: b"\x89PNG fake"
    try:
        app = FakeApp()
        listener = tg.TelegramCommandListener(app, "")
        cmds = app.cfg["telegram"]["commands"]
        # behavior generico
        app.calls.clear()
        listener._dispatch("t", "-1", "behavior", "es", ["combat", "off"], cmds)
        assert ("control", {"behaviors": {"combat": {"enabled": False}}}) in app.calls, app.calls
        # atajo + on/off multilingue
        listener._dispatch("t", "-1", "combat", "es", ["on"], cmds)
        assert ("control", {"behaviors": {"combat": {"enabled": True}}}) in app.calls
        listener._dispatch("t", "-1", "explore", "es", ["desactivar"], cmds)
        assert ("control", {"behaviors": {"explore": {"enabled": False}}}) in app.calls
        # catchall
        listener._dispatch("t", "-1", "catchall", "es", ["on"], cmds)
        assert ("control", {"capture": {"catch_all": True}}) in app.calls
        # humanizer
        app.calls.clear()
        listener._dispatch("t", "-1", "humanizer", "es", ["aggressive"], cmds)
        assert ("config", {"settings": {"humanizer": {"profile": "aggressive"}}}) in app.calls
        assert ("control", {"humanizer": {"profile": "aggressive"}}) in app.calls
        # screenshot
        photos.clear()
        listener._dispatch("t", "-1", "screenshot", "es", [], cmds)
        assert photos and photos[-1][0].startswith(b"\x89PNG"), photos
        # attach
        app.calls.clear()
        listener._dispatch("t", "-1", "attach", "es", [], cmds)
        assert "attach" in app.calls
    finally:
        (tg.send_message, tg.send_photo, tg._api, tg.capture_screen) = orig
    print("OK telegram: extras (behaviors/screenshot/attach/humanizer)")


def test_fly_commands() -> None:
    sent: list = []
    orig = (tg.send_message, tg._api)
    tg.send_message = lambda token, chat, text, **kw: (sent.append(text) or (True, ""))
    tg._api = lambda *a, **k: None
    try:
        app = FakeApp()
        listener = tg.TelegramCommandListener(app, "")
        cmds = app.cfg["telegram"]["commands"]
        app.calls.clear()
        listener._dispatch("t", "-1", "fly", "es", [], cmds)
        assert ("command", "fly 2") in app.calls, app.calls
        assert "Vuelo" in sent[-1]
        listener._dispatch("t", "-1", "flyup", "es", [], cmds)
        assert ("command", "flyup") in app.calls, app.calls
        listener._dispatch("t", "-1", "flydown", "es", [], cmds)
        assert ("command", "flydown") in app.calls
    finally:
        (tg.send_message, tg._api) = orig
    print("OK telegram: fly/flyup/flydown")


def main() -> int:
    test_parse_aliases()
    test_keyboards()
    test_status_text()
    test_allowlist()
    test_dispatch_and_confirm()
    test_route_commands()
    test_info_commands()
    test_extras_commands()
    test_fly_commands()
    print("\nTELEGRAM OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
