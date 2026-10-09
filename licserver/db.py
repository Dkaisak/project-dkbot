"""Base de datos (SQLite) del servidor de licencias.

Dos tablas:
- `licenses`: una fila por clave (plan, expiracion, max dispositivos, activa).
- `activations`: una fila por (clave, maquina) registrada.

Sin dependencias: `sqlite3` de la stdlib.
"""
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

# Generador de claves: sin caracteres ambiguos (0/O, 1/I/L).
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
    """Suma `days` a la expiracion (desde max(now, expires_at))."""
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


def list_licenses(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM licenses ORDER BY created_at DESC").fetchall()
    out = []
    for r in rows:
        n = conn.execute("SELECT COUNT(*) AS n FROM activations WHERE key = ?",
                         (r["key"],)).fetchone()["n"]
        d = dict(r)
        d["devices_used"] = n
        out.append(d)
    return out


def activations(conn: sqlite3.Connection, key: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM activations WHERE key = ? ORDER BY last_seen DESC", (key,)).fetchall()
    return [dict(r) for r in rows]


def register_device(conn: sqlite3.Connection, key: str, machine_id: str, ip: str = "") -> bool:
    """Registra la maquina si hay hueco (o si ya estaba). False si esta lleno."""
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
