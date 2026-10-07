#!/usr/bin/env python3
"""Tests de la capa de decision con LLM (meta-policy).

Cubren, sin red ni cliente:
- normalizacion de config y filtrado de behaviors gobernados,
- percepcion (GameState -> contexto JSON),
- grafo LangGraph (salida valida / no gobernada / error) con un modelo falso,
- controlador: sin API key, frescura (stale) y status,
- LlmBehavior: delegacion en route/combat, fallback sin decision y no-viable.

Uso: python3 tools/test_llm.py
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pxg_bot.behaviors import Blackboard, build_behaviors
from pxg_bot.llm.config import normalize
from pxg_bot.llm.controller import LlmController
from pxg_bot.llm.graph import build_graph
from pxg_bot.llm.perception import render, summarize
from pxg_bot.llm.decision import Decision
from pxg_bot.models import Creature, CreatureKind, GameState, Player, Pokemon, Vec3
from pxg_bot.pathfinding import Route

PASS = 0


def ok(msg: str) -> None:
    global PASS
    PASS += 1
    print("OK " + msg)


class RecInput:
    def __init__(self) -> None:
        self.walks = []
        self.moves = []
        self.skills = []
        self.stops = 0
        self.orders = []
        self.modes = []

    def walk_to(self, target) -> None:
        self.walks.append((target.x, target.y, target.z))

    def walk_path(self, wps, z) -> None:
        pass

    def move(self, direction) -> None:
        self.moves.append(direction)

    def press(self, key, duration=0.03) -> None:
        self.skills.append((key, duration))

    def stop(self) -> None:
        self.stops += 1

    def order(self, target) -> None:
        self.orders.append((target.x, target.y, target.z))

    def middle_click(self, target) -> None:
        pass

    def loot(self) -> None:
        pass

    def ball(self, item_id, target) -> None:
        pass

    def call_slot(self, slot) -> None:
        pass

    def click_slot(self, slot) -> None:
        pass

    def revive(self, slot, item_id=2269) -> None:
        pass

    def hotkey(self, key) -> None:
        pass

    def set_fight_mode(self, mode) -> None:
        self.modes.append(mode)

    def pokestop(self, method="func", arg="") -> None:
        pass


def _state(with_enemy: bool = False, with_aoe: bool = True) -> GameState:
    player = Player(name="Hero", pos=Vec3(100, 100, 7), hp=100, max_hp=100,
                    mp=50, max_mp=50, level=10, alive=True)
    creatures = []
    if with_enemy:
        creatures.append(Creature(cid=1, name="Rattata", pos=Vec3(101, 100, 7),
                                  hp_pct=100, kind=CreatureKind.POKEMON, is_wild=True))
    moves = [{"key": "1", "name": "Air Vortex", "pct": 100, "aoe": True, "effect": "damage"}] if with_aoe else []
    st = GameState(player=player, creatures=creatures, party=[
        Pokemon(slot=0, name="Pikachu", hp_pct=100, active=True, alive=True)
    ])
    st.pokemon_pos = (100, 100, 7)
    st.pokemon_hp = 100
    st.moves = moves
    st.visible = {"w": 21, "h": 11}
    st.bag_counts = {"2269": 5}
    st.connected = True
    return st


# --- normalizacion ---
cfg = normalize({"enabled": True, "governed": ["combat", "explore", "bogus"],
                 "decide_interval": "3"})
assert cfg["governed"] == ["combat", "explore"], cfg["governed"]
assert cfg["priority"] == 71 and cfg["decide_interval"] == 3.0
ok("normalize: filtra behaviors desconocidos y sanea tipos")

cfg2 = normalize({"enabled": True, "governed": []})
assert cfg2["governed"] == ["combat", "route", "explore"]
ok("normalize: governed vacio cae a los tres por defecto")


# --- percepcion ---
ctx = summarize(_state(with_enemy=True), normalize({}))
text = render(ctx)
assert "enemies_total" in ctx and ctx["enemies_total"] == 1
assert ctx["enemies_nearest"][0]["name"] == "Rattata"
assert ctx["revives"] == 5
assert isinstance(text, str) and text.startswith("{")
ok("percepcion: resume enemigos, summon, revives y renderiza JSON")


# --- grafo LangGraph con modelo falso ---
class _Stub:
    def __init__(self, result):
        self._r = result

    def invoke(self, messages):
        if isinstance(self._r, Exception):
            raise self._r
        return self._r


class FakeModel:
    def __init__(self, result):
        self._r = result

    def with_structured_output(self, schema, **kwargs):
        return _Stub(self._r)


g = build_graph(FakeModel(Decision(behavior="combat", rationale="pelear")),
                ["combat", "route", "explore"])
out = g.invoke({"context": "{}"})
assert out.get("decision", {}).get("behavior") == "combat", out
ok("grafo: decision valida pasa (combat)")

g2 = build_graph(FakeModel(Decision(behavior="explore", rationale="x")), ["combat", "route"])
out2 = g2.invoke({"context": "{}"})
assert out2.get("decision") is None, out2
ok("grafo: behavior no gobernado se descarta")

g3 = build_graph(FakeModel(RuntimeError("boom")), ["combat", "route", "explore"])
out3 = g3.invoke({"context": "{}"})
assert out3.get("decision") is None and "boom" in out3.get("error", "")
ok("grafo: error del modelo -> sin decision (fallback)")


# --- controlador: sin API key ---
os.environ.pop("OPENCODE_API_KEY", None)
ctrl = LlmController({"enabled": True, "governed": ["combat", "route", "explore"]},
                     governed=["combat", "route", "explore"])
ctrl.start()
assert ctrl.available is False and ctrl.latest() is None
assert "API key" in ctrl.error
ok("controlador: sin API key queda inactivo (no rompe)")

# frescura (stale) sin red: se fuerza la decision a mano
ctrl2 = LlmController({"enabled": True}, governed=["combat"])
ctrl2.available = True
ctrl2._decision = {"behavior": "combat", "rationale": "x"}
ctrl2._decision_at = time.time()
assert ctrl2.latest() is not None
ctrl2._decision_at = time.time() - 999
assert ctrl2.latest() is None
ok("controlador: latest() respeta la frescura (stale_secs)")


# --- LlmBehavior: delegacion ---
class FakeController:
    def __init__(self, decision):
        self._d = decision

    def latest(self):
        return self._d

    def status(self):
        return {"enabled": True}


def _behaviors(llm_decision, route, with_enemy=False, with_aoe=True):
    cfg = {"llm": {"enabled": True, "governed": ["combat", "route", "explore"],
                   "priority": 71},
           "behaviors": {}, "settings": {}}
    beasts = build_behaviors(cfg, route, world=None, llm=FakeController(llm_decision))
    return beasts


route = Route.from_config([[110, 100, 7], [120, 100, 7]], loop=True)
behaviors = _behaviors({"behavior": "route", "rationale": "avanzar"}, route)
names = [b.name for b in behaviors]
assert "llm" in names, names
assert names.index("llm") < names.index("combat")
llm_behavior = next(b for b in behaviors if b.name == "llm")
bb = Blackboard(route=route)
st = _state()
assert llm_behavior.evaluate(st, bb) is True
assert bb.notes.get("llm_choice") == "route"
inp = RecInput()
llm_behavior.act(st, bb, inp)
assert inp.walks, "deberia haber delegado en route (walk_to)"
ok("LlmBehavior: decision route delega en RouteBehavior")

# sin decision -> fallback
behaviors = _behaviors(None, route)
llm_behavior = next(b for b in behaviors if b.name == "llm")
bb = Blackboard(route=route)
assert llm_behavior.evaluate(_state(), bb) is False
ok("LlmBehavior: sin decision no actua (fallback a prioridad clasica)")

# decision no viable (route sin ruta) -> no actua
behaviors = _behaviors({"behavior": "route", "rationale": "x"}, None)
llm_behavior = next(b for b in behaviors if b.name == "llm")
bb = Blackboard(route=None)
assert llm_behavior.evaluate(_state(), bb) is False
ok("LlmBehavior: decision no viable (sin ruta) no actua")

# decision combat con enemigos -> delega en CombatBehavior
behaviors = _behaviors({"behavior": "combat", "rationale": "pelear"}, route)
llm_behavior = next(b for b in behaviors if b.name == "llm")
bb = Blackboard(route=route)
st = _state(with_enemy=True, with_aoe=True)
assert llm_behavior.evaluate(st, bb) is True
assert bb.notes.get("llm_choice") == "combat"
ok("LlmBehavior: decision combat viable delega en CombatBehavior")

print("\nLLM OK")


if __name__ == "__main__":
    pass
