"""Avisos por WhatsApp, solo salida y con la stdlib.

Dos proveedores (se elige en la GUI / `config.json` → `whatsapp.provider`):

- **meta** (WhatsApp Cloud API, oficial): `POST
  https://graph.facebook.com/<v>/<phone_number_id>/messages` con
  `Authorization: Bearer <token>`. Necesita un número de negocio en Meta, un
  token y el `phone_number_id`. Solo deja enviar texto libre dentro de la ventana
  de 24 h tras el último mensaje del destinatario (fuera, hay que usar plantillas).
- **callmebot** (gateway no oficial, unidireccional): simple GET con una API key.

Ambos son **unidireccionales** (no reciben) y con límite de frecuencia.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_WHATSAPP = {
    "enabled": False,
    "provider": "meta",          # meta | callmebot
    "phone": "",                 # destinatario (formato internacional, +34...)
    # Meta Cloud API
    "token": "",                 # access token (Bearer)
    "phone_number_id": "",       # id del número emisor en Meta
    "api_version": "v21.0",
    # CallMeBot
    "apikey": "",
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
}

CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"
GRAPH_URL = "https://graph.facebook.com"


def to_plain(text: str) -> str:
    """Convierte el HTML de los avisos a texto/negrita de WhatsApp.

    `<b>x</b>` pasa a `*x*` (negrita de WhatsApp) y se quitan el resto de etiquetas.
    """
    t = re.sub(r"<b>(.*?)</b>", r"*\1*", text or "", flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", "", t)
    return t.strip()


def _digits(phone: str) -> str:
    return re.sub(r"[^\d]", "", phone or "")


def is_meta(provider: str) -> bool:
    return str(provider or "").lower() in ("meta", "meta_cloud", "cloud", "graph")


def whatsapp_configured(cfg: dict) -> bool:
    cfg = cfg or {}
    if is_meta(cfg.get("provider", "meta")):
        return bool(cfg.get("token")) and bool(cfg.get("phone_number_id")) and bool(cfg.get("phone"))
    return bool(cfg.get("phone")) and bool(cfg.get("apikey"))


def send_callmebot(phone: str, text: str, apikey: str, timeout: float = 10.0):
    """Envía un WhatsApp con CallMeBot. Devuelve (ok, error)."""
    if not phone or not apikey:
        return False, "falta phone o apikey"
    url = CALLMEBOT_URL + "?" + urllib.parse.urlencode(
        {"phone": phone, "text": text, "apikey": apikey})
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "ignore")
    except Exception as exc:  # red, timeout, HTTP, etc.
        return False, "%s: %s" % (type(exc).__name__, exc)
    low = (body or "").lower()
    if any(w in low for w in ("error", "not allowed", "invalid", "exceeded",
                              "limit", "blocked", "suspended")):
        return False, (body or "").strip()[:200] or "error de CallMeBot"
    return True, ""


def send_meta(token: str, phone_number_id: str, to: str, text: str,
              api_version: str = "v21.0", timeout: float = 15.0):
    """Envía un WhatsApp con la Cloud API de Meta. Devuelve (ok, error)."""
    if not token or not phone_number_id or not to:
        return False, "falta token, phone_number_id o phone"
    url = "%s/%s/%s/messages" % (GRAPH_URL, api_version or "v21.0", phone_number_id)
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": _digits(to),
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "ignore")
        except Exception:
            pass
        return False, "HTTP %s: %s" % (exc.code, detail[:200])
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)
    try:
        parsed = json.loads(body)
    except ValueError:
        return True, ""
    if parsed.get("error"):
        return False, str(parsed["error"])[:200]
    return True, ""


def send_whatsapp(cfg: dict, text: str):
    """Despacha por el proveedor configurado. Devuelve (ok, error)."""
    cfg = cfg or {}
    if is_meta(cfg.get("provider", "meta")):
        return send_meta(str(cfg.get("token", "") or ""),
                         str(cfg.get("phone_number_id", "") or ""),
                         str(cfg.get("phone", "") or ""),
                         text,
                         str(cfg.get("api_version", "v21.0") or "v21.0"))
    return send_callmebot(str(cfg.get("phone", "") or ""), text,
                          str(cfg.get("apikey", "") or ""))
