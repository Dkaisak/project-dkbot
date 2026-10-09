"""Firma HMAC del servidor de licencias.

- **bot -> server**: el bot firma el cuerpo de la peticion con el secreto
  compartido (cabecera `X-Sig`). El server lo comprueba.
- **server -> bot**: la respuesta lleva `sig`, un HMAC del payload. El bot lo
  verifica antes de cachear (evita que un cliente edite su cache a mano).

Es un **deterrente**, no un candado: el secreto va dentro del binario (ofuscado
con PyArmor). Para bloquear de verdad esta la validacion online recurrente.
"""
from __future__ import annotations

import hashlib
import hmac
import json


def _canon(data: dict) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def sign_raw(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_raw(secret: str, body: bytes, sig: str) -> bool:
    return bool(secret) and hmac.compare_digest(sign_raw(secret, body), sig or "")


def sign_payload(secret: str, data: dict) -> str:
    return hmac.new(secret.encode("utf-8"), _canon(data), hashlib.sha256).hexdigest()


def verify_payload(secret: str, data: dict, sig: str) -> bool:
    return bool(secret) and hmac.compare_digest(sign_payload(secret, data), sig or "")


def check_admin(token: str, provided: str) -> bool:
    return bool(token) and hmac.compare_digest(token, provided or "")
