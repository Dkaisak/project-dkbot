"""Comandos entrantes por Telegram (long-poll) para controlar el bot.

El listener vive en el **supervisor** (GUI/app), que es quien arranca/para el bot
y gestiona rutas/control. Reutiliza `notify.send_message` para responder.

Sin dependencias (stdlib): long-poll de `getUpdates`, offset persistido, lista de
permitidos y botones inline con confirmacion para acciones destructivas.

Uso (desde `webui.Dashboard`)::

    app.start_telegram_commands()   # arranca el hilo
    app.stop_telegram_commands()    # al cerrar

Comandos (aliases es / en / pt): start|iniciar|comecar, stop|detener|parar,
pause|pausar, resume|reanudar|continuar, panic|panico|panico, status|estado,
help|ayuda|ajuda.
"""
from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request

from .notify import send_message, send_photo

_API = "https://api.telegram.org/bot%s/%s"


def _api(token: str, method: str, params: dict | None = None, timeout: float = 30.0):
    """Llama a la Bot API. Devuelve el JSON (dict) o None si falla."""
    if not token:
        return None
    url = _API % (token, method)
    data = urllib.parse.urlencode(params or {}).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "ignore")
    except Exception:
        return None
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    return parsed if parsed.get("ok") else None


# --- aliases (es / en / pt) ---
ALIASES = {
    "start":  ("start", "iniciar", "comecar", "começar"),
    "stop":   ("stop", "detener", "parar"),
    "pause":  ("pause", "pausar"),
    "resume": ("resume", "reanudar", "continuar"),
    "panic":  ("panic", "panico", "pánico", "pânico"),
    "status": ("status", "estado"),
    "help":   ("help", "ayuda", "ajuda"),
    "route":  ("route", "ruta", "rota"),
    "routes": ("routes", "rutas", "rotas"),
    "follow": ("follow", "seguir"),
    "stoproute": ("stoproute", "pararuta", "pararrota", "pararota"),
    "where":  ("where", "donde", "onde"),
    "teleport": ("teleport", "teleportar"),
    "deaths": ("deaths", "muertes", "mortes"),
    "counters": ("counters", "contadores"),
    "players": ("players", "jugadores", "jogadores"),
    "log": ("log", "registro"),
    "behavior": ("behavior", "comportamiento", "comportamento"),
    "combat": ("combat", "combate"),
    "explore": ("explore", "explorar"),
    "loot": ("loot", "saquear"),
    "capture": ("capture", "captura"),
    "revive": ("revive", "revivir", "reviver"),
    "recovery": ("recovery", "recuperacion", "recuperação", "recuperacao"),
    "catchall": ("catchall",),
    "humanizer": ("humanizer", "humanizar"),
    "screenshot": ("screenshot", "pantalla", "tela"),
    "attach": ("attach", "atachar"),
    "fly": ("fly", "volar", "voar"),
    "flyup": ("flyup", "subir"),
    "flydown": ("flydown", "bajar", "descer"),
}
ALIAS_TO_ACTION = {a: act for act, aliases in ALIASES.items() for a in aliases}

# idioma del alias (para responder en el idioma del comando). Los ambiguos
# es/pt se resuelven a es por defecto.
_ALIAS_LANG = {
    "start": "en", "iniciar": "es", "comecar": "pt", "começar": "pt",
    "stop": "en", "detener": "es", "parar": "es",
    "pause": "en", "pausar": "es",
    "resume": "en", "reanudar": "es", "continuar": "es",
    "panic": "en", "panico": "es", "pánico": "es", "pânico": "pt",
    "status": "en", "estado": "es",
    "help": "en", "ayuda": "es", "ajuda": "pt",
    "route": "en", "ruta": "es", "rota": "pt",
    "routes": "en", "rutas": "es", "rotas": "pt",
    "follow": "en", "seguir": "es",
    "stoproute": "en", "pararuta": "es", "pararrota": "pt", "pararota": "pt",
    "where": "en", "donde": "es", "onde": "pt",
    "teleport": "en", "teleportar": "es",
    "deaths": "en", "muertes": "es", "mortes": "pt",
    "counters": "en", "contadores": "es",
    "players": "en", "jugadores": "es", "jogadores": "pt",
    "log": "en", "registro": "es",
    "behavior": "en", "comportamiento": "es", "comportamento": "pt",
    "combat": "en", "combate": "es",
    "explore": "en", "explorar": "es",
    "loot": "en", "saquear": "es",
    "capture": "en", "captura": "es",
    "revive": "en", "revivir": "es", "reviver": "pt",
    "recovery": "en", "recuperacion": "es", "recuperação": "pt", "recuperacao": "pt",
    "catchall": "en",
    "humanizer": "en", "humanizar": "es",
    "screenshot": "en", "pantalla": "es", "tela": "pt",
    "attach": "en", "atachar": "es",
    "fly": "en", "volar": "es", "voar": "pt",
    "flyup": "en", "subir": "es",
    "flydown": "en", "bajar": "es", "descer": "pt",
}

# comportamientos que se pueden activar/desactivar por comando
VALID_BEHAVIORS = ("crisis", "heal", "capture", "combat", "loot", "revive",
                   "summon", "route", "explore", "recovery")
BEHAVIOR_SHORTCUTS = ("combat", "explore", "loot", "capture", "revive", "recovery")

_ON = {"on", "true", "1", "si", "sí", "sim", "yes", "enable", "enabled",
       "activar", "activado", "activo", "encender", "ligar"}
_OFF = {"off", "false", "0", "no", "disable", "disabled",
        "desactivar", "desactivado", "inactivo", "apagar", "desligar"}


def parse_onoff(token) -> bool | None:
    t = str(token or "").strip().lower()
    if t in _ON:
        return True
    if t in _OFF:
        return False
    return None


def _capture_window_win(hwnd):
    """Captura el contenido de una ventana con PrintWindow (aunque este tapada)."""
    try:
        import ctypes
        from ctypes import wintypes
        from PIL import Image
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w <= 0 or h <= 0:
            return None
        hwnd_dc = user32.GetWindowDC(hwnd)
        if not hwnd_dc:
            return None
        mfc_dc = gdi32.CreateCompatibleDC(hwnd_dc)
        bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
        old = gdi32.SelectObject(mfc_dc, bmp)
        user32.PrintWindow(hwnd, mfc_dc, 2)  # PW_RENDERFULLCONTENT

        class BMIH(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        bmi = BMIH()
        bmi.biSize = ctypes.sizeof(BMIH)
        bmi.biWidth = w
        bmi.biHeight = -h  # top-down
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mfc_dc, bmp, 0, h, buf, ctypes.byref(bmi), 0)
        img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
        gdi32.SelectObject(mfc_dc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mfc_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception:
        return None


def capture_screen(pid=None):
    """Bytes PNG de la ventana del cliente (o de la pantalla). None si no se puede."""
    if pid and sys.platform == "win32":
        try:
            from .input import find_window_for_pid
            hwnd = find_window_for_pid(int(pid))
            if hwnd:
                data = _capture_window_win(hwnd)
                if data:
                    return data
        except Exception:
            pass
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None

TEXTS = {
    "es": {
        "help": ("<b>ShinyBot — comandos</b>\n"
                 "/start · /iniciar · /começar — arrancar el bot\n"
                 "/stop · /detener · /parar — parar el bot\n"
                 "/pause · /pausar — pausar\n"
                 "/resume · /reanudar · /continuar — reanudar\n"
                 "/panic · /panico · /pânico — pánico (parar + pausar)\n"
                 "/route · /ruta · /rota <nombre> — ir a una ruta guardada\n"
                 "/routes · /rutas · /rotas — listar rutas\n"
                 "/follow · /seguir — seguir la ruta\n"
                 "/stoproute · /pararuta — parar la ruta\n"
                 "/where · /donde · /onde — posición\n"
                 "/teleport · /teleportar <destino> — teletransportar\n"
                 "/deaths · /muertes · /mortes — últimas muertes\n"
                 "/counters · /contadores — contadores\n"
                 "/players · /jugadores · /jogadores — jugadores cerca\n"
                 "/log · /registro [n] — registro\n"
                 "/behavior · /comportamiento <nombre> <on|off> — activar/desactivar\n"
                 "/screenshot · /pantalla · /tela — captura de pantalla\n"
                 "/attach · /atachar — reinyectar el agente\n"
                 "/fly · /volar · /voar — volar (orderonself)\n"
                 "/flyup · /subir — subir en vuelo · /flydown · /bajar — bajar\n"
                 "/status · /estado — estado\n"
                 "/help · /ayuda · /ajuda — esta ayuda"),
        "started": "▶ <b>Bot iniciado</b> (pid {pid}).",
        "already": "El bot ya está corriendo.",
        "stopped": "■ <b>Bot detenido</b>.",
        "not_running": "El bot no estaba corriendo.",
        "paused": "⏸ <b>Bot pausado</b>.",
        "resumed": "⏵ <b>Bot reanudado</b>.",
        "panic": "⚠ <b>Pánico</b>: bot detenido y en pausa.",
        "confirm_stop": "¿Seguro que quieres <b>DETENER</b> el bot?",
        "confirm_panic": "¿Seguro que quieres activar el <b>PÁNICO</b>?",
        "cancelled": "Cancelado.",
        "denied": "No autorizado.",
        "status_title": "<b>Estado</b>",
        "running": "running", "connected": "conectado", "pos": "posición",
        "behavior": "behavior", "counters": "contadores",
        "on": "sí", "off": "no",
        "btn_start": "▶ Iniciar", "btn_stop": "■ Detener",
        "btn_pause": "⏸ Pausar", "btn_resume": "⏵ Reanudar",
        "btn_panic": "⚠ Pánico", "yes": "✅ Sí", "cancel": "✖ Cancelar",
        "routes_title": "<b>Rutas guardadas</b>",
        "no_routes": "No hay rutas guardadas.",
        "route_loaded": "▶ <b>Ruta</b> «{name}» aplicada ({n} waypoints).",
        "route_not_found": "No existe la ruta «{name}».",
        "route_usage": "Uso: /route <nombre> (lista con /routes).",
        "follow_on": "▶ Siguiendo la ruta.",
        "follow_off": "■ Ruta detenida.",
        "where_title": "📍 <b>Posición</b>",
        "counters_title": "<b>Contadores</b>",
        "deaths_title": "<b>Últimas muertes</b>",
        "players_title": "<b>Jugadores cerca</b>",
        "log_title": "<b>Registro</b>",
        "none": "—", "dist": "dist", "level": "nivel",
        "teleport_sent": "🌀 Teleport en curso a «{dest}».",
        "teleport_usage": "Uso: /teleport <destino> (p. ej. outland north).",
        "behavior_usage": "Uso: /behavior <nombre> <on|off>. Nombres: crisis, heal, combat, loot, capture, revive, summon, route, explore, recovery.",
        "behavior_set": "• {name}: {state}",
        "catchall_set": "• capturar todo (catch_all): {state}",
        "humanizer_set": "• humanizer: {profile}",
        "humanizer_usage": "Uso: /humanizer <light|normal|aggressive>.",
        "screenshot_caption": "📷 Pantalla del cliente",
        "screenshot_fail": "⚠ No se pudo capturar la pantalla del cliente.",
        "attach_ok": "🔌 Agente reinyectado.",
        "attach_fail": "⚠ No se pudo atachar: {error}",
        "fly_sent": "🕊 Vuelo iniciado (slot {slot}). Usa /flyup y /flydown para la altura.",
        "flyup_sent": "⬆ Subiendo en vuelo.",
        "flydown_sent": "⬇ Bajando en vuelo.",
        "fly_fail": "⚠ No se pudo enviar el comando de vuelo: {error}",
    },
    "en": {
        "help": ("<b>ShinyBot — commands</b>\n"
                 "/start · /iniciar · /começar — start the bot\n"
                 "/stop · /detener · /parar — stop the bot\n"
                 "/pause · /pausar — pause\n"
                 "/resume · /reanudar · /continuar — resume\n"
                 "/panic · /panico · /pânico — panic (stop + pause)\n"
                 "/route · /ruta · /rota <name> — go to a saved route\n"
                 "/routes · /rutas · /rotas — list routes\n"
                 "/follow · /seguir — follow the route\n"
                 "/stoproute · /pararuta — stop the route\n"
                 "/where · /donde · /onde — position\n"
                 "/teleport · /teleportar <dest> — teleport\n"
                 "/deaths · /muertes · /mortes — last deaths\n"
                 "/counters · /contadores — counters\n"
                 "/players · /jugadores · /jogadores — nearby players\n"
                 "/log · /registro [n] — log\n"
                 "/behavior · /comportamiento <name> <on|off> — enable/disable\n"
                 "/screenshot · /pantalla · /tela — screenshot\n"
                 "/attach · /atachar — re-inject the agent\n"
                 "/fly · /volar · /voar — fly (orderonself)\n"
                 "/flyup · /subir — fly up · /flydown · /bajar — fly down\n"
                 "/status · /estado — status\n"
                 "/help · /ayuda · /ajuda — this help"),
        "started": "▶ <b>Bot started</b> (pid {pid}).",
        "already": "The bot is already running.",
        "stopped": "■ <b>Bot stopped</b>.",
        "not_running": "The bot was not running.",
        "paused": "⏸ <b>Bot paused</b>.",
        "resumed": "⏵ <b>Bot resumed</b>.",
        "panic": "⚠ <b>Panic</b>: bot stopped and paused.",
        "confirm_stop": "Are you sure you want to <b>STOP</b> the bot?",
        "confirm_panic": "Are you sure you want to trigger <b>PANIC</b>?",
        "cancelled": "Cancelled.",
        "denied": "Not authorized.",
        "status_title": "<b>Status</b>",
        "running": "running", "connected": "connected", "pos": "position",
        "behavior": "behavior", "counters": "counters",
        "on": "yes", "off": "no",
        "btn_start": "▶ Start", "btn_stop": "■ Stop",
        "btn_pause": "⏸ Pause", "btn_resume": "⏵ Resume",
        "btn_panic": "⚠ Panic", "yes": "✅ Yes", "cancel": "✖ Cancel",
        "routes_title": "<b>Saved routes</b>",
        "no_routes": "No saved routes.",
        "route_loaded": "▶ <b>Route</b> “{name}” applied ({n} waypoints).",
        "route_not_found": "Route “{name}” does not exist.",
        "route_usage": "Usage: /route <name> (list with /routes).",
        "follow_on": "▶ Following the route.",
        "follow_off": "■ Route stopped.",
        "where_title": "📍 <b>Position</b>",
        "counters_title": "<b>Counters</b>",
        "deaths_title": "<b>Last deaths</b>",
        "players_title": "<b>Nearby players</b>",
        "log_title": "<b>Log</b>",
        "none": "—", "dist": "dist", "level": "level",
        "teleport_sent": "🌀 Teleport in progress to “{dest}”.",
        "teleport_usage": "Usage: /teleport <dest> (e.g. outland north).",
        "behavior_usage": "Usage: /behavior <name> <on|off>. Names: crisis, heal, combat, loot, capture, revive, summon, route, explore, recovery.",
        "behavior_set": "• {name}: {state}",
        "catchall_set": "• catch all (catch_all): {state}",
        "humanizer_set": "• humanizer: {profile}",
        "humanizer_usage": "Usage: /humanizer <light|normal|aggressive>.",
        "screenshot_caption": "📷 Client screen",
        "screenshot_fail": "⚠ Could not capture the client screen.",
        "attach_ok": "🔌 Agent re-injected.",
        "attach_fail": "⚠ Could not attach: {error}",
        "fly_sent": "🕊 Fly started (slot {slot}). Use /flyup and /flydown for altitude.",
        "flyup_sent": "⬆ Flying up.",
        "flydown_sent": "⬇ Flying down.",
        "fly_fail": "⚠ Could not send the fly command: {error}",
    },
    "pt": {
        "help": ("<b>ShinyBot — comandos</b>\n"
                 "/start · /iniciar · /começar — iniciar o bot\n"
                 "/stop · /detener · /parar — parar o bot\n"
                 "/pause · /pausar — pausar\n"
                 "/resume · /reanudar · /continuar — continuar\n"
                 "/panic · /panico · /pânico — pânico (parar + pausar)\n"
                 "/route · /ruta · /rota <nome> — ir a uma rota salva\n"
                 "/routes · /rutas · /rotas — listar rotas\n"
                 "/follow · /seguir — seguir a rota\n"
                 "/stoproute · /pararuta — parar a rota\n"
                 "/where · /donde · /onde — posição\n"
                 "/teleport · /teleportar <destino> — teletransportar\n"
                 "/deaths · /muertes · /mortes — últimas mortes\n"
                 "/counters · /contadores — contadores\n"
                 "/players · /jugadores · /jogadores — jogadores perto\n"
                 "/log · /registro [n] — registro\n"
                 "/behavior · /comportamiento <nome> <on|off> — ativar/desativar\n"
                 "/screenshot · /pantalla · /tela — captura de tela\n"
                 "/attach · /atachar — reinjetar o agente\n"
                 "/fly · /volar · /voar — voar (orderonself)\n"
                 "/flyup · /subir — subir · /flydown · /bajar — descer\n"
                 "/status · /estado — estado\n"
                 "/help · /ayuda · /ajuda — esta ajuda"),
        "started": "▶ <b>Bot iniciado</b> (pid {pid}).",
        "already": "O bot já está rodando.",
        "stopped": "■ <b>Bot parado</b>.",
        "not_running": "O bot não estava rodando.",
        "paused": "⏸ <b>Bot pausado</b>.",
        "resumed": "⏵ <b>Bot continuado</b>.",
        "panic": "⚠ <b>Pânico</b>: bot parado e pausado.",
        "confirm_stop": "Tem certeza que quer <b>PARAR</b> o bot?",
        "confirm_panic": "Tem certeza que quer ativar o <b>PÂNICO</b>?",
        "cancelled": "Cancelado.",
        "denied": "Não autorizado.",
        "status_title": "<b>Estado</b>",
        "running": "rodando", "connected": "conectado", "pos": "posição",
        "behavior": "behavior", "counters": "contadores",
        "on": "sim", "off": "não",
        "btn_start": "▶ Iniciar", "btn_stop": "■ Parar",
        "btn_pause": "⏸ Pausar", "btn_resume": "⏵ Continuar",
        "btn_panic": "⚠ Pânico", "yes": "✅ Sim", "cancel": "✖ Cancelar",
        "routes_title": "<b>Rotas salvas</b>",
        "no_routes": "Não há rotas salvas.",
        "route_loaded": "▶ <b>Rota</b> “{name}” aplicada ({n} waypoints).",
        "route_not_found": "A rota “{name}” não existe.",
        "route_usage": "Uso: /route <nome> (lista com /routes).",
        "follow_on": "▶ Seguindo a rota.",
        "follow_off": "■ Rota parada.",
        "where_title": "📍 <b>Posição</b>",
        "counters_title": "<b>Contadores</b>",
        "deaths_title": "<b>Últimas mortes</b>",
        "players_title": "<b>Jogadores perto</b>",
        "log_title": "<b>Registro</b>",
        "none": "—", "dist": "dist", "level": "nível",
        "teleport_sent": "🌀 Teleport em curso para “{dest}”.",
        "teleport_usage": "Uso: /teleport <destino> (p. ex. outland north).",
        "behavior_usage": "Uso: /behavior <nome> <on|off>. Nomes: crisis, heal, combat, loot, capture, revive, summon, route, explore, recovery.",
        "behavior_set": "• {name}: {state}",
        "catchall_set": "• capturar tudo (catch_all): {state}",
        "humanizer_set": "• humanizer: {profile}",
        "humanizer_usage": "Uso: /humanizer <light|normal|aggressive>.",
        "screenshot_caption": "📷 Tela do cliente",
        "screenshot_fail": "⚠ Não foi possível capturar a tela do cliente.",
        "attach_ok": "🔌 Agente reinjetado.",
        "attach_fail": "⚠ Não foi possível atachar: {error}",
        "fly_sent": "🕊 Voo iniciado (slot {slot}). Use /flyup e /flydown para a altura.",
        "flyup_sent": "⬆ Subindo no voo.",
        "flydown_sent": "⬇ Descendo no voo.",
        "fly_fail": "⚠ Não foi possível enviar o comando de voo: {error}",
    },
}


def T(lang: str, key: str) -> str:
    table = TEXTS.get(lang) or TEXTS["es"]
    return table.get(key) or TEXTS["es"].get(key, key)


def parse_command(text: str):
    """Devuelve (action, lang, args) o None si no es un comando valido."""
    text = (text or "").strip()
    if not text.startswith("/"):
        return None
    parts = text[1:].split()
    if not parts:
        return None
    cmd = parts[0].lower()
    if "@" in cmd:  # /start@MiBot
        cmd = cmd.split("@", 1)[0]
    action = ALIAS_TO_ACTION.get(cmd)
    if not action:
        return None
    return action, _ALIAS_LANG.get(cmd, "es"), parts[1:]


def build_status_keyboard(lang: str) -> str:
    rows = [
        [{"text": T(lang, "btn_start"), "callback_data": f"cmd:{lang}:start"},
         {"text": T(lang, "btn_stop"), "callback_data": f"cmd:{lang}:stop"}],
        [{"text": T(lang, "btn_pause"), "callback_data": f"cmd:{lang}:pause"},
         {"text": T(lang, "btn_resume"), "callback_data": f"cmd:{lang}:resume"}],
        [{"text": T(lang, "btn_panic"), "callback_data": f"cmd:{lang}:panic"}],
    ]
    return json.dumps({"inline_keyboard": rows})


def build_confirm_keyboard(lang: str, action: str) -> str:
    rows = [[
        {"text": T(lang, "yes"), "callback_data": f"confirm:{lang}:{action}:yes"},
        {"text": T(lang, "cancel"), "callback_data": f"confirm:{lang}:{action}:no"},
    ]]
    return json.dumps({"inline_keyboard": rows})


def status_text(state: dict, lang: str) -> str:
    running = bool(state.get("running"))
    pid = state.get("pid")
    yn = lambda b: T(lang, "on") if b else T(lang, "off")
    counters = state.get("counters") or {}
    ctxt = ", ".join(f"{k}={v}" for k, v in sorted(counters.items())) or "—"
    head = T(lang, "status_title")
    runtxt = yn(running) + (f" (pid {pid})" if running and pid else "")
    return "\n".join([
        head,
        f"• {T(lang, 'running')}: {runtxt}",
        f"• {T(lang, 'connected')}: {yn(state.get('connected'))}",
        f"• HP: {state.get('hp')}/{state.get('maxhp')} ({state.get('hppct')}%)",
        f"• {T(lang, 'pos')}: ({state.get('x')},{state.get('y')},{state.get('z')})",
        f"• {T(lang, 'behavior')}: {state.get('behavior')}",
        f"• {T(lang, 'counters')}: {ctxt}",
    ])


class TelegramCommandListener(threading.Thread):
    """Hilo que hace long-poll de getUpdates y ejecuta los comandos."""

    def __init__(self, app, offset_path: str = ""):
        super().__init__(name="telegram-cmd", daemon=True)
        self.app = app
        self.offset_path = offset_path or ""
        self._stop = threading.Event()
        self._offset = self._load_offset()
        self._primed = self._offset > 0
        self.log: list = []

    # --- offset ---
    def _load_offset(self) -> int:
        if not self.offset_path:
            return 0
        try:
            with open(self.offset_path, "r", encoding="utf-8") as handle:
                return int(json.load(handle).get("offset", 0))
        except (OSError, ValueError):
            return 0

    def _save_offset(self) -> None:
        if not self.offset_path:
            return
        try:
            tmp = self.offset_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump({"offset": self._offset}, handle)
            os.replace(tmp, self.offset_path)
        except OSError:
            pass

    def stop(self) -> None:
        self._stop.set()

    # --- config ---
    def _cfg(self):
        tg = (getattr(self.app, "cfg", {}) or {}).get("telegram", {}) or {}
        cmds = tg.get("commands", {}) or {}
        return tg, cmds

    @staticmethod
    def _allowed(chat_id, user_id, tg: dict, cmds: dict) -> bool:
        chat = str(chat_id)
        allowed_chats = cmds.get("allowed_chat_ids") or []
        if allowed_chats:
            if chat not in {str(c) for c in allowed_chats}:
                return False
        else:
            main = str(tg.get("chat_id") or "")
            if main and chat != main:
                return False
        allowed_users = cmds.get("allowed_user_ids") or []
        if allowed_users and str(user_id) not in {str(u) for u in allowed_users}:
            return False
        return True

    # --- bucle ---
    def run(self) -> None:
        while not self._stop.is_set():
            tg, cmds = self._cfg()
            token = str(tg.get("bot_token") or "")
            if not (cmds.get("enabled", False) and token):
                self._stop.wait(3.0)
                continue
            if not self._primed:
                # saltar el backlog: tomar el ultimo update y seguir desde ahi
                _api(token, "deleteWebhook", {"drop_pending_updates": "true"}, timeout=15)
                last = _api(token, "getUpdates", {"offset": -1, "timeout": 0}, timeout=15)
                if last and last.get("result"):
                    self._offset = int(last["result"][-1].get("update_id", 0)) + 1
                    self._save_offset()
                self._primed = True
            timeout = int(cmds.get("poll_timeout", 25) or 25)
            res = _api(token, "getUpdates",
                       {"offset": self._offset, "timeout": timeout,
                        "allowed_updates": json.dumps(["message", "callback_query"])},
                       timeout=timeout + 10)
            if not res:
                self._stop.wait(1.0)
                continue
            for update in res.get("result", []):
                self._offset = int(update.get("update_id", 0)) + 1
                self._save_offset()
                try:
                    self._handle(token, tg, cmds, update)
                except Exception as exc:  # nunca tumbar el hilo
                    self._log(f"error: {type(exc).__name__}: {exc}")

    def _log(self, line: str) -> None:
        self.log.append(line)
        if len(self.log) > 200:
            del self.log[:100]

    # --- manejo de updates ---
    def _handle(self, token: str, tg: dict, cmds: dict, update: dict) -> None:
        msg = update.get("message")
        cb = update.get("callback_query")
        if msg:
            chat_id = (msg.get("chat") or {}).get("id")
            user_id = (msg.get("from") or {}).get("id")
            if not self._allowed(chat_id, user_id, tg, cmds):
                return
            parsed = parse_command(msg.get("text") or "")
            if not parsed:
                return
            action, lang, args = parsed
            self._log(f"cmd {action} ({lang}) chat={chat_id}")
            self._dispatch(token, chat_id, action, lang, args, cmds)
        elif cb:
            chat_id = ((cb.get("message") or {}).get("chat") or {}).get("id")
            user_id = (cb.get("from") or {}).get("id")
            _api(token, "answerCallbackQuery", {"callback_query_id": cb.get("id")})
            if not self._allowed(chat_id, user_id, tg, cmds):
                return
            data = cb.get("data") or ""
            self._log(f"callback {data} chat={chat_id}")
            self._handle_callback(token, chat_id, data, cmds)

    def _dispatch(self, token: str, chat_id, action: str, lang: str, args, cmds: dict) -> None:
        if action == "help":
            send_message(token, str(chat_id), T(lang, "help"))
            return
        if action == "status":
            state = self.app.state()
            send_message(token, str(chat_id), status_text(state, lang),
                         reply_markup=build_status_keyboard(lang))
            return
        if action == "routes":
            self._list_routes(token, str(chat_id), lang)
            return
        if action == "route":
            name = " ".join(args).strip()
            if not name:
                send_message(token, str(chat_id), T(lang, "route_usage"))
                return
            self._load_route(token, str(chat_id), name, lang)
            return
        if action == "follow":
            self.app.patch_control({"behaviors": {"route": {"enabled": True}}})
            send_message(token, str(chat_id), T(lang, "follow_on"),
                         reply_markup=build_status_keyboard(lang))
            return
        if action == "stoproute":
            self.app.patch_control({"behaviors": {"route": {"enabled": False}}})
            send_message(token, str(chat_id), T(lang, "follow_off"),
                         reply_markup=build_status_keyboard(lang))
            return
        if action == "where":
            self._send_where(token, str(chat_id), lang)
            return
        if action == "counters":
            self._send_counters(token, str(chat_id), lang)
            return
        if action == "deaths":
            self._send_deaths(token, str(chat_id), lang)
            return
        if action == "players":
            self._send_players(token, str(chat_id), lang)
            return
        if action == "log":
            self._send_log(token, str(chat_id), lang, args)
            return
        if action == "teleport":
            self._do_teleport(token, str(chat_id), lang, args)
            return
        if action == "behavior":
            name = (args[0].lower() if args else "")
            val = parse_onoff(args[1]) if len(args) > 1 else None
            if name not in VALID_BEHAVIORS or val is None:
                send_message(token, str(chat_id), T(lang, "behavior_usage"))
                return
            self._set_behavior(token, str(chat_id), lang, name, val)
            return
        if action in BEHAVIOR_SHORTCUTS:
            val = parse_onoff(args[0]) if args else None
            if val is None:
                send_message(token, str(chat_id), T(lang, "behavior_usage"))
                return
            self._set_behavior(token, str(chat_id), lang, action, val)
            return
        if action == "catchall":
            val = parse_onoff(args[0]) if args else None
            if val is None:
                send_message(token, str(chat_id), T(lang, "behavior_usage"))
                return
            self.app.patch_control({"capture": {"catch_all": val}})
            send_message(token, str(chat_id),
                         T(lang, "catchall_set").format(state=T(lang, "on" if val else "off")))
            return
        if action == "humanizer":
            profile = (args[0].lower() if args else "")
            if profile not in ("light", "normal", "aggressive"):
                send_message(token, str(chat_id), T(lang, "humanizer_usage"))
                return
            self.app.patch_config({"settings": {"humanizer": {"profile": profile}}})
            self.app.patch_control({"humanizer": {"profile": profile}})
            send_message(token, str(chat_id), T(lang, "humanizer_set").format(profile=profile))
            return
        if action == "screenshot":
            self._send_screenshot(token, str(chat_id), lang)
            return
        if action == "attach":
            self._do_attach(token, str(chat_id), lang)
            return
        if action == "fly":
            self._do_fly(token, str(chat_id), lang)
            return
        if action == "flyup":
            self._do_fly_key(token, str(chat_id), lang, "flyup")
            return
        if action == "flydown":
            self._do_fly_key(token, str(chat_id), lang, "flydown")
            return
        if action in ("stop", "panic") and cmds.get("confirm_destructive", True):
            key = "confirm_stop" if action == "stop" else "confirm_panic"
            send_message(token, str(chat_id), T(lang, key),
                         reply_markup=build_confirm_keyboard(lang, action))
            return
        self._do_action(token, str(chat_id), action, lang)

    def _handle_callback(self, token: str, chat_id, data: str, cmds: dict) -> None:
        if data.startswith("cmd:"):
            _, lang, action = data.split(":", 2)
            self._do_action(token, str(chat_id), action, lang)
        elif data.startswith("confirm:"):
            _, lang, action, ans = data.split(":", 3)
            if ans == "yes":
                self._do_action(token, str(chat_id), action, lang)
            else:
                send_message(token, str(chat_id), T(lang, "cancelled"))
        elif data.startswith("route:"):
            rest = data[len("route:"):]
            lang, _, name = rest.partition(":")
            self._load_route(token, str(chat_id), name, lang)

    # --- rutas guardadas ---
    def _route_key(self, routes: dict, name: str):
        if name in routes:
            return name
        low = name.lower()
        for key in routes:
            if str(key).lower() == low:
                return key
        return None

    def _load_route(self, token: str, chat_id, name: str, lang: str) -> None:
        routes = self.app.routes() or {}
        if not routes:
            send_message(token, chat_id, T(lang, "no_routes"))
            return
        key = self._route_key(routes, name)
        if not key:
            send_message(token, chat_id, T(lang, "route_not_found").format(name=name))
            return
        r = routes.get(key) or {}
        self.app.save_route({
            "waypoints": r.get("waypoints", []),
            "loop": bool(r.get("loop", True)),
            "ping_pong": bool(r.get("ping_pong", False)),
            "enabled": True,
            "pokemon": r.get("pokemon", ""),
            "pokemon_slot": r.get("pokemon_slot"),
        })
        send_message(token, chat_id,
                     T(lang, "route_loaded").format(name=key, n=len(r.get("waypoints", []))),
                     reply_markup=build_status_keyboard(lang))

    def _list_routes(self, token: str, chat_id, lang: str) -> None:
        routes = self.app.routes() or {}
        if not routes:
            send_message(token, chat_id, T(lang, "no_routes"))
            return
        lines = [T(lang, "routes_title")]
        rows = []
        for name in sorted(routes):
            n = len((routes[name] or {}).get("waypoints", []))
            lines.append(f"• {name} ({n})")
            if len(rows) < 12:
                rows.append([{"text": f"▶ {name}", "callback_data": f"route:{lang}:{name}"}])
        kb = json.dumps({"inline_keyboard": rows}) if rows else None
        send_message(token, chat_id, "\n".join(lines), reply_markup=kb)

    # --- info / navegacion ---
    def _send_where(self, token: str, chat_id, lang: str) -> None:
        st = self.app.state()
        txt = (f"{T(lang, 'where_title')}\n"
               f"• {st.get('name') or '?'}\n"
               f"• ({st.get('x')},{st.get('y')},{st.get('z')})\n"
               f"• HP {st.get('hp')}/{st.get('maxhp')} ({st.get('hppct')}%)\n"
               f"• {T(lang, 'level')} {st.get('level')}")
        send_message(token, chat_id, txt)

    def _send_counters(self, token: str, chat_id, lang: str) -> None:
        counters = (self.app.state().get("counters") or {})
        lines = [T(lang, "counters_title")]
        if counters:
            for key, value in sorted(counters.items()):
                lines.append(f"• {key}: {value}")
        else:
            lines.append(T(lang, "none"))
        send_message(token, chat_id, "\n".join(lines))

    def _send_deaths(self, token: str, chat_id, lang: str) -> None:
        recs = (self.app.deaths(10) or {}).get("deaths") or []
        lines = [T(lang, "deaths_title")]
        if recs:
            for r in recs[-10:]:
                sig = ",".join(r.get("signals") or [])
                lines.append(f"• #{r.get('index')} [{sig}] {T(lang, 'level')} {r.get('level')} "
                             f"{r.get('pos')}")
        else:
            lines.append(T(lang, "none"))
        send_message(token, chat_id, "\n".join(lines))

    def _send_players(self, token: str, chat_id, lang: str) -> None:
        players = (self.app.players() or {}).get("players") or []
        lines = [T(lang, "players_title")]
        if players:
            for p in players[:15]:
                lines.append(f"• {p.get('name')} ({p.get('dist')} {T(lang, 'dist')})")
        else:
            lines.append(T(lang, "none"))
        send_message(token, chat_id, "\n".join(lines))

    def _send_log(self, token: str, chat_id, lang: str, args) -> None:
        n = 15
        if args:
            try:
                n = max(1, min(50, int(args[0])))
            except (ValueError, TypeError):
                n = 15
        lines = (self.app.log_tail(n) or {}).get("lines") or []
        txt = T(lang, "log_title") + "\n" + ("\n".join(lines[-n:]) or T(lang, "none"))
        send_message(token, chat_id, txt[:3800])

    def _do_teleport(self, token: str, chat_id, lang: str, args) -> None:
        dest = " ".join(args).strip()
        if not dest:
            send_message(token, chat_id, T(lang, "teleport_usage"))
            return
        cfg = getattr(self.app, "cfg", {}) or {}
        tcfg = (((cfg.get("behaviors", {}) or {}).get("recovery", {}) or {})
                .get("teleport", {}) or {})
        slot = tcfg.get("pokemon_slot")
        cmd = f"teleport {int(slot)} {dest}" if slot else f"teleport {dest}"
        res = self.app.command(cmd)
        if res.get("ok"):
            send_message(token, chat_id, T(lang, "teleport_sent").format(dest=dest))
        else:
            send_message(token, chat_id, "⚠ " + str(res.get("error", "?")))

    # --- extras ---
    def _set_behavior(self, token: str, chat_id, lang: str, name: str, val: bool) -> None:
        self.app.patch_control({"behaviors": {name: {"enabled": bool(val)}}})
        send_message(token, chat_id,
                     T(lang, "behavior_set").format(name=name,
                                                    state=T(lang, "on" if val else "off")))

    def _send_screenshot(self, token: str, chat_id, lang: str) -> None:
        pid = None
        try:
            pid = self.app.client_pid()
        except Exception:
            pid = None
        data = capture_screen(pid)
        if not data:
            send_message(token, chat_id, T(lang, "screenshot_fail"))
            return
        ok, err = send_photo(token, chat_id, data, caption=T(lang, "screenshot_caption"))
        if not ok:
            send_message(token, chat_id, "⚠ " + str(err))

    def _do_attach(self, token: str, chat_id, lang: str) -> None:
        try:
            res = self.app.attach_client()
        except Exception as exc:
            send_message(token, chat_id, T(lang, "attach_fail").format(error=exc))
            return
        if res.get("ok"):
            send_message(token, chat_id, T(lang, "attach_ok"))
        else:
            send_message(token, chat_id,
                         T(lang, "attach_fail").format(error=res.get("error", "?")))

    def _fly_slot(self):
        cfg = getattr(self.app, "cfg", {}) or {}
        fcfg = (((cfg.get("behaviors", {}) or {}).get("recovery", {}) or {})
                .get("fly", {}) or {})
        return fcfg.get("pokemon_slot")

    def _do_fly(self, token: str, chat_id, lang: str) -> None:
        slot = self._fly_slot()
        cmd = f"fly {int(slot)}" if slot else "fly"
        res = self.app.command(cmd)
        if res.get("ok"):
            send_message(token, chat_id,
                         T(lang, "fly_sent").format(slot=slot if slot else "-"))
        else:
            send_message(token, chat_id,
                         T(lang, "fly_fail").format(error=res.get("error", "?")))

    def _do_fly_key(self, token: str, chat_id, lang: str, key: str) -> None:
        res = self.app.command(key)
        if res.get("ok"):
            send_message(token, chat_id, T(lang, key + "_sent"))
        else:
            send_message(token, chat_id,
                         T(lang, "fly_fail").format(error=res.get("error", "?")))

    def _do_action(self, token: str, chat_id, action: str, lang: str) -> None:
        app = self.app
        text = ""
        if action == "start":
            res = app.start_bot()
            if res.get("ok"):
                text = T(lang, "started").format(pid=res.get("pid", "?"))
            else:
                text = T(lang, "already")
        elif action == "stop":
            res = app.stop_bot()
            text = T(lang, "stopped") if res.get("stopped", True) else T(lang, "not_running")
        elif action == "pause":
            app.patch_control({"paused": True})
            text = T(lang, "paused")
        elif action == "resume":
            app.patch_control({"paused": False})
            text = T(lang, "resumed")
        elif action == "panic":
            app.stop_bot()
            app.patch_control({"paused": True})
            text = T(lang, "panic")
        else:
            return
        send_message(token, chat_id, text, reply_markup=build_status_keyboard(lang))
