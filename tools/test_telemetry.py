#!/usr/bin/env python3
"""Tests de la telemetria de muertes (sin cliente ni red).

Cubren: deteccion por XP/nivel/HP/alert/pokemon-faint, coalescing (settle),
cooldown anti-duplicados, escritura de trace y modo deshabilitado.

Uso: python3 tools/test_telemetry.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pxg_bot.telemetry as telmod
from pxg_bot.models import Creature, CreatureKind, GameState, Player, Pokemon, Vec3
from pxg_bot.telemetry import Telemetry

PASS = 0


def ok(msg: str) -> None:
    global PASS
    PASS += 1
    print("OK " + msg)


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def time(self) -> float:
        return self.t


def make_state(level=10, exp=1000, hp=100, party=None, msgs=None, connected=True) -> GameState:
    p = Player(name="Hero", pos=Vec3(100, 100, 7), hp=hp, max_hp=100, level=level,
               exp=exp, alive=hp > 0)
    st = GameState(player=p, creatures=[], party=party or [
        Pokemon(slot=1, name="Pikachu", hp_pct=100, active=True, alive=True)
    ])
    st.pokemon_pos = (100, 100, 7)
    st.pokemon_hp = 100
    st.moves = [{"key": "1", "pct": 100, "aoe": True, "effect": "damage"}]
    st.visible = {"w": 21, "h": 11}
    st.bag_counts = {"2269": 5}
    st.server_msgs = msgs or []
    st.connected = connected
    return st


def new_tel(base, **over):
    cfg = {"telemetry": {
        "enabled": True, "dir": base, "sample_secs": 0.0, "window_secs": 30.0,
        "trace": True, "death_cooldown_secs": 10.0, "settle_secs": 1.0, **over}}
    return Telemetry(cfg)


def read_jsonl(path):
    out = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    return out


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="pxg_tel_")
    clock = Clock()
    real_time = telmod.time
    telmod.time = clock
    try:
        # --- 1) deshabilitado: no crea ficheros ---
        d0 = os.path.join(tmp, "off")
        t = Telemetry({"telemetry": {"enabled": False, "dir": d0}})
        t.observe(make_state())
        assert not os.path.exists(os.path.join(d0, "pxg_trace.jsonl"))
        ok("deshabilitado: no escribe nada")

        # --- 2) trace + muerte por XP ---
        d1 = os.path.join(tmp, "on")
        t = new_tel(d1)
        t.observe(make_state(exp=1000))
        clock.t += 0.5
        t.observe(make_state(exp=1000))            # sin cambio
        clock.t += 0.5
        t.observe(make_state(exp=900, hp=0))       # XP baja + HP 0 -> muerte
        clock.t += 1.5
        t.observe(make_state(exp=900, hp=0))       # dispara el flush (settle)
        deaths = read_jsonl(os.path.join(d1, "pxg_deaths.jsonl"))
        trace = read_jsonl(os.path.join(d1, "pxg_trace.jsonl"))
        assert len(deaths) == 1, deaths
        sigs = set(deaths[0]["signals"])
        assert "exp_drop" in sigs and "player_death" in sigs, sigs
        assert len(deaths[0]["window"]) >= 2
        assert len(trace) >= 4
        ok("muerte por XP+HP: registra episodio (con coalescing) y trace")

        # --- 3) cooldown: no duplica ---
        clock.t += 1.0
        t.observe(make_state(exp=800, hp=0))       # otra caida, dentro del cooldown
        clock.t += 1.5
        t.observe(make_state(exp=800, hp=0))
        deaths = read_jsonl(os.path.join(d1, "pxg_deaths.jsonl"))
        assert len(deaths) == 1, deaths
        ok("cooldown: no registra una segunda muerte pegada")

        # --- 4) pokemon faint ---
        d2 = os.path.join(tmp, "faint")
        t = new_tel(d2, exp_drop=False, level_drop=False, player_death=False, alert_msg=False)
        t.observe(make_state(party=[Pokemon(slot=1, name="P", hp_pct=50, active=True)]))
        clock.t += 0.5
        t.observe(make_state(party=[Pokemon(slot=1, name="P", hp_pct=0, active=True)]))
        clock.t += 1.5
        t.observe(make_state(party=[Pokemon(slot=1, name="P", hp_pct=0, active=True)]))
        deaths = read_jsonl(os.path.join(d2, "pxg_deaths.jsonl"))
        assert deaths and "pokemon_faint" in deaths[0]["signals"], deaths
        ok("pokemon faint: detecta el pokemon debilitado")

        # --- 5) alerta en el chat ---
        d3 = os.path.join(tmp, "alert")
        t = new_tel(d3, exp_drop=False, level_drop=False, player_death=False, pokemon_faint=False)
        t.observe(make_state(msgs=["hola"]))
        clock.t += 0.5
        t.observe(make_state(msgs=["hola", "Has muerto. Pierdes experiencia."]))
        clock.t += 1.5
        t.observe(make_state(msgs=["hola", "Has muerto. Pierdes experiencia."]))
        deaths = read_jsonl(os.path.join(d3, "pxg_deaths.jsonl"))
        assert deaths and "alert_msg" in deaths[0]["signals"], deaths
        ok("alerta: detecta el mensaje de muerte en el chat")

        # --- 6) bajada de nivel ---
        d4 = os.path.join(tmp, "lvl")
        t = new_tel(d4, exp_drop=False)
        t.observe(make_state(level=42))
        clock.t += 0.5
        t.observe(make_state(level=41))
        clock.t += 1.5
        t.observe(make_state(level=41))
        deaths = read_jsonl(os.path.join(d4, "pxg_deaths.jsonl"))
        assert deaths and "level_drop" in deaths[0]["signals"], deaths
        ok("nivel: detecta la bajada de nivel")

        print("\nTELEMETRY OK")
        return 0
    finally:
        telmod.time = real_time
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
