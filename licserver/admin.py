"""CLI de administracion del servidor de licencias (stdlib).

Uso:
  python -m licserver.admin create --plan 30d [--max-devices 3] [--email x@y]
  python -m licserver.admin list
  python -m licserver.admin extend <KEY> --days 30
  python -m licserver.admin revoke <KEY> | restore <KEY> | delete <KEY>
  python -m licserver.admin devices <KEY>

La BD sale de DB_PATH (o ./licenses.db junto a este paquete).
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
