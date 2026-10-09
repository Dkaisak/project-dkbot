"""Servidor de licencias (FastAPI + SQLite).

Endpoints:
- POST /api/activate   {key, machine_id, version}  -> registra el dispositivo (max N)
- POST /api/verify     {key, machine_id, version}  -> valida (registra si hay hueco)
- POST /api/deactivate {key, machine_id}           -> libera un dispositivo
- POST /api/webhook/{gateway}                       -> alta/renovacion automatica al pagar
- Admin (cabecera X-Admin-Token): crear/listar/extender/revocar

Variables de entorno (ver config.example.env):
- LICENSE_SECRET   secreto compartido con el bot (HMAC)
- ADMIN_TOKEN      token de las rutas /api/admin
- DB_PATH          ruta del SQLite (def. ./licenses.db)
- PLANS            JSON {plan: dias} (def. {"7d":7,"15d":15,"30d":30})
- VARIANT_PLANS    JSON {variant_id: plan} para el webhook
- WEBHOOK_SECRET   secreto de firma del gateway de pago
- SMTP_*           (opcional) para enviar la clave por email

Arranque:  uvicorn main:app --host 0.0.0.0 --port 8080   (ver DEPLOY.md)
"""
from __future__ import annotations

import json
import os
import smtplib
import time
from email.message import EmailMessage

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from . import auth, db

SECRET = os.environ.get("LICENSE_SECRET", "")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "licenses.db"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

try:
    PLANS = json.loads(os.environ.get("PLANS") or '{"7d":7,"15d":15,"30d":30}')
except ValueError:
    PLANS = {"7d": 7, "15d": 15, "30d": 30}
try:
    VARIANT_PLANS = json.loads(os.environ.get("VARIANT_PLANS") or "{}")
except ValueError:
    VARIANT_PLANS = {}

app = FastAPI(title="ShinyBot licenses", version="1.0")


@app.on_event("startup")
def _startup() -> None:
    db.init(DB_PATH)


def _conn():
    return db.connect(DB_PATH)


def _ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def _check(conn, key: str, machine_id: str, ip: str, register: bool) -> dict:
    lic = db.get_license(conn, key)
    if lic is None:
        return {"valid": False, "message": "clave no encontrada"}
    if not int(lic["active"]):
        return {"valid": False, "plan": lic["plan"], "expires_at": lic["expires_at"],
                "message": "licencia revocada"}
    if float(lic["expires_at"]) <= time.time():
        return {"valid": False, "plan": lic["plan"], "expires_at": lic["expires_at"],
                "message": "licencia expirada"}
    if register:
        if not db.register_device(conn, key, machine_id, ip):
            return {"valid": False, "plan": lic["plan"], "expires_at": lic["expires_at"],
                    "message": "limite de dispositivos alcanzado (%s)" % lic["max_devices"]}
    elif not db.is_registered(conn, key, machine_id):
        return {"valid": False, "plan": lic["plan"], "expires_at": lic["expires_at"],
                "message": "dispositivo no activado"}
    used = len(db.activations(conn, key))
    return {"valid": True, "plan": lic["plan"], "days": lic["days"],
            "expires_at": lic["expires_at"], "devices_used": used,
            "max_devices": lic["max_devices"], "message": ""}


def _signed(payload: dict) -> JSONResponse:
    payload = dict(payload)
    payload.setdefault("server_time", time.time())
    payload["sig"] = auth.sign_payload(SECRET, payload)
    return JSONResponse(payload)


async def _body(request: Request) -> dict:
    raw = await request.body()
    if not auth.verify_raw(SECRET, raw, request.headers.get("x-sig", "")):
        raise HTTPException(status_code=401, detail="firma invalida")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="json invalido")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="json invalido")
    return data


def _require_admin(token: str) -> None:
    if not auth.check_admin(ADMIN_TOKEN, token):
        raise HTTPException(status_code=401, detail="admin no autorizado")


@app.get("/health")
def health() -> dict:
    return {"ok": True, "time": time.time(), "plans": PLANS}


@app.post("/api/activate")
async def activate(request: Request) -> JSONResponse:
    data = await _body(request)
    key = str(data.get("key", "")).strip().upper()
    mid = str(data.get("machine_id", "")).strip()
    if not key or not mid:
        return _signed({"valid": False, "message": "faltan key o machine_id"})
    conn = _conn()
    try:
        return _signed(_check(conn, key, mid, _ip(request), register=True))
    finally:
        conn.close()


@app.post("/api/verify")
async def verify(request: Request) -> JSONResponse:
    data = await _body(request)
    key = str(data.get("key", "")).strip().upper()
    mid = str(data.get("machine_id", "")).strip()
    if not key or not mid:
        return _signed({"valid": False, "message": "faltan key o machine_id"})
    conn = _conn()
    try:
        return _signed(_check(conn, key, mid, _ip(request), register=True))
    finally:
        conn.close()


@app.post("/api/deactivate")
async def deactivate(request: Request) -> JSONResponse:
    data = await _body(request)
    key = str(data.get("key", "")).strip().upper()
    mid = str(data.get("machine_id", "")).strip()
    conn = _conn()
    try:
        ok = db.unregister_device(conn, key, mid)
        return _signed({"ok": ok, "message": "" if ok else "dispositivo no encontrado"})
    finally:
        conn.close()


# --- webhook de la pasarela de pago (LemonSqueezy) ---
def _send_email(to: str, key: str, plan: str, days: int) -> None:
    host = os.environ.get("SMTP_HOST", "")
    if not host or not to:
        return
    try:
        msg = EmailMessage()
        msg["Subject"] = "Tu licencia de ShinyBot"
        msg["From"] = os.environ.get("SMTP_FROM", os.environ.get("SMTP_USER", ""))
        msg["To"] = to
        msg.set_content(
            "Gracias por tu compra.\n\n"
            f"Tu licencia de ShinyBot ({plan}, {days} dias):\n\n    {key}\n\n"
            "Pegala en la pestana Licencia de la app.")
        port = int(os.environ.get("SMTP_PORT", "587") or 587)
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls()
            user = os.environ.get("SMTP_USER", "")
            if user:
                s.login(user, os.environ.get("SMTP_PASS", ""))
            s.send_message(msg)
    except Exception:
        pass


@app.post("/api/webhook/{gateway}")
async def webhook(gateway: str, request: Request) -> JSONResponse:
    raw = await request.body()
    sig = request.headers.get("x-signature", "")
    if WEBHOOK_SECRET and not auth.verify_raw(WEBHOOK_SECRET, raw, sig):
        raise HTTPException(status_code=401, detail="firma de webhook invalida")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="json invalido")

    email = ""
    variant = ""
    event = ""
    # LemonSqueezy
    meta = data.get("meta") or {}
    event = str(meta.get("event_name", "") or "")
    attrs = (data.get("data") or {}).get("attributes") or {}
    email = str(attrs.get("user_email", "") or "")
    item = attrs.get("first_order_item") or {}
    variant = str(item.get("variant_id", "") or "")

    if event and event not in ("order_created", "subscription_created",
                               "subscription_payment_success", "order_refunded"):
        return {"ok": True, "ignored": event}

    plan = VARIANT_PLANS.get(variant, "")
    if not plan or plan not in PLANS:
        return {"ok": False, "error": f"variant {variant!r} sin plan mapeado"}
    days = int(PLANS[plan])

    conn = _conn()
    try:
        # renovacion: si ya existe una clave para el email con ese plan, se extiende
        existing = None
        if email:
            for lic in db.list_licenses(conn):
                if lic["email"] == email and lic["plan"] == plan:
                    existing = lic["key"]
                    break
        if existing:
            db.extend_license(conn, existing, days)
            key = existing
        else:
            key = db.create_license(conn, plan, days, email=email, notes=gateway)
    finally:
        conn.close()
    _send_email(email, key, plan, days)
    return {"ok": True, "key": key, "plan": plan, "days": days}


# --- admin ---
def _admin_conn(x_admin_token: str):
    _require_admin(x_admin_token)
    return _conn()


@app.get("/api/admin/licenses")
def admin_list(x_admin_token: str = Header(default="")) -> dict:
    conn = _admin_conn(x_admin_token)
    try:
        return {"licenses": db.list_licenses(conn), "plans": PLANS}
    finally:
        conn.close()


@app.post("/api/admin/licenses")
async def admin_create(request: Request, x_admin_token: str = Header(default="")) -> dict:
    _require_admin(x_admin_token)
    data = await request.json()
    plan = str(data.get("plan", "30d"))
    if plan not in PLANS:
        raise HTTPException(status_code=400, detail="plan invalido")
    conn = _conn()
    try:
        key = db.create_license(conn, plan, int(PLANS[plan]),
                                max_devices=int(data.get("max_devices", 3) or 3),
                                email=str(data.get("email", "") or ""),
                                notes=str(data.get("notes", "") or ""))
        return {"ok": True, "key": key, "plan": plan, "days": PLANS[plan]}
    finally:
        conn.close()


@app.post("/api/admin/licenses/{key}/extend")
async def admin_extend(key: str, request: Request, x_admin_token: str = Header(default="")) -> dict:
    _require_admin(x_admin_token)
    data = await request.json()
    days = int(data.get("days", 30) or 30)
    conn = _conn()
    try:
        ok = db.extend_license(conn, key.upper(), days)
        return {"ok": ok}
    finally:
        conn.close()


@app.post("/api/admin/licenses/{key}/revoke")
def admin_revoke(key: str, x_admin_token: str = Header(default="")) -> dict:
    conn = _admin_conn(x_admin_token)
    try:
        return {"ok": db.set_active(conn, key.upper(), False)}
    finally:
        conn.close()


@app.post("/api/admin/licenses/{key}/restore")
def admin_restore(key: str, x_admin_token: str = Header(default="")) -> dict:
    conn = _admin_conn(x_admin_token)
    try:
        return {"ok": db.set_active(conn, key.upper(), True)}
    finally:
        conn.close()


@app.delete("/api/admin/licenses/{key}")
def admin_delete(key: str, x_admin_token: str = Header(default="")) -> dict:
    conn = _admin_conn(x_admin_token)
    try:
        return {"ok": db.delete_license(conn, key.upper())}
    finally:
        conn.close()


@app.get("/api/admin/licenses/{key}/devices")
def admin_devices(key: str, x_admin_token: str = Header(default="")) -> dict:
    conn = _admin_conn(x_admin_token)
    try:
        return {"devices": db.activations(conn, key.upper())}
    finally:
        conn.close()


@app.delete("/api/admin/licenses/{key}/devices/{machine_id}")
def admin_del_device(key: str, machine_id: str, x_admin_token: str = Header(default="")) -> dict:
    conn = _admin_conn(x_admin_token)
    try:
        return {"ok": db.unregister_device(conn, key.upper(), machine_id)}
    finally:
        conn.close()
