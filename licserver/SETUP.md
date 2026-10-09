# Construir el servidor de licencias de ShinyBot en un VPS (guía autocontenida)

> Esta guía es **autocontenida**: incluye todo el código. Pásasela al agente de la
> VPS (o cópiala tal cual). Objetivo: un microservicio **FastAPI + SQLite** que
> valida licencias (planes 7/15/30 días, máx 3 dispositivos) y las crea/renueva
> por **webhook** de la pasarela de pago.

---

## 0. Contexto

El bot `ShinyBot` (en el PC del cliente) llama al servidor para validar su licencia:

- `POST /api/verify {key, machine_id, version}` → `{valid, plan, expires_at, devices_used}`.
- Peticiones firmadas con **HMAC** (`X-Sig`); respuestas firmadas (`sig`).
- La clave la emite el servidor al pagar (webhook) o a mano (CLI admin).

---

## 1. Requisitos

- VPS Linux (1 vCPU / 512 MB basta) con un **dominio** apuntando a su IP.
- **Python 3.10+**, `pip`, `git` (opcional).
- **nginx** + **certbot** (HTTPS/Let's Encrypt) y **systemd**.

---

## 2. Estructura de ficheros

```
/opt/shinybot-licenses/
├── .env                      # secretos y config (NO al repo)
├── requirements.txt
└── licserver/
    ├── __init__.py
    ├── db.py
    ├── auth.py
    ├── main.py
    └── admin.py
```

---

## 3. Crear los ficheros

### `requirements.txt`
```txt
fastapi>=0.110
uvicorn[standard]>=0.29
```

### `.env`
```env
# Genera el secreto:  python -c "import secrets;print(secrets.token_hex(32))"
LICENSE_SECRET=cambia_esto_por_un_secreto_largo_y_aleatorio
ADMIN_TOKEN=cambia_esto_por_un_token_admin
WEBHOOK_SECRET=cambia_esto_por_el_webhook_secret
DB_PATH=/var/lib/shinybot-licenses/licenses.db
PLANS={"7d":7,"15d":15,"30d":30}
VARIANT_PLANS={"123456":"7d","123457":"15d","123458":"30d"}
# (Opcional) envio de la clave por email al comprar:
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASS=
SMTP_FROM=licencias@tudominio.com
```

### `licserver/__init__.py`
```python
"""Servidor de licencias de ShinyBot (FastAPI + SQLite)."""
```

### `licserver/db.py`
```python
"""Base de datos (SQLite) del servidor de licencias."""
from __future__ import annotations

import secrets
import sqlite3
import time
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS licenses (
  key         TEXT PRIMARY KEY,
  plan        TEXT NOT NULL,
  days        INTEGER NOT NULL,
  created_at  REAL NOT NULL,
  expires_at  REAL NOT NULL,
  max_devices INTEGER NOT NULL DEFAULT 3,
  active      INTEGER NOT NULL DEFAULT 1,
  email       TEXT DEFAULT '',
  notes       TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS activations (
  key        TEXT NOT NULL,
  machine_id TEXT NOT NULL,
  first_seen REAL NOT NULL,
  last_seen  REAL NOT NULL,
  ip         TEXT DEFAULT '',
  PRIMARY KEY (key, machine_id)
);
"""

_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init(path: str) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)


def new_key() -> str:
    parts = ["".join(secrets.choice(_ALPHABET) for _ in range(4)) for _ in range(3)]
    return "DK-" + "-".join(parts)


def _row(conn: sqlite3.Connection, key: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM licenses WHERE key = ?", (key,)).fetchone()


def get_license(conn: sqlite3.Connection, key: str) -> Optional[dict]:
    row = _row(conn, key)
    return dict(row) if row is not None else None


def create_license(conn: sqlite3.Connection, plan: str, days: int, *,
                   key: Optional[str] = None, max_devices: int = 3,
                   email: str = "", notes: str = "") -> str:
    key = key or new_key()
    now = time.time()
    with conn:
        conn.execute(
            "INSERT INTO licenses(key, plan, days, created_at, expires_at, "
            "max_devices, active, email, notes) VALUES(?,?,?,?,?,?,1,?,?)",
            (key, plan, days, now, now + days * 86400, max_devices, email, notes))
    return key


def extend_license(conn: sqlite3.Connection, key: str, days: int) -> bool:
    row = _row(conn, key)
    if row is None:
        return False
    base = max(time.time(), float(row["expires_at"]))
    with conn:
        conn.execute("UPDATE licenses SET expires_at = ?, active = 1 WHERE key = ?",
                     (base + days * 86400, key))
    return True


def set_active(conn: sqlite3.Connection, key: str, active: bool) -> bool:
    row = _row(conn, key)
    if row is None:
        return False
    with conn:
        conn.execute("UPDATE licenses SET active = ? WHERE key = ?", (1 if active else 0, key))
    return True


def delete_license(conn: sqlite3.Connection, key: str) -> bool:
    with conn:
        conn.execute("DELETE FROM activations WHERE key = ?", (key,))
        cur = conn.execute("DELETE FROM licenses WHERE key = ?", (key,))
    return cur.rowcount > 0


def list_licenses(conn: sqlite3.Connection) -> list:
    rows = conn.execute("SELECT * FROM licenses ORDER BY created_at DESC").fetchall()
    out = []
    for r in rows:
        n = conn.execute("SELECT COUNT(*) AS n FROM activations WHERE key = ?",
                         (r["key"],)).fetchone()["n"]
        d = dict(r)
        d["devices_used"] = n
        out.append(d)
    return out


def activations(conn: sqlite3.Connection, key: str) -> list:
    rows = conn.execute(
        "SELECT * FROM activations WHERE key = ? ORDER BY last_seen DESC", (key,)).fetchall()
    return [dict(r) for r in rows]


def register_device(conn: sqlite3.Connection, key: str, machine_id: str, ip: str = "") -> bool:
    row = _row(conn, key)
    if row is None:
        return False
    now = time.time()
    with conn:
        cur = conn.execute("SELECT 1 FROM activations WHERE key = ? AND machine_id = ?",
                           (key, machine_id)).fetchone()
        if cur is not None:
            conn.execute("UPDATE activations SET last_seen = ?, ip = ? WHERE key = ? AND machine_id = ?",
                         (now, ip, key, machine_id))
            return True
        used = conn.execute("SELECT COUNT(*) AS n FROM activations WHERE key = ?",
                            (key,)).fetchone()["n"]
        if used >= int(row["max_devices"]):
            return False
        conn.execute("INSERT INTO activations(key, machine_id, first_seen, last_seen, ip) "
                     "VALUES(?,?,?,?,?)", (key, machine_id, now, now, ip))
    return True


def is_registered(conn: sqlite3.Connection, key: str, machine_id: str) -> bool:
    return conn.execute("SELECT 1 FROM activations WHERE key = ? AND machine_id = ?",
                        (key, machine_id)).fetchone() is not None


def unregister_device(conn: sqlite3.Connection, key: str, machine_id: str) -> bool:
    with conn:
        cur = conn.execute("DELETE FROM activations WHERE key = ? AND machine_id = ?",
                           (key, machine_id))
    return cur.rowcount > 0
```

### `licserver/auth.py`
```python
"""Firma HMAC del servidor de licencias."""
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
```

### `licserver/main.py`
```python
"""Servidor de licencias (FastAPI + SQLite)."""
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
        return {"ok": db.extend_license(conn, key.upper(), days)}
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
```

### `licserver/admin.py`
```python
"""CLI de administracion del servidor de licencias (stdlib).

Uso:
  python -m licserver.admin create --plan 30d [--max-devices 3] [--email x@y]
  python -m licserver.admin list
  python -m licserver.admin extend <KEY> --days 30
  python -m licserver.admin revoke <KEY> | restore <KEY> | delete <KEY>
  python -m licserver.admin devices <KEY>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import db

PLANS = json.loads(os.environ.get("PLANS") or '{"7d":7,"15d":15,"30d":30}')


def _connect():
    path = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "licenses.db"))
    db.init(path)
    return db.connect(path)


def _fmt(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return "?"


def cmd_create(args) -> int:
    if args.plan not in PLANS:
        print("plan invalido; usa:", ", ".join(PLANS))
        return 2
    conn = _connect()
    key = db.create_license(conn, args.plan, int(PLANS[args.plan]),
                            key=args.key, max_devices=args.max_devices,
                            email=args.email, notes=args.notes)
    print(key)
    return 0


def cmd_list(args) -> int:
    conn = _connect()
    for lic in db.list_licenses(conn):
        exp = _fmt(lic["expires_at"])
        state = "activa" if lic["active"] else "REVOCADA"
        print(f'{lic["key"]}  {lic["plan"]:>4}  exp={exp}  '
              f'dev={lic["devices_used"]}/{lic["max_devices"]}  {state}  {lic["email"]}')
    return 0


def cmd_extend(args) -> int:
    conn = _connect()
    ok = db.extend_license(conn, args.key.upper(), args.days)
    print("ok" if ok else "clave no encontrada")
    return 0 if ok else 1


def cmd_setactive(args, active: bool) -> int:
    conn = _connect()
    ok = db.set_active(conn, args.key.upper(), active)
    print("ok" if ok else "clave no encontrada")
    return 0 if ok else 1


def cmd_delete(args) -> int:
    conn = _connect()
    print("ok" if db.delete_license(conn, args.key.upper()) else "clave no encontrada")
    return 0


def cmd_devices(args) -> int:
    conn = _connect()
    for d in db.activations(conn, args.key.upper()):
        print(f'{d["machine_id"]}  first={_fmt(d["first_seen"])}  '
              f'last={_fmt(d["last_seen"])}  ip={d["ip"]}')
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="licserver.admin")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create"); c.add_argument("--plan", default="30d")
    c.add_argument("--max-devices", type=int, default=3)
    c.add_argument("--email", default=""); c.add_argument("--notes", default="")
    c.add_argument("--key", default=None); c.set_defaults(func=cmd_create)

    sub.add_parser("list").set_defaults(func=cmd_list)

    e = sub.add_parser("extend"); e.add_argument("key"); e.add_argument("--days", type=int, default=30)
    e.set_defaults(func=cmd_extend)

    r = sub.add_parser("revoke"); r.add_argument("key")
    r.set_defaults(func=lambda a: cmd_setactive(a, False))
    s = sub.add_parser("restore"); s.add_argument("key")
    s.set_defaults(func=lambda a: cmd_setactive(a, True))

    d = sub.add_parser("delete"); d.add_argument("key"); d.set_defaults(func=cmd_delete)
    dv = sub.add_parser("devices"); dv.add_argument("key"); dv.set_defaults(func=cmd_devices)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
```

---

## 4. Puesta en marcha (pasos del agente)

```bash
sudo mkdir -p /opt/shinybot-licenses /var/lib/shinybot-licenses
# copia aqui .env, requirements.txt y licserver/  (los de arriba)
cd /opt/shinybot-licenses
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# prueba manual
set -a; . ./.env; set +a
uvicorn licserver.main:app --host 127.0.0.1 --port 8080
# en otra terminal:  curl -s localhost:8080/health
```

---

## 5. systemd

`/etc/systemd/system/shinybot-licenses.service`
```ini
[Unit]
Description=ShinyBot licenses
After=network.target

[Service]
WorkingDirectory=/opt/shinybot-licenses
EnvironmentFile=/opt/shinybot-licenses/.env
ExecStart=/opt/shinybot-licenses/.venv/bin/uvicorn licserver.main:app --host 127.0.0.1 --port 8080
Restart=always
User=www-data

[Install]
WantedBy=multi-user.target
```
```bash
sudo chown -R www-data:www-data /var/lib/shinybot-licenses
sudo systemctl daemon-reload
sudo systemctl enable --now shinybot-licenses
systemctl status shinybot-licenses --no-pager
```

---

## 6. HTTPS con nginx (+ certbot)

```bash
sudo apt install nginx certbot python3-certbot-nginx
```

`/etc/nginx/sites-available/shinybot` (web de venta Next en `:3002` + licencias en `:8080`):
```nginx
# web de venta (Next.js en :3002)
server {
    listen 80;
    server_name tudominio.com www.tudominio.com;
    location / {
        proxy_pass http://127.0.0.1:3002;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}

# API de licencias (FastAPI en :8080)
server {
    listen 80;
    server_name lic.tudominio.com;
    client_max_body_size 64k;
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```
```bash
sudo ln -s /etc/nginx/sites-available/shinybot /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
# TLS + redirect 80->443 + renovacion automatica (certbot.timer)
sudo certbot --nginx -d tudominio.com -d www.tudominio.com -d lic.tudominio.com
sudo certbot renew --dry-run
```
El bot apuntará a `https://lic.tudominio.com`. El servidor de licencias lee
`X-Forwarded-For`, así que registra bien la IP del cliente.

> **Web de venta (Next.js)**: corre en `127.0.0.1:3002` (p. ej.
> `next start -p 3002`, bajo systemd). nginx le hace `proxy_pass` ahí. Si haces
> **static export**, cambia ese `location` por
> `root /var/www/landing; try_files $uri $uri/ /index.html;` y no necesitas el
> proceso Node.

---

## 7. Webhook de la pasarela (LemonSqueezy)

1. LemonSqueezy → **Settings → Webhooks** → URL
   `https://lic.tudominio.com/api/webhook/lemonsqueezy`, eventos
   `order_created` y `subscription_payment_success`.
2. Copia el **signing secret** al `.env` → `WEBHOOK_SECRET` y reinicia el servicio.
3. Mapea cada `variant_id` de tus productos en `VARIANT_PLANS`
   (`{"123456":"7d","123457":"15d","123458":"30d"}`).

---

## 8. Pruebas de aceptación

```bash
# 1) health
curl -s https://lic.tudominio.com/health

# 2) crear una clave de prueba (admin)
curl -s -X POST https://lic.tudominio.com/api/admin/licenses \
  -H "X-Admin-Token: $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"plan":"7d","max_devices":3,"email":"prueba@x.com"}'
# -> {"ok":true,"key":"DK-XXXX-XXXX-XXXX", ...}

# 3) verify firmado (calcula la firma con el LICENSE_SECRET)
python3 - <<'PY'
import hashlib, hmac, json, os, urllib.request
secret = os.environ["LICENSE_SECRET"]
url = "https://lic.tudominio.com/api/verify"
body = json.dumps({"key":"DK-XXXX-XXXX-XXXX","machine_id":"prueba1","version":"1"}).encode()
sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
req = urllib.request.Request(url, data=body, method="POST",
        headers={"Content-Type":"application/json","X-Sig":sig})
print(urllib.request.urlopen(req, timeout=10).read().decode())
PY
# -> {"valid":true,"plan":"7d","expires_at":...,"devices_used":1,"max_devices":3,"sig":"..."}
```

Criterio: `health` responde, la clave de prueba valida con `valid:true` y
`devices_used:1`, y con una clave inexistente devuelve `valid:false`.

---

## 9. Operación

```bash
export DB_PATH=/var/lib/shinybot-licenses/licenses.db
cd /opt/shinybot-licenses && . .venv/bin/activate && set -a && . ./.env && set +a

python -m licserver.admin list
python -m licserver.admin create --plan 15d --email cliente@x.com
python -m licserver.admin extend DK-XXXX-XXXX-XXXX --days 30
python -m licserver.admin revoke DK-XXXX-XXXX-XXXX
python -m licserver.admin devices DK-XXXX-XXXX-XXXX
```

Backup del SQLite:
```bash
sqlite3 /var/lib/shinybot-licenses/licenses.db \
  ".backup '/backup/licenses-$(date +%F).db'"
```

---

## 10. Lo que debe hacer el agente de la VPS (resumen)

1. Crear `/opt/shinybot-licenses/` con `.env`, `requirements.txt` y `licserver/`
   (código de la §3).
2. `python3 -m venv`, `pip install -r requirements.txt`.
3. Probar `uvicorn` a mano y `curl /health`.
4. Instalar el **servicio systemd** (§5) y `enable --now`.
5. Configurar **nginx** con los dominios (§6), `nginx -t`, `reload` y `certbot`.
6. Configurar el **webhook** de la pasarela y `VARIANT_PLANS` (§7).
7. Correr las **pruebas de aceptación** (§8) y pegar la salida.
8. Reportar: dominio, ruta del SQLite, y confirmación de `health` + `verify`.

> **En el PC (después):** rellena en `pxg_bot/license.py`
> `_BUILTIN_SERVER = "https://lic.tudominio.com"` y
> `_BUILTIN_SECRET = "<el LICENSE_SECRET>"`, y compila con
> `build_exe_obfuscated.bat`.
