#!/usr/bin/env python3
"""Post-mortem de la telemetria: explica por que muere el bot.

Lee `pxg_deaths.jsonl` y `pxg_trace.jsonl` (los escribe `pxg_bot/telemetry.py`)
y saca un resumen: cuantas muertes, de que tipo, y el contexto (vida, enemigos,
revives, behavior activo) en los segundos previos.

Uso:
  python3 tools/deaths_report.py                 # resuelve el dir desde config.json
  python3 tools/deaths_report.py --dir <mydata>
  python3 tools/deaths_report.py --config config.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pxg_bot.config import load_config


def read_jsonl(path: str) -> list:
    out = []
    if not path or not os.path.exists(path):
        return out
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def resolve_dir(cfg: dict, explicit: str) -> str:
    if explicit:
        return explicit
    tel = (cfg.get("telemetry") or {})
    if tel.get("dir"):
        return str(tel["dir"])
    lua = cfg.get("lua", {}) or {}
    base = lua.get("status_file") or lua.get("state_file") or ""
    return os.path.dirname(base) if base else ""


def fmt_ts(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return str(ts)


def window_stats(win: list) -> dict:
    if not win:
        return {}
    hp = [w.get("player_hp_pct") for w in win if isinstance(w.get("player_hp_pct"), (int, float))]
    enemies = [w.get("n_enemies_onscreen") for w in win if isinstance(w.get("n_enemies_onscreen"), (int, float))]
    dists = [w.get("min_dist_enemy_summon") for w in win
             if isinstance(w.get("min_dist_enemy_summon"), (int, float)) and w["min_dist_enemy_summon"] >= 0]
    rev = [w.get("revives") for w in win if isinstance(w.get("revives"), (int, float))]
    behaviors = Counter(str(w.get("chosen", "?")) for w in win)
    lures = Counter(str(w.get("lure_state")) for w in win if w.get("lure_state"))
    return {
        "span_s": round((win[-1].get("t", 0) - win[0].get("t", 0)), 1),
        "min_hp": min(hp) if hp else None,
        "hp_start": hp[0] if hp else None,
        "max_enemies_onscreen": max(enemies) if enemies else None,
        "min_dist_enemy": min(dists) if dists else None,
        "revives_last": rev[-1] if rev else None,
        "behaviors": behaviors,
        "lures": lures,
    }


def print_death(rec: dict) -> None:
    st = window_stats(rec.get("window") or [])
    sig = ",".join(rec.get("signals") or [])
    print(f"[{rec.get('index')}] {fmt_ts(rec.get('ts'))}  señales=[{sig}]")
    print(f"    nivel={rec.get('level')} exp={rec.get('exp')} hp={rec.get('player_hp_pct')}% "
          f"pos={tuple(rec.get('pos') or [])} behavior={rec.get('chosen')}")
    print(f"    ventana {st.get('span_s')}s: min_hp={st.get('min_hp')} "
          f"max_enemigos_pantalla={st.get('max_enemies_onscreen')} "
          f"min_dist_enemigo={st.get('min_dist_enemy')} revives={st.get('revives_last')}")
    if st.get("behaviors"):
        beh = " ".join(f"{k}:{v}" for k, v in st["behaviors"].most_common())
        print(f"    behaviors(ventana): {beh}")
    if st.get("lures"):
        lur = " ".join(f"{k}:{v}" for k, v in st["lures"].most_common())
        print(f"    lure: {lur}")
    msgs = rec.get("server_msgs") or []
    if msgs:
        print(f"    chat: {msgs[-1][:120]!r}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--dir", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config) if os.path.exists(args.config) else {}
    base = resolve_dir(cfg, args.dir)
    deaths_path = os.path.join(base, "pxg_deaths.jsonl")
    trace_path = os.path.join(base, "pxg_trace.jsonl")
    deaths = read_jsonl(deaths_path)
    trace = read_jsonl(trace_path)

    print(f"dir:    {base}")
    print(f"trace:  {len(trace)} muestras  ({trace_path})")
    print(f"muertes:{len(deaths)}  ({deaths_path})")
    print()

    if trace:
        t0 = trace[0].get("t")
        t1 = trace[-1].get("t")
        print(f"rango:  {fmt_ts(t0)} .. {fmt_ts(t1)}")
        hours = max(0.0001, ((t1 or 0) - (t0 or 0)) / 3600.0)
        print(f"tasa:   {len(deaths) / hours:.2f} muertes/hora")
        print()

    if not deaths:
        print("No hay muertes registradas todavia.")
        return 0

    print("=== Muertes ===")
    for rec in deaths:
        print_death(rec)
        print()

    print("=== Agregado ===")
    sig_count = Counter()
    for rec in deaths:
        for s in rec.get("signals") or []:
            sig_count[s] += 1
    print("por señal: " + (" ".join(f"{k}:{v}" for k, v in sig_count.most_common()) or "-"))
    ctx = [window_stats(r.get("window") or []) for r in deaths]
    no_revives = sum(1 for c in ctx if c.get("revives_last") == 0)
    print(f"muertes con 0 revives: {no_revives}/{len(deaths)}")
    behav = Counter()
    for c in ctx:
        for k, v in (c.get("behaviors") or {}).items():
            behav[k] += v
    if behav:
        print("behaviors en ventanas: " + " ".join(f"{k}:{v}" for k, v in behav.most_common()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
