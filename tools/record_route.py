#!/usr/bin/env python3
"""Graba una ruta caminando con el personaje.

Mientras corre, muestrea la posición del jugador (vía el agente Lua) y guarda
waypoints cuando te alejas del último guardado. Al salir (Ctrl+C) los escribe en
config.json (route.waypoints).

Uso:
  python3 tools/record_route.py --min-step 3
  (camina la ruta con el personaje y pulsa Ctrl+C al terminar)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pxg_bot.config import load_config


def dist(a, b) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    parser.add_argument("--min-step", type=int, default=3, help="distancia mínima (tiles) entre waypoints")
    parser.add_argument("--interval", type=float, default=0.3)
    args = parser.parse_args()

    cfg = load_config(args.config)
    state_file = cfg["lua"]["state_file"]
    waypoints: list[list[int]] = []

    print("Grabando ruta... camina por el recorrido. Ctrl+C para terminar y guardar.")
    last = None
    try:
        while True:
            try:
                with open(state_file, "r", encoding="utf-8") as handle:
                    d = json.load(handle)
            except (OSError, ValueError):
                d = {}
            if d.get("connected") and "x" in d:
                cur = [int(d["x"]), int(d["y"]), int(d["z"])]
                if last is None or cur[2] != last[2] or dist(cur, last) >= args.min_step:
                    waypoints.append(cur)
                    last = cur
                    print(f"  wp[{len(waypoints)}] = {cur}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass

    if not waypoints:
        print("no se grabaron waypoints")
        return 1

    with open(args.config, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    raw.setdefault("route", {})
    raw["route"]["waypoints"] = waypoints
    with open(args.config, "w", encoding="utf-8") as handle:
        json.dump(raw, handle, indent=2, ensure_ascii=False)
    print(f"guardados {len(waypoints)} waypoints en {args.config}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
