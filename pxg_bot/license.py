"""Verificacion de licencia (servidor propio, HMAC). Sin dependencias (stdlib).

- `machine_id()`: huella de la maquina (Windows: `MachineGuid` + nº de volumen;
  en otros SO: hostname+arquitectura), hasheada. No identifica a la persona.
- Al arrancar: valida **online**; si no hay red, usa la **cache** con **gracia
  offline** (def. 12 h).
- La cache (`pxg_license.json` junto al ejecutable) va **firmada** (HMAC) para
  que no se pueda editar a mano sin el secreto.

El secreto (`_BUILTIN_SECRET`) lo rellena el vendedor y queda **ofuscado** con
PyArmor. Es un deterrente, no un candado: el bloqueo real es la validacion
online recurrente.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import time
import urllib.request

_BUILTIN_SERVER = "https://lic.shinybot.online"  # URL del servidor de licencias
_BUILTIN_SECRET = "2380e4d9852fdf5e57853388c4b3e086744cec3b0608b5d26622a220bc2d87fe"  # LICENSE_SECRET (mismo que el servidor)
_DEFAULT_GRACE_HOURS = 12.0
_DEFAULT_RECHECK_HOURS = 6.0


def _canon(data: dict) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _sign(secret: str, data: dict) -> str:
    return hmac.new(secret.encode("utf-8"), _canon(data), hashlib.sha256).hexdigest()


def _win_machine_guid() -> str:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Cryptography") as k:
            return str(winreg.QueryValueEx(k, "MachineGuid")[0])
    except Exception:
        return ""


def _win_volume_serial() -> str:
    try:
        import ctypes
        serial = ctypes.c_uint(0)
        ctypes.windll.kernel32.GetVolumeInformationW(
            "C:\\", None, 0, ctypes.byref(serial), None, None, None, 0)
        return str(serial.value)
    except Exception:
        return ""


def machine_id() -> str:
    parts = []
    if os.name == "nt":
        parts.append(_win_machine_guid())
        parts.append(_win_volume_serial())
    else:
        parts.append(platform.node())
    parts.append(platform.machine())
    raw = "|".join(p for p in parts if p)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class License:
    def __init__(self, cfg: dict, base_dir: str):
        lc = (cfg or {}).get("license", {}) or {}
        self.key = str(lc.get("key", "") or "").strip().upper()
        self.server_url = (_BUILTIN_SERVER or str(lc.get("server_url", "") or "")).rstrip("/")
        self.secret = _BUILTIN_SECRET or str(lc.get("secret", "") or "")
        self.grace_hours = float(lc.get("grace_hours", _DEFAULT_GRACE_HOURS) or _DEFAULT_GRACE_HOURS)
        self.recheck_hours = float(lc.get("recheck_hours", _DEFAULT_RECHECK_HOURS) or _DEFAULT_RECHECK_HOURS)
        self.version = str(lc.get("version", "") or "")
        self.base_dir = base_dir or "."
        self.path = os.path.join(self.base_dir, "pxg_license.json")
        self.mid = machine_id()
        self.state: dict = {}
        self.error = ""
        self.online = False
        self._load_cache()

    # --- cache local (firmada) ---
    def _load_cache(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        sig = data.pop("sig", "")
        if self.secret and not hmac.compare_digest(_sign(self.secret, data), sig):
            return  # cache manipulada: se ignora
        self.state = data

    def _save_cache(self, data: dict) -> None:
        try:
            payload = dict(data)
            payload["sig"] = _sign(self.secret, payload)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            pass

    # --- peticion firmada ---
    def _request(self, path: str, body: dict, timeout: float = 15.0) -> dict:
        raw = json.dumps(body).encode("utf-8")
        sig = hmac.new(self.secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
        req = urllib.request.Request(
            self.server_url + path, data=raw, method="POST",
            headers={"Content-Type": "application/json", "X-Sig": sig})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        s = data.pop("sig", "")
        if self.secret and not hmac.compare_digest(_sign(self.secret, data), s):
            raise ValueError("firma de respuesta invalida")
        return data

    # --- verificacion ---
    def enforced(self) -> bool:
        """True si hay un servidor configurado (producto con licencias). En
        desarrollo (sin server) no se aplica."""
        return bool(self.server_url)

    def check(self, force: bool = False) -> bool:
        if not self.server_url:
            return True
        if not self.key:
            self.error = "sin clave de licencia"
            return self._cache_ok()
        try:
            data = self._request("/api/verify", {"key": self.key, "machine_id": self.mid,
                                                 "version": self.version})
            self.online = True
            self.error = str(data.get("message", "") or "")
            exp = float(data.get("expires_at", 0) or 0)
            if data.get("valid"):
                self.state = {"key": self.key, "plan": data.get("plan", ""),
                              "expires_at": exp, "machine_id": self.mid,
                              "last_ok": time.time()}
                self._save_cache(self.state)
                return True
            self._save_cache({"key": self.key, "plan": data.get("plan", ""),
                              "expires_at": exp, "machine_id": self.mid, "last_ok": 0.0})
            return False
        except Exception as exc:
            self.online = False
            self.error = "offline (%s: %s)" % (type(exc).__name__, exc)
            return self._cache_ok()

    def _cache_ok(self) -> bool:
        exp = float(self.state.get("expires_at", 0) or 0)
        last = float(self.state.get("last_ok", 0) or 0)
        if exp <= 0 or last <= 0:
            return False
        now = time.time()
        if exp <= now:
            self.error = "licencia expirada"
            return False
        if now - last > self.grace_hours * 3600:
            self.error = "gracia offline agotada"
            return False
        return True

    def due(self) -> bool:
        last = float(self.state.get("last_ok", 0) or 0)
        return (time.time() - last) > self.recheck_hours * 3600

    def remaining_days(self):
        exp = float(self.state.get("expires_at", 0) or 0)
        if exp <= 0:
            return None
        return max(0.0, (exp - time.time()) / 86400.0)

    def status(self) -> dict:
        return {"key": self.key, "machine_id": self.mid, "online": self.online,
                "plan": self.state.get("plan", ""),
                "expires_at": self.state.get("expires_at"),
                "remaining_days": (round(self.remaining_days(), 2)
                                   if self.remaining_days() is not None else None),
                "error": self.error}
