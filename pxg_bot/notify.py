"""Avisos por Telegram.

Envia mensajes al bot de Telegram cuando ocurren eventos del juego: muerte del
personaje, otro jugador cerca, shiny encontrado, sin revives y desconexion.

- Sin dependencias: usa `urllib.request` de la stdlib contra la Bot API
  (`https://api.telegram.org/bot<token>/sendMessage`).
- Los envios van a un hilo con cola: el bucle del bot (0.05 s) nunca se bloquea
  por la red, y un fallo de Telegram jamas tumba el bot.
- Dedupe por clave + cooldown por evento: no spamea el mismo aviso.
"""
from __future__ import annotations

import json
import queue
import threading
import time
import urllib.parse
import urllib.request

from .models import CreatureKind

DEFAULT_TELEGRAM = {
    "enabled": False,
    "bot_token": "",
    "chat_id": "",
    "parse_mode": "HTML",
    "player_range": 7,
    "events": {
        "death": True,
        "player": True,
        "shiny": True,
        "capture": True,
        "revives_out": True,
        "disconnect": True,
    },
    "cooldowns": {
        "death": 15.0,
        "player": 30.0,
        "shiny": 10.0,
        "capture": 0.0,
        "revives_out": 60.0,
        "disconnect": 30.0,
    },
    "commands": {
        "enabled": False,
        "allowed_chat_ids": [],
        "allowed_user_ids": [],
        "language": "es",
        "poll_timeout": 25,
        "confirm_destructive": True,
    },
}

# (clave, etiqueta) para la GUI.
EVENTS = [
    ("death", "Muerte del personaje"),
    ("player", "Otro jugador cerca"),
    ("shiny", "Shiny encontrado"),
    ("capture", "Captura de Pokemon"),
    ("revives_out", "Sin revives"),
    ("disconnect", "Desconexion"),
]


def send_message(token: str, chat_id: str, text: str,
                 parse_mode: str = "HTML", timeout: float = 10.0,
                 reply_markup=None):
    """Envia un mensaje por la Bot API. Devuelve (ok, error)."""
    if not token or not chat_id:
        return False, "falta bot_token o chat_id"
    url = "https://api.telegram.org/bot%s/sendMessage" % token
    payload = {
        "chat_id": str(chat_id),
        "text": text,
        "disable_web_page_preview": "true",
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = (reply_markup if isinstance(reply_markup, str)
                                   else json.dumps(reply_markup))
    data = urllib.parse.urlencode(payload).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "ignore")
    except Exception as exc:  # red, timeout, HTTP, etc.
        return False, "%s: %s" % (type(exc).__name__, exc)
    try:
        parsed = json.loads(body)
    except ValueError:
        return True, ""
    if parsed.get("ok"):
        return True, ""
    return False, str(parsed.get("description") or "error")


def send_photo(token: str, chat_id: str, photo: bytes, filename: str = "screen.png",
               caption: str = "", parse_mode: str = "HTML", timeout: float = 30.0):
    """Envia una foto (multipart) por la Bot API. Devuelve (ok, error)."""
    if not token or not chat_id:
        return False, "falta bot_token o chat_id"
    if not photo:
        return False, "sin imagen"
    boundary = "----shinybot%d" % int(time.time() * 1000)
    parts = []

    def field(name: str, value: str) -> None:
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                      % (boundary, name, value)).encode("utf-8"))

    field("chat_id", str(chat_id))
    if caption:
        field("caption", caption)
    if parse_mode:
        field("parse_mode", parse_mode)
    head = ("--%s\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"%s\"\r\n"
            "Content-Type: image/png\r\n\r\n" % (boundary, filename)).encode("utf-8")
    body = b"".join(parts) + head + photo + ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    url = "https://api.telegram.org/bot%s/sendPhoto" % token
    try:
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "ignore")
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)
    try:
        parsed = json.loads(raw)
    except ValueError:
        return True, ""
    if parsed.get("ok"):
        return True, ""
    return False, str(parsed.get("description") or "error")


class Notifier:
    """Avisos por un canal (Telegram o WhatsApp). La deteccion de eventos es
    comun; solo cambia el envio (`_send`)."""

    def __init__(self, cfg: dict, section: str = "telegram"):
        self.section = section
        self._lock = threading.Lock()
        self._q: "queue.Queue" = queue.Queue()
        self._thread = None
        self._stop = threading.Event()
        self.sent = 0
        self.error = ""
        self.last_event = ""
        self.last_at = 0.0
        self._last: dict = {}       # dedupe: clave -> ts
        self._prev: dict = {}       # muestra previa (deteccion de muerte)
        self._shiny_active: set = set()   # shinies ya avisados (en pantalla)
        self._connected = None
        self.cfg: dict = {}
        self.events: dict = {}
        self.cooldowns: dict = {}
        self._raw: dict = {}
        self.set_config((cfg or {}).get(section, {}) or {})
        if self.enabled():
            self._ensure_thread()

    # --- config ---
    def set_config(self, data: dict) -> None:
        data = data or {}
        with self._lock:
            self._raw = dict(data)
            self.cfg = {
                "enabled": bool(data.get("enabled", False)),
                "player_range": int(data.get("player_range", 7) or 7),
            }
            self.events = dict(data.get("events") or {})
            self.cooldowns = dict(data.get("cooldowns") or {})
        if self.enabled():
            self._ensure_thread()

    def _creds_ok(self) -> bool:
        if self.section == "whatsapp":
            from .whatsapp import whatsapp_configured

            return whatsapp_configured(self._raw)
        return bool(self._raw.get("bot_token")) and bool(self._raw.get("chat_id"))

    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled")) and self._creds_ok()

    def _send(self, text: str):
        if self.section == "whatsapp":
            from .whatsapp import send_whatsapp, to_plain

            return send_whatsapp(self._raw, to_plain(text))
        return send_message(str(self._raw.get("bot_token", "") or ""),
                            str(self._raw.get("chat_id", "") or ""),
                            text,
                            str(self._raw.get("parse_mode", "HTML") or ""))

    def _event_on(self, name: str) -> bool:
        return bool(self.events.get(name, True))

    def _cd(self, name: str, default: float) -> float:
        try:
            return float(self.cooldowns.get(name, default))
        except (TypeError, ValueError):
            return default

    # --- hilo de envio ---
    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=self.section, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                text = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if text is None:
                break
            ok, err = self._send(text)
            with self._lock:
                if ok:
                    self.sent += 1
                    self.error = ""
                else:
                    self.error = err

    def close(self, timeout: float = 5.0) -> None:
        """Drena la cola y para el hilo (con tope de tiempo).

        Se envia el centinela primero y se espera a que el worker procese lo que
        quede; solo despues se marca `_stop`. Asi ningun aviso se pierde al
        detener el bot (p. ej. el de 'sin revives', que va seguido del cierre).
        """
        try:
            self._q.put_nowait(None)
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._stop.set()

    # --- emision ---
    def _emit(self, event: str, text: str) -> None:
        if not self._event_on(event) or not self.enabled():
            return
        self._ensure_thread()
        try:
            self._q.put_nowait(text)
        except Exception:
            return
        self.last_event = event
        self.last_at = time.time()

    def _cd_ok(self, key: str, cooldown: float, now: float) -> bool:
        if now - self._last.get(key, 0.0) < cooldown:
            return False
        self._last[key] = now
        return True

    def notify(self, event: str, text: str) -> None:
        """Emite un aviso concreto (p. ej. desde el bot)."""
        try:
            self._emit(event, text)
        except Exception as exc:
            self.error = "%s: %s" % (type(exc).__name__, exc)

    def capture(self, name: str = "", count: int = 1) -> None:
        """Avisa de una captura confirmada (nombre de especie y cantidad)."""
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = 1
        what = str(name or "").strip()
        if what:
            text = "🎯 <b>¡Has capturado un Pokémon!</b> (%s)" % what
        else:
            text = "🎯 <b>¡Has capturado un Pokémon!</b>"
        if count > 1:
            text += " ×%d" % count
        self.notify("capture", text)

    def test(self) -> dict:
        label = "WhatsApp" if self.section == "whatsapp" else "Telegram"
        ok, err = self._send("✅ Prueba de ShinyBot: los avisos por %s funcionan." % label)
        if ok:
            self.sent += 1
        return {"ok": ok, "error": err}

    # --- observacion del estado ---
    def observe(self, state, bb=None) -> None:
        try:
            self._observe(state, bb)
        except Exception as exc:  # nunca tumbar el bot por un aviso
            self.error = "%s: %s" % (type(exc).__name__, exc)

    def _observe(self, state, bb) -> None:
        now = time.time()
        self._check_connection(state, now)
        self._check_death(state, now)
        self._check_players(state, now)
        self._check_shinies(state, now)
        self._check_revives(state, bb, now)

    def _who(self, state) -> str:
        p = state.player
        return "%s · (%d,%d,%d) · nivel %d" % (
            p.name or "personaje", p.pos.x, p.pos.y, p.pos.z, p.level)

    def _check_connection(self, state, now: float) -> None:
        connected = bool(getattr(state, "connected", False))
        if self._connected is None:
            self._connected = connected
            return
        if self._connected and not connected:
            if self._cd_ok("disconnect", self._cd("disconnect", 30.0), now):
                self._emit("disconnect", "🔌 <b>Desconectado</b>\n%s" % self._who(state))
        self._connected = connected

    def _check_death(self, state, now: float) -> None:
        p = state.player
        hp = p.hp_pct
        exp = getattr(p, "exp", 0) or 0
        lvl = p.level
        prev = self._prev
        reason = None
        if prev.get("hp", 0) > 0 and hp <= 0:
            reason = "vida a 0"
        elif prev.get("exp") and exp and exp < prev["exp"]:
            reason = "pérdida de XP"
        elif prev.get("level") and lvl and lvl < prev["level"]:
            reason = "bajó de nivel"
        if reason and self._cd_ok("death", self._cd("death", 15.0), now):
            self._emit("death", "💀 <b>Muerto</b> (%s)\n%s" % (reason, self._who(state)))
        self._prev = {"hp": hp, "exp": exp, "level": lvl}

    def _check_players(self, state, now: float) -> None:
        rng = int(self.cfg.get("player_range", 7))
        for c in state.creatures:
            if c.is_self or not (c.is_player or c.kind == CreatureKind.PLAYER):
                continue
            d = state.player.pos.distance(c.pos)
            if d > rng:
                continue
            if self._cd_ok("player:%s" % (c.name or "?"), self._cd("player", 30.0), now):
                self._emit("player", "👤 <b>Jugador</b>: %s a %d tiles\n%s"
                           % (c.name or "?", d, self._who(state)))

    def _check_shinies(self, state, now: float) -> None:
        # Un aviso por criatura mientras siga en pantalla (antes reavisaba cada
        # 10 s por el cooldown, lo que parecia spam). Al desaparecer se rearma
        # para un nuevo encuentro.
        present = set()
        for c in state.creatures:
            if not getattr(c, "shiny", False) or not c.attackable:
                continue
            uid = c.uid or "%s@%d,%d,%d" % (c.name, c.pos.x, c.pos.y, c.pos.z)
            present.add(uid)
            if uid in self._shiny_active:
                continue
            self._shiny_active.add(uid)
            self._emit("shiny", "✨ <b>Shiny</b>: %s en (%d,%d,%d)"
                       % (c.name, c.pos.x, c.pos.y, c.pos.z))
        self._shiny_active &= present

    def _check_revives(self, state, bb, now: float) -> None:
        if bb is None or not bb.notes.get("revives_out_done"):
            return
        if self._cd_ok("revives_out", self._cd("revives_out", 60.0), now):
            self._emit("revives_out",
                       "🚫 <b>Sin revives</b> — logout y bot detenido\n%s" % self._who(state))

    # --- estado ---
    def status(self) -> dict:
        return {
            "channel": self.section,
            "enabled": self.enabled(),
            "configured": self._creds_ok(),
            "active": bool(self.cfg.get("enabled")),
            "sent": self.sent,
            "last_event": self.last_event,
            "last_at": round(self.last_at, 1) if self.last_at else None,
            "error": self.error,
        }


class Notifiers:
    """Fan-out de avisos: reparte cada evento a varios canales (Telegram + WhatsApp).

    La deteccion de eventos corre por canal (barato) y cada uno tiene su propio
    dedupe/cooldown; el bot solo habla con este objeto."""

    def __init__(self, cfg: dict):
        self.by_section = {
            "telegram": Notifier(cfg, "telegram"),
            "whatsapp": Notifier(cfg, "whatsapp"),
        }

    def set_config(self, section: str, data: dict) -> None:
        ch = self.by_section.get(section)
        if ch is not None:
            ch.set_config(data)

    def observe(self, state, bb=None) -> None:
        for ch in self.by_section.values():
            ch.observe(state, bb)

    def capture(self, name: str = "", count: int = 1) -> None:
        for ch in self.by_section.values():
            ch.capture(name, count)

    def notify(self, event: str, text: str) -> None:
        for ch in self.by_section.values():
            ch.notify(event, text)

    def close(self, timeout: float = 5.0) -> None:
        for ch in self.by_section.values():
            ch.close(timeout)

    def status(self) -> dict:
        return {section: ch.status() for section, ch in self.by_section.items()}
