#!/usr/bin/env python3
"""Tests focalizados de behaviors que la fuente de memoria no puede cubrir
(loot y captura de cuerpos via state.defeated). No requiere el juego ni X11.

Uso: python3 tools/test_behaviors.py
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pxg_bot.behaviors import (Blackboard, CaptureBehavior, CombatBehavior, CrisisBehavior,
                               LootBehavior, ReviveBehavior, SummonBehavior, RouteBehavior,
                               summon_near_tile)
from pxg_bot.lua_bridge import LuaBridge, LuaStateSource
from pxg_bot.models import Creature, CreatureKind, GameState, Player, Pokemon, Vec3
from pxg_bot.pathfinding import Route


class RecInput:
    """Input de prueba que registra loot/balls/skills."""

    def __init__(self) -> None:
        self.loot_calls = 0
        self.balls: list = []
        self.walks: list = []
        self.moves: list = []
        self.skills: list = []
        self.modes: list = []
        self.ball_throws: list = []
        self.orders: list = []
        self.calls: list = []
        self.stops: int = 0
        self.pokestops: int = 0

    def set_fight_mode(self, mode: int) -> None:
        self.modes.append(int(mode))

    def stop(self) -> None:
        self.stops += 1

    def pokestop(self, method: str = "func", arg: str = "") -> None:
        self.pokestops += 1

    def call_slot(self, slot: int) -> None:
        self.calls.append(int(slot))

    def order(self, target) -> None:
        self.orders.append((target.x, target.y, target.z))

    def ball(self, item_id: int, target) -> None:
        self.ball_throws.append((int(item_id), target.x, target.y, target.z))

    def middle_click(self, target) -> None:
        pass

    def loot(self) -> None:
        self.loot_calls += 1

    def hotkey(self, key: str) -> None:
        self.balls.append(key)

    def walk_to(self, target: Vec3) -> None:
        self.walks.append((target.x, target.y, target.z))

    def move(self, direction: int) -> None:
        self.moves.append(direction)

    def press(self, key: str) -> None:
        self.skills.append(key)


def make_state(px: int = 100, py: int = 100, defeated=None, creatures=None) -> GameState:
    st = GameState(player=Player(name="P", pos=Vec3(px, py, 7), hp=100, max_hp=100))
    st.visible = {"w": 21, "h": 11}
    st.defeated = defeated or []
    st.creatures = creatures or []
    return st


def test_loot_adjacent() -> None:
    bb = Blackboard()
    st = make_state(defeated=[{"id": "1", "name": "Rattata", "x": 100, "y": 100, "z": 7}])
    bb.update_corpses(st)
    assert len(bb.notes["corpses_pending"]) == 1, bb.notes["corpses_pending"]

    loot = LootBehavior({"enabled": True, "reach": 20,
                         "collect_interval": 0.0, "collect_grace": 0.0}, {})
    assert loot.evaluate(st, bb) is True, "loot no detecto el cuerpo adyacente"
    inp = RecInput()
    loot.act(st, bb, inp)
    assert inp.loot_calls == 1, inp.loot_calls

    # al expirar la gracia, el cuerpo pasa a looted y se limpia
    bb.notes["collect_rec"]["t"] = time.time() - 10
    assert loot.evaluate(st, bb) is False
    assert not bb.notes["corpses_pending"]
    assert "1" in bb.notes["looted_ids"]
    print("OK loot adyacente -> recoge y marca looted")


def test_loot_no_duplicates() -> None:
    bb = Blackboard()
    st = make_state(defeated=[{"id": "1", "name": "Rattata", "x": 100, "y": 100, "z": 7}])
    bb.update_corpses(st)
    bb.update_corpses(st)
    assert len(bb.notes["corpses_pending"]) == 1
    print("OK sin cuerpos duplicados")


def test_capture_shiny_after_loot() -> None:
    bb = Blackboard()
    st = make_state()
    st.creatures = [Creature(cid=7, name="Shiny X", pos=Vec3(100, 100, 7),
                             hp_pct=100, kind=CreatureKind.MONSTER, shiny=True)]
    st.defeated = [{"id": "7", "name": "Shiny X", "x": 100, "y": 100, "z": 7}]
    bb.update_corpses(st)
    assert bb.notes["corpse_meta"]["7"]["shiny"] is True

    # el cuerpo ya fue looteado; desaparece de pantalla
    bb.notes.setdefault("looted_ids", set()).add("7")
    st.creatures = []

    cap = CaptureBehavior({"enabled": False, "catch_all": False}, {})
    assert cap.evaluate(st, bb) is True, "el shiny debe capturarse aunque capture este off"
    assert bb.notes["capture_pending"][0]["id"] == "7"
    print("OK captura de shiny tras loot (independiente de 'enabled')")


def test_capture_nonshiny_off() -> None:
    bb = Blackboard()
    st = make_state()
    st.defeated = [{"id": "9", "name": "Rattata", "x": 100, "y": 100, "z": 7}]
    bb.update_corpses(st)
    bb.notes.setdefault("looted_ids", set()).add("9")
    cap = CaptureBehavior({"enabled": False, "catch_all": False}, {})
    assert cap.evaluate(st, bb) is False, "un cuerpo no-shiny no debe capturarse si catch_all esta off"
    print("OK cuerpo no-shiny no se captura con catch_all off")


def test_fight_mode_lure() -> None:
    bb = Blackboard()
    cb = CombatBehavior({"lure_aoe": True, "manage_fight_mode": True,
                         "cast_fight_mode": 1, "idle_fight_mode": 3,
                         "fight_mode_delay": 0.3}, {})
    inp = RecInput()
    # sin lure -> defensivo
    cb._manage_fight_mode(bb, inp)
    assert inp.modes == [3], inp.modes
    # lureando (wait) -> sigue defensivo, sin reenviar
    bb.notes["lure_state"] = "wait"
    cb._manage_fight_mode(bb, inp)
    assert inp.modes == [3], inp.modes
    # a lanzar skills (fight) -> ofensivo
    bb.notes["lure_state"] = "fight"
    cb._manage_fight_mode(bb, inp)
    assert inp.modes == [3, 1], inp.modes
    assert cb._fight_mode_waiting(bb) is True, "debe esperar el delay antes del burst"
    bb.notes["fight_mode_at"] = time.time() - 1.0
    assert cb._fight_mode_waiting(bb) is False
    # termina la pelea -> vuelve a defensivo
    bb.notes["lure_state"] = "resume"
    cb._manage_fight_mode(bb, inp)
    assert inp.modes == [3, 1, 3], inp.modes
    print("OK fight mode: defensivo fuera del burst, ofensivo en 'fight' + delay")


def test_fight_mode_ignored_without_lure() -> None:
    bb = Blackboard()
    inp = RecInput()
    cb = CombatBehavior({"lure_aoe": False}, {})
    bb.notes["lure_state"] = "fight"
    cb._manage_fight_mode(bb, inp)
    assert inp.modes == [], "sin lure no debe tocar el modo"
    print("OK fight mode no se toca si lure_aoe esta off")


def test_revive_atomic() -> None:
    # loot cede mientras hay una secuencia de revive en curso
    bb = Blackboard()
    st = make_state(defeated=[{"id": "1", "name": "Rattata", "x": 100, "y": 100, "z": 7}])
    bb.update_corpses(st)
    loot = LootBehavior({"enabled": True, "reach": 20}, {})
    assert loot.evaluate(st, bb) is True
    bb.notes["revive_rec"] = {"slot": 3, "step": "recall", "t": time.time()}
    assert loot.evaluate(st, bb) is False, "loot debe ceder durante el revive"

    # captura tambien cede
    bb2 = Blackboard()
    st2 = make_state()
    st2.creatures = [Creature(cid=7, name="Shiny X", pos=Vec3(100, 100, 7),
                              hp_pct=100, kind=CreatureKind.MONSTER, shiny=True)]
    st2.defeated = [{"id": "7", "name": "Shiny X", "x": 100, "y": 100, "z": 7}]
    bb2.update_corpses(st2)
    bb2.notes.setdefault("looted_ids", set()).add("7")
    st2.creatures = []
    cap = CaptureBehavior({"enabled": False, "catch_all": False}, {})
    assert cap.evaluate(st2, bb2) is True
    bb2.notes["revive_rec"] = {"slot": 3, "step": "recall", "t": time.time()}
    assert cap.evaluate(st2, bb2) is False, "captura debe ceder durante el revive"
    print("OK revive atomico: loot/captura ceden durante la secuencia")


def test_capture_range() -> None:
    """La ball se lanza desde <= range (4); si el cuerpo esta mas lejos, se acerca."""
    bb = Blackboard()
    st = make_state()
    st.creatures = [Creature(cid=7, name="Shiny X", pos=Vec3(104, 100, 7), hp_pct=100,
                             kind=CreatureKind.MONSTER, shiny=True)]
    st.defeated = [{"id": "7", "name": "Shiny X", "x": 104, "y": 100, "z": 7}]
    bb.update_corpses(st)
    bb.notes.setdefault("looted_ids", set()).add("7")
    st.creatures = []
    cap = CaptureBehavior({"enabled": True, "catch_all": True, "ball_item": 2652,
                           "interval": 0.0, "max_throws": 5, "range": 4}, {})
    assert cap.evaluate(st, bb) is True
    inp = RecInput()
    cap.act(st, bb, inp)
    assert inp.ball_throws == [(2652, 104, 100, 7)], f"a 4 tiles debe lanzar: {inp.ball_throws}"
    assert inp.walks == [], "a 4 tiles no debe caminar"
    # a 5 tiles -> se acerca, no lanza
    bb2 = Blackboard()
    st2 = make_state()
    st2.creatures = [Creature(cid=9, name="Shiny Y", pos=Vec3(105, 100, 7), hp_pct=100,
                              kind=CreatureKind.MONSTER, shiny=True)]
    st2.defeated = [{"id": "9", "name": "Shiny Y", "x": 105, "y": 100, "z": 7}]
    bb2.update_corpses(st2)
    bb2.notes.setdefault("looted_ids", set()).add("9")
    st2.creatures = []
    cap2 = CaptureBehavior({"enabled": True, "catch_all": True, "ball_item": 2652,
                            "interval": 0.0, "range": 4}, {})
    assert cap2.evaluate(st2, bb2) is True
    inp2 = RecInput()
    cap2.act(st2, bb2, inp2)
    assert inp2.ball_throws == [], f"a >4 no debe lanzar: {inp2.ball_throws}"
    assert inp2.walks, "debe acercarse si esta a mas de 4"
    print("OK capture: lanza desde <= 4 tiles y se acerca si esta mas lejos")


def test_capture_uses_lua_ball() -> None:
    bb = Blackboard()
    st = make_state()
    st.creatures = [Creature(cid=7, name="Shiny X", pos=Vec3(100, 100, 7),
                             hp_pct=100, kind=CreatureKind.MONSTER, shiny=True)]
    st.defeated = [{"id": "7", "name": "Shiny X", "x": 100, "y": 100, "z": 7}]
    bb.update_corpses(st)
    bb.notes.setdefault("looted_ids", set()).add("7")
    st.creatures = []
    cap = CaptureBehavior({"enabled": True, "catch_all": True, "ball_item": 2652,
                           "interval": 0.0, "max_throws": 2, "range": 1}, {})
    assert cap.evaluate(st, bb) is True
    inp = RecInput()
    cap.act(st, bb, inp)
    assert inp.ball_throws == [(2652, 100, 100, 7)], inp.ball_throws
    print("OK capture lanza la ball por Lua (item 2652) sobre el cuerpo")


def test_buff_on_screen() -> None:
    bb = Blackboard()
    st = make_state()
    st.pokemon_pos = (100, 100, 7)
    st.moves = [
        {"key": "9", "aoe": False, "effect": "buff/nevermiss", "pct": 100, "name": "Confide"},
        {"key": "7", "aoe": True, "effect": "damage/debuff", "pct": 100, "name": "Air Vortex"},
    ]
    cb = CombatBehavior({"buff_on_screen": True, "buff_visible_min": 3,
                         "ready_pct": 100, "cooldown": 0.0}, {})
    inp = RecInput()
    st.creatures = [Creature(cid=i, name=f"E{i}", pos=Vec3(101 + i, 100, 7), hp_pct=100,
                             kind=CreatureKind.MONSTER, is_wild=True) for i in range(2)]
    cb.act(st, bb, inp)
    assert "9" not in inp.skills, f"con 2 enemigos NO debe buffear: {inp.skills}"
    st.creatures = [Creature(cid=i, name=f"E{i}", pos=Vec3(101 + i, 100, 7), hp_pct=100,
                             kind=CreatureKind.MONSTER, is_wild=True) for i in range(3)]
    assert cb.evaluate(st, bb) is True, "con 3 enemigos evaluate debe pedir el buff"
    cb.act(st, bb, inp)
    assert "9" in inp.skills, f"con 3 enemigos DEBE buffear: {inp.skills}"
    print("OK buff: se usa con 3 enemigos en pantalla (no por lure)")


def test_revive_rec_not_cancelled() -> None:
    bb = Blackboard()
    st = make_state()
    st.creatures = [Creature(cid=1, name="Fearow", pos=Vec3(101, 100, 7), hp_pct=100,
                             kind=CreatureKind.MONSTER, is_wild=True)]
    rb = ReviveBehavior({"enabled": True, "on_stun": True}, {})
    bb.notes["revive_rec"] = {"slot": 3, "step": "recall_wait", "t": time.time()}
    # con enemigos en pantalla y sin stun, la secuencia YA INICIADA debe continuar
    assert rb.evaluate(st, bb) is True, "no debe cancelar la secuencia en curso"
    assert "revive_rec" in bb.notes
    print("OK revive: la secuencia en curso no se cancela por enemigos")


def test_revive_first_flag() -> None:
    bb = Blackboard()
    st = make_state()
    rb = ReviveBehavior({"enabled": True}, {})
    rb.act(st, bb, RecInput())
    assert bb.notes["revive_rec"]["first"] is True, "el primer revive debe marcarse 'first'"
    bb.notes.pop("revive_rec")
    rb._revives_done = 1
    rb.act(st, bb, RecInput())
    assert bb.notes["revive_rec"]["first"] is False, "los siguientes no"
    print("OK revive: 'first' solo en el primer revive")


class _FakeOtmm:
    ready = True

    def __init__(self, blocked=()):
        self.blocked = set(blocked)

    def pathable(self, x, y, z):
        return (x, y) not in self.blocked


class _FakeWorld:
    def __init__(self, blocked=()):
        self.otmm = _FakeOtmm(blocked)


def test_summon_picks_free_tile() -> None:
    bb = Blackboard()
    st = make_state(px=100, py=100)
    st.pokemon_pos = (105, 100, 7)  # lejos del player -> reordenar
    sb = SummonBehavior({"enabled": True, "interval_secs": 0.0, "radius": 4, "max_dist": 1},
                        {}, _FakeWorld())
    assert sb.evaluate(st, bb) is True, "debe querer reordenar (esta lejos)"
    inp = RecInput()
    sb.act(st, bb, inp)
    assert inp.orders, "debe ordenar el ownsummon"
    ox, oy, oz = inp.orders[0]
    mm = _FakeOtmm()
    assert all(mm.pathable(ox + dx, oy + dy, oz)
               for dx in (-1, 0, 1) for dy in (-1, 0, 1)), "los 8 vecinos deben ser libres"
    print("OK summon: ordena el ownsummon a un tile con vecinos libres")


def test_revive_waits_after_aoe() -> None:
    bb = Blackboard()
    st = make_state()
    # una AoE en cooldown y otra LISTA -> no agotado (respeta after_aoe_wait)
    st.moves = [{"key": "5", "aoe": True, "effect": "damage", "pct": 0},
                {"key": "6", "aoe": True, "effect": "damage", "pct": 100}]
    rb = ReviveBehavior({"enabled": True, "after_aoe_wait_secs": 1.0, "on_stun": True}, {})
    bb.notes["last_aoe_at"] = time.time()
    assert rb.evaluate(st, bb) is False, "no debe arrancar el revive dentro de 1s del AoE"
    bb.notes["last_aoe_at"] = time.time() - 2.0
    assert rb.evaluate(st, bb) is True, "tras 1s del AoE, si revive"
    print("OK revive: espera 1s tras el ultimo AoE")


def test_lure_summon_orders() -> None:
    bb = Blackboard()
    st = make_state(px=100, py=100)
    st.pokemon_pos = (105, 100, 7)
    cb = CombatBehavior({"lure_aoe": True, "lure_summon": True, "lure_summon_radius": 3,
                         "ready_pct": 100, "cooldown": 0.0}, {}, _FakeWorld())
    inp = RecInput()
    bb.notes["lure_state"] = "hold"
    cb.act(st, bb, inp)
    assert inp.orders, "en 'hold' debe ordenar el ownsummon cerca del player"
    assert inp.stops == 1, "en 'hold' debe parar la navegacion de la ruta"
    assert bb.notes.get("lure_order_target"), "debe guardar el tile objetivo del order"
    ox, oy, oz = inp.orders[0]
    mm = _FakeOtmm()
    assert all(mm.pathable(ox + dx, oy + dy, oz)
               for dx in (-1, 0, 1) for dy in (-1, 0, 1))
    print("OK lure: en hold para la ruta y ordena el ownsummon (vecinos libres)")


def test_shiny_observe() -> None:
    from pxg_bot.shiny import ShinyTable
    import tempfile
    path = tempfile.mktemp(suffix=".json")
    try:
        t = ShinyTable(path, min_obs=1)
        summon = Creature(cid=1, name="Fearow", pos=Vec3(1, 1, 7), hp_pct=100,
                          kind=CreatureKind.MONSTER, is_summon=True, outfit=9004, uid="s1")
        wild = Creature(cid=2, name="Fearow", pos=Vec3(2, 2, 7), hp_pct=100,
                        kind=CreatureKind.MONSTER, outfit=9007, uid="w1")
        t.observe([summon, wild])
        assert 9004 not in t.counts.get("Fearow", {}), "el summon no debe contar"
        assert t.counts["Fearow"][9007] == 1
        # dedupe: la misma criatura en varios ticks cuenta una sola vez
        t.observe([summon, wild])
        assert t.counts["Fearow"][9007] == 1, "no debe duplicar por tick"
        # criatura nueva (otro uid) -> cuenta de nuevo
        t.observe([Creature(cid=3, name="Fearow", pos=Vec3(2, 2, 7), hp_pct=100,
                            kind=CreatureKind.MONSTER, outfit=9007, uid="w2")])
        assert t.counts["Fearow"][9007] == 2
        assert t.normal("Fearow") == 9007
        print("OK shiny: no cuenta summons y deduplica por encuentro")
    finally:
        import os as _os
        for p in (path, path + ".tmp"):
            try:
                _os.remove(p)
            except OSError:
                pass


def test_lure_combo_custom() -> None:
    """Con combo propio del lure se usan exactamente esas skills, en su orden."""
    bb = Blackboard()
    st = make_state()
    st.pokemon_pos = (100, 100, 7)
    st.attacking_name = "E1"
    st.lure_order = ["2", "7"]          # incluye una no-AoE: manda el usuario
    st.moves = [
        {"key": "2", "aoe": False, "effect": "damage", "pct": 100, "name": "Feather"},
        {"key": "3", "aoe": True, "effect": "damage", "pct": 100, "name": "Drill"},
        {"key": "7", "aoe": True, "effect": "damage/debuff", "pct": 100, "name": "Air Vortex"},
    ]
    st.creatures = [
        Creature(cid=1, name="E1", pos=Vec3(101, 100, 7), hp_pct=100,
                 kind=CreatureKind.MONSTER, is_wild=True),
        Creature(cid=2, name="E2", pos=Vec3(100, 101, 7), hp_pct=100,
                 kind=CreatureKind.MONSTER, is_wild=True),
    ]
    cb = CombatBehavior({"ready_pct": 100, "cooldown": 0.0, "attack_range": 3,
                         "cast_min_in_range": 2, "lure_use_single": False}, {})
    inp = RecInput()
    cb._aoe_fight(st, bb, inp, st.creatures)
    assert inp.skills == ["2"], f"primera del combo (no-AoE): {inp.skills}"
    cb._aoe_fight(st, bb, inp, st.creatures)
    assert inp.skills == ["2", "7"], f"el combo manda; 3 no está en el lure: {inp.skills}"
    print("OK combo del lure: usa exactamente la lista configurada")


def test_lure_idle_to_hold() -> None:
    """idle: caminar hasta X visibles -> hold; por debajo sigue la ruta."""
    st = make_state()
    st.pokemon_pos = (100, 100, 7)
    st.moves = [{"key": "7", "aoe": True, "effect": "damage", "pct": 100, "name": "Air Vortex"}]
    st.creatures = [Creature(cid=i, name=f"E{i}", pos=Vec3(105 + i, 100, 7), hp_pct=100,
                             kind=CreatureKind.MONSTER, is_wild=True) for i in range(2)]
    cb = CombatBehavior({"lure_aoe": True, "lure_visible_min": 3}, {})
    assert cb.evaluate(st, Blackboard()) is False, "con menos de X sigue la ruta"
    st.creatures.append(Creature(cid=9, name="E9", pos=Vec3(108, 100, 7), hp_pct=100,
                                 kind=CreatureKind.MONSTER, is_wild=True))
    bb = Blackboard()
    assert cb.evaluate(st, bb) is True
    assert bb.notes.get("lure_state") == "hold"
    print("OK lure: idle -> hold al juntar X visibles")


def test_lure_hold_requires_all_in_range() -> None:
    """hold: no pasa a fight hasta summon llegado + todos a rango; timeout -> resume."""
    st = make_state()
    st.pokemon_pos = (100, 100, 7)
    st.moves = [{"key": "7", "aoe": True, "effect": "damage", "pct": 100, "name": "Air Vortex"}]
    st.creatures = [
        Creature(cid=1, name="E1", pos=Vec3(101, 100, 7), hp_pct=100,
                 kind=CreatureKind.MONSTER, is_wild=True),
        Creature(cid=2, name="E2", pos=Vec3(102, 100, 7), hp_pct=100,
                 kind=CreatureKind.MONSTER, is_wild=True),
        Creature(cid=3, name="E3", pos=Vec3(105, 100, 7), hp_pct=100,
                 kind=CreatureKind.MONSTER, is_wild=True),
    ]
    cb = CombatBehavior({"lure_aoe": True, "lure_visible_min": 3, "attack_range": 3,
                         "require_all_close": True, "hold_timeout": 100.0}, {})
    bb = Blackboard()
    bb.notes["lure_state"] = "hold"
    bb.notes["lure_hold_start"] = time.time()
    bb.notes["lure_order_target"] = (100, 100, 7)  # summon "llego"
    assert cb.evaluate(st, bb) is True
    assert bb.notes.get("lure_state") == "hold", "con uno lejos sigue en hold"
    st.creatures[2].pos = Vec3(103, 100, 7)        # ahora todos a rango
    assert cb.evaluate(st, bb) is True
    assert bb.notes.get("lure_state") == "fight"
    assert bb.notes.get("lure_pokestop") is True
    # timeout sin agrupar -> resume
    st.creatures[2].pos = Vec3(108, 100, 7)
    bb2 = Blackboard()
    bb2.notes["lure_state"] = "hold"
    bb2.notes["lure_hold_start"] = time.time() - 999
    bb2.notes["lure_order_target"] = (100, 100, 7)
    cb.evaluate(st, bb2)
    assert bb2.notes.get("lure_state") == "resume", "sin agrupar -> resume"
    print("OK lure: hold exige summon llegado + todos a rango; timeout -> resume")


def test_lure_fight_returns_to_hold() -> None:
    """fight: si el gate cae de forma persistente, vuelve a hold a reagrupar."""
    st = make_state()
    st.pokemon_pos = (100, 100, 7)
    st.moves = [{"key": "7", "aoe": True, "effect": "damage", "pct": 100, "name": "Air Vortex"}]
    st.creatures = [
        Creature(cid=1, name="E1", pos=Vec3(101, 100, 7), hp_pct=100,
                 kind=CreatureKind.MONSTER, is_wild=True),
        Creature(cid=2, name="E2", pos=Vec3(108, 100, 7), hp_pct=100,
                 kind=CreatureKind.MONSTER, is_wild=True),  # lejos -> gate cae
    ]
    cb = CombatBehavior({"lure_aoe": True, "attack_range": 3, "require_all_close": True,
                         "fight_recover_secs": 1.0, "cooldown": 0.0, "ready_pct": 100}, {})
    bb = Blackboard()
    bb.notes["lure_state"] = "fight"
    inp = RecInput()
    cb._lure_fight(st, bb, inp)
    assert bb.notes.get("lure_state") == "fight", "aun no (recien cae)"
    bb.notes["lure_fight_lost"] = time.time() - 5.0
    cb._lure_fight(st, bb, inp)
    assert bb.notes.get("lure_state") == "hold", "gate caido persistente -> hold"
    print("OK lure: fight vuelve a hold si el gate cae de forma persistente")


def test_gate_ignores_far_enemy() -> None:
    bb = Blackboard()
    st = make_state()
    st.pokemon_pos = (100, 100, 7)
    st.attacking_name = "E1"
    st.moves = [{"key": "7", "aoe": True, "effect": "damage", "pct": 100, "name": "Air Vortex"}]
    st.creatures = [
        Creature(cid=1, name="E1", pos=Vec3(101, 100, 7), hp_pct=100,
                 kind=CreatureKind.MONSTER, is_wild=True),
        # lejos del summon (dist 10 > engage_radius) pero dentro de la camara
        Creature(cid=2, name="E2", pos=Vec3(110, 100, 7), hp_pct=100,
                 kind=CreatureKind.MONSTER, is_wild=True),
    ]
    cb = CombatBehavior({"lure_aoe": True, "ready_pct": 100, "cooldown": 0.0,
                         "attack_range": 3, "engage_radius": 8, "cast_min_in_range": 1,
                         "require_all_close": False}, {})
    assert cb._all_enemies_close(st, 3) is False, "el criterio estricto bloquea"
    assert cb._enough_in_range(st, 3) is True, "el criterio nuevo no debe bloquear"
    inp = RecInput()
    cb._aoe_fight(st, bb, inp, st.creatures)
    assert inp.skills == ["7"], f"debe castear ignorando al lejano: {inp.skills}"

    # con cast_min_in_range=2: 2 enemigos que cuentan, pero solo 1 a rango de ataque
    st.creatures.append(
        Creature(cid=3, name="E3", pos=Vec3(106, 100, 7), hp_pct=100,
                 kind=CreatureKind.MONSTER, is_wild=True))
    cb2 = CombatBehavior({"cast_min_in_range": 2, "engage_radius": 8, "attack_range": 3,
                          "require_all_close": False}, {})
    assert cb2._enough_in_range(st, 3) is False, "con 2 requeridos y 1 a rango no castea"
    print("OK gate: enemigo lejano ya no bloquea; cast_min_in_range se respeta")


def test_gate_strict_requires_all_in_range() -> None:
    """Por defecto (require_all_close) no lanza hasta que TODOS los de pantalla
    esten a rango de ataque; panic lo salta."""
    bb = Blackboard()
    st = make_state()
    st.pokemon_pos = (100, 100, 7)
    st.attacking_name = "E1"
    st.moves = [{"key": "7", "aoe": True, "effect": "damage", "pct": 100, "name": "Air Vortex"}]
    st.creatures = [
        Creature(cid=1, name="E1", pos=Vec3(101, 100, 7), hp_pct=100,
                 kind=CreatureKind.MONSTER, is_wild=True),
        Creature(cid=2, name="E2", pos=Vec3(110, 100, 7), hp_pct=100,
                 kind=CreatureKind.MONSTER, is_wild=True),
    ]
    cb = CombatBehavior({"lure_aoe": True, "ready_pct": 100, "cooldown": 0.0,
                         "attack_range": 3, "engage_radius": 0, "require_all_close": True}, {})
    inp = RecInput()
    cb._aoe_fight(st, bb, inp, st.creatures)
    assert inp.skills == [], f"un enemigo lejano debe bloquear: {inp.skills}"
    # HP panico del pokemon -> lanza igual
    st.pokemon_hp = 5
    cb2 = CombatBehavior({"lure_aoe": True, "ready_pct": 100, "cooldown": 0.0,
                          "attack_range": 3, "engage_radius": 0, "require_all_close": True,
                          "panic_hp": 25}, {})
    inp2 = RecInput()
    cb2._aoe_fight(st, bb, inp2, st.creatures)
    assert inp2.skills == ["7"], f"en panico debe lanzar: {inp2.skills}"
    print("OK gate: estricto (todos a rango) salvo HP panico")


def test_panic_source_and_skills() -> None:
    """Panico configurable: origen del HP (summon/activo/min) y que lanza."""
    def build():
        st = make_state()
        st.pokemon_pos = (100, 100, 7)
        st.attacking_name = "E1"
        st.moves = [
            {"key": "7", "aoe": True, "effect": "damage/debuff", "pct": 100, "name": "Air Vortex"},
            {"key": "1", "aoe": False, "effect": "damage", "pct": 100, "name": "Peck"},
            {"key": "9", "aoe": False, "effect": "buff/nevermiss", "pct": 100, "name": "Confide"},
        ]
        st.creatures = [
            Creature(cid=1, name="E1", pos=Vec3(101, 100, 7), hp_pct=100,
                     kind=CreatureKind.MONSTER, is_wild=True),
            Creature(cid=2, name="E2", pos=Vec3(108, 100, 7), hp_pct=100,
                     kind=CreatureKind.MONSTER, is_wild=True),
        ]
        return st

    base = {"lure_aoe": True, "ready_pct": 100, "cooldown": 0.0, "attack_range": 3,
            "engage_radius": 0, "require_all_close": True, "panic_hp": 25}

    # origen = summon: summon sano -> NO panico -> el gate estricto bloquea (E2 lejos)
    st = build(); st.pokemon_hp = 100; st.party = [Pokemon(slot=1, name="P", hp_pct=10, active=True)]
    cb = CombatBehavior({**base, "panic_hp_source": "summon"}, {})
    inp = RecInput(); cb._aoe_fight(st, Blackboard(), inp, st.creatures)
    assert inp.skills == [], f"summon sano no debe entrar en panico: {inp.skills}"

    # origen = active: el activo esta a 10 -> panico -> lanza saltando el gate
    cb = CombatBehavior({**base, "panic_hp_source": "active"}, {})
    inp = RecInput(); cb._aoe_fight(st, Blackboard(), inp, st.creatures)
    assert inp.skills == ["7"], f"activo bajo debe disparar panico: {inp.skills}"

    # origen = min -> tambien dispara
    cb = CombatBehavior({**base, "panic_hp_source": "min"}, {})
    inp = RecInput(); cb._aoe_fight(st, Blackboard(), inp, st.creatures)
    assert inp.skills == ["7"], f"min debe disparar panico: {inp.skills}"

    # skills = all (por defecto): AoE + dano simple, sin buff
    st.pokemon_hp = 5; st.party = []
    cb = CombatBehavior({**base, "panic_skills": "all"}, {})
    inp = RecInput(); bb = Blackboard()
    cb._aoe_fight(st, bb, inp, st.creatures); cb._aoe_fight(st, bb, inp, st.creatures)
    cb._aoe_fight(st, bb, inp, st.creatures)
    assert inp.skills == ["7", "1"], f"'all' no debe lanzar buff: {inp.skills}"

    # skills = everything: incluye buff / no-dano
    cb = CombatBehavior({**base, "panic_skills": "everything"}, {})
    inp = RecInput(); bb = Blackboard()
    cb._aoe_fight(st, bb, inp, st.creatures); cb._aoe_fight(st, bb, inp, st.creatures)
    cb._aoe_fight(st, bb, inp, st.creatures)
    assert inp.skills == ["7", "1", "9"], f"'everything' debe incluir buff: {inp.skills}"

    # skills = combo: en panico respeta el combo configurado
    st.lure_order = ["1"]
    cb = CombatBehavior({**base, "panic_skills": "combo"}, {})
    inp = RecInput(); cb._aoe_fight(st, Blackboard(), inp, st.creatures)
    assert inp.skills == ["1"], f"'combo' debe lanzar solo el combo: {inp.skills}"
    # y sin 'combo', el panico ignora el combo (usa AoE + dano)
    cb = CombatBehavior({**base, "panic_skills": "all"}, {})
    inp = RecInput(); cb._aoe_fight(st, Blackboard(), inp, st.creatures)
    assert inp.skills == ["7"], f"'all' ignora el combo en panico: {inp.skills}"
    print("OK panico: origen de HP (summon/active/min) y skills (all/everything/combo)")


def test_revives_left() -> None:
    """revives_left lee bag_counts (id->cantidad); None si no hay datos fiables."""
    rb = ReviveBehavior({"item": 2269}, {})
    st = make_state()
    st.bag_counts = {"2269": 5, "123": 2}
    assert rb.revives_left(st) == 5, rb.revives_left(st)
    st.bag_counts = {"123": 2}
    assert rb.revives_left(st) == 0, rb.revives_left(st)
    st.bag_counts = {}
    assert rb.revives_left(st) is None, rb.revives_left(st)
    # clave numerica (sin pasar por JSON)
    st.bag_counts = {2269: 3}
    assert rb.revives_left(st) == 3, rb.revives_left(st)
    print("OK revives_left (cuenta desde bag_counts, None si no fiable)")


def test_aoe_cast_order_respects_combo() -> None:
    bb = Blackboard()
    st = make_state()
    st.pokemon_pos = (100, 100, 7)
    st.attacking_name = "E1"
    st.skill_order = ["3", "7", "2"]
    st.moves = [
        {"key": "1", "aoe": False, "effect": "damage", "pct": 100, "name": "Peck"},
        {"key": "2", "aoe": False, "effect": "damage", "pct": 100, "name": "Feather"},
        {"key": "3", "aoe": True, "effect": "damage", "pct": 100, "name": "Drill"},
        {"key": "7", "aoe": True, "effect": "damage/debuff", "pct": 100, "name": "Air Vortex"},
    ]
    st.creatures = [
        Creature(cid=1, name="E1", pos=Vec3(101, 100, 7), hp_pct=100,
                 kind=CreatureKind.MONSTER, is_wild=True),
        Creature(cid=2, name="E2", pos=Vec3(100, 101, 7), hp_pct=100,
                 kind=CreatureKind.MONSTER, is_wild=True),
    ]
    cb = CombatBehavior({"lure_aoe": True, "ready_pct": 100, "cooldown": 0.0,
                         "attack_range": 3, "cast_min_in_range": 2, "lure_use_single": True}, {})
    inp = RecInput()
    cb._aoe_fight(st, bb, inp, st.creatures)
    assert inp.skills == ["3"], f"primer skill del combo: {inp.skills}"
    # no repite la misma (guard) y sigue el combo con la siguiente lista
    cb._aoe_fight(st, bb, inp, st.creatures)
    assert inp.skills == ["3", "7"], f"segundo skill del combo: {inp.skills}"
    print("OK orden: el combo se respeta (AoE y dano) en el lure")


def test_route_calls_pokemon() -> None:
    """Al iniciar la ruta, el bot saca (callslot) el pokemon elegido, salvo si ya
    esta out (entonces lo deja)."""
    bb = Blackboard()
    st = make_state()
    st.party = [Pokemon(slot=2, name="Shiny fearow", hp_pct=100)]
    st.active_pokemon_name = ""
    rb = RouteBehavior({"enabled": True}, {}, Route.from_config([[110, 100, 7]]))
    rb.set_pokemon("Shiny fearow", None)
    inp = RecInput()
    rb.act(st, bb, inp)
    assert inp.calls == [2], f"debe sacar el slot 2: {inp.calls}"
    # el slot ya esta out -> NO clickea
    st.party[0].active = True
    rb2 = RouteBehavior({"enabled": True}, {}, Route.from_config([[110, 100, 7]]))
    rb2.set_pokemon("Shiny fearow", None)
    inp2 = RecInput()
    rb2.act(st, bb, inp2)
    assert inp2.calls == [], f"si el slot ya esta out no debe clickear: {inp2.calls}"
    # activo detectado por nombre -> no lo saca
    st.party[0].active = False
    st.active_pokemon_name = "Shiny fearow"
    rb3 = RouteBehavior({"enabled": True}, {}, Route.from_config([[110, 100, 7]]))
    rb3.set_pokemon("Shiny fearow", None)
    inp3 = RecInput()
    rb3.act(st, bb, inp3)
    assert inp3.calls == [], f"si ya esta activo por nombre no debe llamar: {inp3.calls}"
    # sin nombre (no capturado): usa el slot de reserva
    st.active_pokemon_name = ""
    rb4 = RouteBehavior({"enabled": True}, {}, Route.from_config([[110, 100, 7]]))
    rb4.set_pokemon("", 4)
    inp4 = RecInput()
    rb4.act(st, bb, inp4)
    assert inp4.calls == [4], f"fallback por slot: {inp4.calls}"
    print("OK ruta: saca el pokemon elegido al iniciar, salvo si ya esta out")


def test_revive_uses_route_slot() -> None:
    """El revive se vincula al slot del Pokemon elegido para la ruta."""
    rb = ReviveBehavior({"enabled": True, "slot": 3}, {})
    assert rb._slot() == 3, "sin seleccion usa revive.slot"
    rb.set_route_slot(2)
    assert rb._slot() == 2, "con seleccion debe usar el slot de la ruta"
    # el chequeo de HP tambien apunta al slot de la ruta
    st = make_state()
    st.party = [Pokemon(slot=2, name="X", hp_pct=0), Pokemon(slot=3, name="Y", hp_pct=100)]
    assert rb._slot_hp(st) == 0, "debe mirar el HP del slot de la ruta"
    rb.set_route_slot(None)
    assert rb._slot() == 3, "al quitar la seleccion vuelve a config"
    print("OK revive: usa el slot del Pokemon de la ruta")


def test_revive_urgent_when_dead() -> None:
    """Pokemon debilitado: revive inmediato, saltando esperas y enemigos."""
    bb = Blackboard()
    st = make_state()
    st.party = [Pokemon(slot=3, name="X", hp_pct=0)]
    st.moves = [{"key": "7", "aoe": True, "effect": "damage", "pct": 100}]
    st.creatures = [Creature(cid=1, name="E", pos=Vec3(101, 100, 7), hp_pct=100,
                             kind=CreatureKind.MONSTER, is_wild=True)]
    rb = ReviveBehavior({"enabled": True, "slot": 3, "after_aoe_wait_secs": 5.0,
                         "min_interval_secs": 999.0, "on_stun": True}, {})
    bb.notes["last_aoe_at"] = time.time()    # dentro de after_aoe
    bb.notes["revive_done"] = time.time()    # dentro de min_interval
    assert rb.evaluate(st, bb) is True, "con el pokemon debilitado debe revivir ya"
    # y no debe arrancar la espera 'first'
    rb.act(st, bb, RecInput())
    assert bb.notes["revive_rec"]["first"] is False, "estando debilitado no hay espera 'first'"
    print("OK revive: urgente cuando el pokemon esta debilitado")


def test_loot_yields_when_revive_urgent() -> None:
    bb = Blackboard()
    st = make_state(defeated=[{"id": "1", "name": "Rattata", "x": 100, "y": 100, "z": 7}])
    bb.update_corpses(st)
    bb.notes["revive_urgent"] = True
    loot = LootBehavior({"enabled": True, "reach": 20}, {})
    assert loot.evaluate(st, bb) is False, "loot debe ceder ante un revive urgente"
    print("OK revive: loot cede cuando el revive es urgente")


def _revive_cfg(**kw):
    base = {"enabled": True, "slot": 3, "after_aoe_wait_secs": 0.0,
            "min_interval_secs": 0.0, "fast_min_interval_secs": 0.0,
            "stun_min_interval_secs": 0.0, "stun_secs": 2.5, "on_stun": True}
    base.update(kw)
    return base


def test_revive_only_when_in_range_cleared() -> None:
    """Un enemigo fuera del rango de ataque no bloquea; uno vivo dentro, si."""
    bb = Blackboard()
    st = make_state()
    st.pokemon_pos = (100, 100, 7)
    st.moves = [{"key": "5", "aoe": True, "effect": "damage", "pct": 0},
                {"key": "6", "aoe": True, "effect": "damage", "pct": 100}]
    st.creatures = [Creature(cid=1, name="E", pos=Vec3(108, 100, 7), hp_pct=100,
                             kind=CreatureKind.MONSTER, is_wild=True)]  # dist 8
    rb = ReviveBehavior(_revive_cfg(), {}, {"attack_range": 3})
    assert rb._alive_in_range(st) == 0, "el enemigo lejano no cuenta"
    assert rb.evaluate(st, bb) is True, "sin vivos en rango -> revive"
    st.creatures.append(Creature(cid=2, name="E2", pos=Vec3(101, 100, 7), hp_pct=100,
                                 kind=CreatureKind.MONSTER, is_wild=True))  # dist 1
    assert rb._alive_in_range(st) == 1
    assert rb.evaluate(st, Blackboard()) is False, "vivo en rango sin stun -> no revive"
    print("OK revive: solo con el rango de ataque despejado (o stun)")


def test_revive_stun_bypass() -> None:
    bb = Blackboard()
    st = make_state()
    st.pokemon_pos = (100, 100, 7)
    st.moves = [{"key": "5", "aoe": True, "effect": "damage/stun", "pct": 0},
                {"key": "6", "aoe": True, "effect": "damage", "pct": 100}]
    st.creatures = [Creature(cid=1, name="E", pos=Vec3(101, 100, 7), hp_pct=100,
                             kind=CreatureKind.MONSTER, is_wild=True)]
    rb = ReviveBehavior(_revive_cfg(), {}, {"attack_range": 3})
    assert rb.evaluate(st, Blackboard()) is False, "sin stun reciente y con skills listas, no"
    bb = Blackboard()
    bb.notes["last_stun_at"] = time.time()
    assert rb.evaluate(st, bb) is True, "vivos en rango pero stuneados -> revive"
    print("OK revive: revive con vivos en rango si estan stuneados")


def test_revive_no_stun_only_trigger() -> None:
    """Un stun reciente NO debe disparar revive si el pokemon esta sano y el
    combo listo (regresion: en panico revivia una y otra vez sin necesidad)."""
    rb = ReviveBehavior(_revive_cfg(), {}, {"attack_range": 3})
    # combo listo + skill de stun reciente + enemigo vivo en rango
    st = make_state()
    st.pokemon_pos = (100, 100, 7)
    st.skill_order = ["5"]
    st.moves = [{"key": "5", "aoe": False, "effect": "damage/stun", "pct": 100}]
    st.creatures = [Creature(cid=1, name="E", pos=Vec3(101, 100, 7), hp_pct=100,
                             kind=CreatureKind.MONSTER, is_wild=True)]
    bb = Blackboard()
    bb.notes["last_stun_at"] = time.time()
    assert rb._stun_active(st, bb) is True
    assert rb._combo_status(st) == (False, False), "el combo debe estar listo"
    assert rb.evaluate(st, bb) is False, "stun solo, sin necesidad -> no revive"
    assert rb.urgent(st, bb) is False, "stun solo no es urgente (loot/captura no ceden)"
    # con el combo en cooldown (hace falta) + stun -> si revive
    st.moves = [{"key": "5", "aoe": False, "effect": "damage/stun", "pct": 0},
                {"key": "6", "aoe": False, "effect": "damage", "pct": 100}]
    st.skill_order = ["5", "6"]
    bb2 = Blackboard()
    bb2.notes["last_stun_at"] = time.time()
    assert rb._combo_status(st)[0] is True
    assert rb.evaluate(st, bb2) is True, "necesita reset + stun -> revive"
    print("OK revive: un stun sin necesidad (combo listo) no dispara revive")


def test_revive_exhausted_fallback() -> None:
    """Sin skills del combo: revive igual y rapido, aunque haya vivos en rango."""
    bb = Blackboard()
    st = make_state()
    st.pokemon_pos = (100, 100, 7)
    st.moves = [{"key": "5", "aoe": True, "effect": "damage", "pct": 0},
                {"key": "6", "aoe": True, "effect": "damage", "pct": 0}]
    st.creatures = [Creature(cid=1, name="E", pos=Vec3(101, 100, 7), hp_pct=100,
                             kind=CreatureKind.MONSTER, is_wild=True)]
    rb = ReviveBehavior(_revive_cfg(), {}, {"attack_range": 3})
    assert rb.evaluate(st, bb) is True, "sin skills del combo -> revive aunque haya vivos"
    assert bb.notes.get("revive_fast") is True, "el fallback debe ser rapido"
    print("OK revive: fallback rapido al agotarse el combo")


def test_buff_respects_gate() -> None:
    """Con `buff_gate=combat`, el buff no sale si no estan todos a rango."""
    bb = Blackboard()
    st = make_state()
    st.pokemon_pos = (100, 100, 7)
    st.moves = [{"key": "9", "aoe": False, "effect": "buff/nevermiss", "pct": 100, "name": "Confide"},
                {"key": "7", "aoe": True, "effect": "damage", "pct": 100, "name": "Air Vortex"}]
    # enemigos a distancia 1, 4 y 7 -> no todos a rango 3
    st.creatures = [Creature(cid=i, name=f"E{i}", pos=Vec3(101 + i * 3, 100, 7), hp_pct=100,
                             kind=CreatureKind.MONSTER, is_wild=True) for i in range(3)]
    cb = CombatBehavior({"buff_on_screen": True, "buff_visible_min": 3,
                         "buff_gate": "combat", "require_all_close": True, "attack_range": 3,
                         "ready_pct": 100, "cooldown": 0.0}, {})
    inp = RecInput()
    cb.act(st, bb, inp)
    assert "9" not in inp.skills, f"el buff no debe salir si no todos a rango: {inp.skills}"
    print("OK buff: con gate de combate no sale si no todos a rango")


def test_buff_gate() -> None:
    """buff_gate: 'screen' (def) no exige distancia; 'range'/'combat' si."""
    def skills(**kw):
        bb = Blackboard()
        st = make_state()
        st.pokemon_pos = (100, 100, 7)
        st.moves = [{"key": "9", "aoe": False, "effect": "buff/nevermiss", "pct": 100},
                    {"key": "7", "aoe": True, "effect": "damage", "pct": 100}]
        # 4 enemigos en pantalla, uno a distancia 4 (fuera de attack_range 3)
        st.creatures = [Creature(cid=i, name=f"E{i}", pos=Vec3(100 + d, 100, 7), hp_pct=100,
                                 kind=CreatureKind.MONSTER, is_wild=True)
                        for i, d in enumerate([1, 2, 3, 4])]
        cfg = {"enabled": True, "buff_on_screen": True, "buff_visible_min": 4,
               "require_all_close": True, "attack_range": 3, "ready_pct": 100, "cooldown": 0.0}
        cfg.update(kw)
        cb = CombatBehavior(cfg, {})
        inp = RecInput()
        for _ in range(3):
            if cb.evaluate(st, bb):
                cb.act(st, bb, inp)
        return inp.skills

    assert "9" in skills(buff_gate="screen"), "screen: sale aunque haya uno lejos"
    assert "9" not in skills(buff_gate="range"), "range: no sale con uno fuera de rango"
    assert "9" not in skills(buff_gate="combat"), "combat: usa el gate de combate"
    print("OK buff: buff_gate screen/range/combat")


def test_revive_no_double_after_lag() -> None:
    """Tras completar un revive, no arranca otro por lag; se rearma al recuperarse
    o tras revive_retry_secs."""
    st = make_state()
    st.pokemon_pos = (100, 100, 7)
    st.moves = [{"key": "7", "aoe": True, "effect": "damage", "pct": 0}]  # combo agotado
    st.party = [Pokemon(slot=3, name="X", hp_pct=0)]                      # debilitado
    rb = ReviveBehavior({"enabled": True, "slot": 3, "revive_retry_secs": 3.0}, {},
                        {"attack_range": 3})
    bb = Blackboard()
    bb.notes["revive_ready"] = False          # acaba de completar un revive
    bb.notes["revive_done"] = time.time()
    assert rb.evaluate(st, bb) is False, "no debe revivir 2 veces por lag"
    # fallback: si nunca se recupera, reintenta tras revive_retry_secs
    bb.notes["revive_done"] = time.time() - 10.0
    assert rb.evaluate(st, bb) is True, "reintento tras revive_retry_secs"
    # recuperado (vivo y combo listo) -> rearma y luego revive con 'need'
    bb2 = Blackboard()
    bb2.notes["revive_ready"] = False
    bb2.notes["revive_done"] = time.time()
    st.party = [Pokemon(slot=3, name="X", hp_pct=100)]
    st.moves = [{"key": "7", "aoe": True, "effect": "damage", "pct": 100}]
    rb.evaluate(st, bb2)
    assert bb2.notes.get("revive_ready") is True, "debe rearmarse al recuperarse"
    st.moves = [{"key": "7", "aoe": True, "effect": "damage", "pct": 0}]
    bb2.notes["revive_done"] = time.time() - 5.0   # el revive ya fue hace rato
    assert rb.evaluate(st, bb2) is True, "con 'need' de nuevo debe revivir"
    print("OK revive: no se duplica por lag; se rearma al recuperarse")


def test_lure_summon_arrival_grace() -> None:
    """_summon_arrived: tolerancia de 1 tile o gracia temporal (no bloquear)."""
    st = make_state()
    st.pokemon_pos = (101, 100, 7)   # 1 tile del objetivo -> llegado
    cb = CombatBehavior({"lure_summon": True, "summon_arrive_tolerance": 1,
                         "summon_grace_secs": 3.0}, {})
    bb = Blackboard()
    bb.notes["lure_order_target"] = (100, 100, 7)
    bb.notes["lure_order_at"] = time.time()
    assert cb._summon_arrived(st, bb) is True, "a 1 tile debe contar como llegado"
    st.pokemon_pos = (105, 100, 7)   # lejos y dentro de la gracia
    assert cb._summon_arrived(st, bb) is False
    bb.notes["lure_order_at"] = time.time() - 10.0   # paso la gracia
    assert cb._summon_arrived(st, bb) is True, "tras la gracia no debe bloquear"
    st.pokemon_pos = None
    bb.notes["lure_order_at"] = time.time()
    assert cb._summon_arrived(st, bb) is False, "sin posicion y sin gracia -> esperar"
    print("OK lure: llegada del summon con tolerancia + gracia")


def test_summon_never_on_player() -> None:
    """El `order` del summon nunca apunta a 1 tile del personaje (minimo 2)."""
    st = make_state(px=100, py=100)
    otmm = _FakeOtmm()
    for rad in (1, 2, 4, 8):
        t = summon_near_tile(st, otmm, rad)
        assert t is not None, f"deberia encontrar tile con radio {rad}"
        d = max(abs(t.x - 100), abs(t.y - 100))
        assert d >= 2, f"el order debe estar a >=2 tiles del player, no {d} (radio {rad})"
    print("OK summon: el order nunca apunta a 1 tile del personaje (minimo 2)")


def test_route_start_idle_toggle() -> None:
    """El idle de inicio se puede activar/desactivar (start_idle_enabled)."""
    st = make_state(px=100, py=100)
    wps = [[100, 100, 7], [110, 100, 7]]
    rb = RouteBehavior({"enabled": True, "start_idle_enabled": True, "start_idle_seconds": 99}, {},
                       Route.from_config(wps, loop=True))
    rb._looped = True
    rb.act(st, Blackboard(), RecInput())
    assert rb._idle_until > time.time(), "con idle activado debe esperar"
    rb2 = RouteBehavior({"enabled": True, "start_idle_enabled": False, "start_idle_seconds": 99}, {},
                        Route.from_config(wps, loop=True))
    rb2._looped = True
    rb2.act(st, Blackboard(), RecInput())
    assert rb2._idle_until == 0.0, "con idle desactivado no debe esperar"
    print("OK ruta: idle de inicio activable/desactivable")


def test_lure_gather_timeout() -> None:
    """Sin juntar X, tras lure_gather_timeout pasa a hold; 0 = sin tope; se
    resetea al quedarse sin enemigos."""
    st = make_state()
    st.pokemon_pos = (100, 100, 7)
    st.moves = [{"key": "7", "aoe": True, "effect": "damage", "pct": 100, "name": "Air Vortex"}]
    st.creatures = [Creature(cid=1, name="E1", pos=Vec3(105, 100, 7), hp_pct=100,
                             kind=CreatureKind.MONSTER, is_wild=True)]
    cb = CombatBehavior({"lure_aoe": True, "lure_visible_min": 5, "lure_gather_timeout": 10.0}, {})
    bb = Blackboard()
    assert cb.evaluate(st, bb) is False, "con 1 (<X) sigue la ruta"
    assert bb.notes.get("lure_gather_start"), "debe arrancar el contador con el primer enemigo"
    assert cb.evaluate(st, bb) is False, "aun no pasaron los 10s -> sigue"
    bb.notes["lure_gather_start"] = time.time() - 11.0
    assert cb.evaluate(st, bb) is True, "pasados los 10s -> hold"
    assert bb.notes.get("lure_state") == "hold"
    # tope 0 = sin tope
    cb0 = CombatBehavior({"lure_aoe": True, "lure_visible_min": 5, "lure_gather_timeout": 0}, {})
    bb0 = Blackboard()
    bb0.notes["lure_gather_start"] = time.time() - 100
    assert cb0.evaluate(st, bb0) is False, "con tope 0 no debe forzar hold"
    # sin enemigos -> se resetea
    st0 = make_state()
    st0.pokemon_pos = (100, 100, 7)
    st0.moves = st.moves
    bb2 = Blackboard()
    bb2.notes["lure_gather_start"] = time.time()
    cb.evaluate(st0, bb2)
    assert bb2.notes.get("lure_gather_start") is None, "sin enemigos se resetea"
    print("OK lure: tope de tiempo (10s) pasa a hold; 0 = sin tope; se resetea sin enemigos")


def test_aoe_cooldown_spacing() -> None:
    """Con cooldown>0 no se lanzan dos skills en el mismo instante."""
    bb = Blackboard()
    st = make_state()
    st.pokemon_pos = (100, 100, 7)
    st.moves = [{"key": "7", "aoe": True, "effect": "damage", "pct": 100},
                {"key": "8", "aoe": True, "effect": "damage", "pct": 100}]
    st.creatures = [Creature(cid=1, name="E1", pos=Vec3(101, 100, 7), hp_pct=100,
                             kind=CreatureKind.MONSTER, is_wild=True)]
    cb = CombatBehavior({"lure_aoe": True, "ready_pct": 100, "cooldown": 0.5,
                         "attack_range": 3, "require_all_close": False,
                         "cast_min_in_range": 1}, {})
    inp = RecInput()
    cb._aoe_fight(st, bb, inp, st.creatures)
    cb._aoe_fight(st, bb, inp, st.creatures)
    assert len(inp.skills) == 1, f"con cooldown 0.5 solo debe lanzar 1: {inp.skills}"
    print("OK skills: cooldown separa los lanzamientos")


def test_luabridge_state_cache() -> None:
    """El estado se cachea por mtime (no reparsea si no cambio)."""
    import json as _json
    import os as _os
    import tempfile as _tmp
    d = _tmp.mkdtemp()
    sf = _os.path.join(d, "state.json")
    with open(sf, "w") as fh:
        _json.dump({"connected": True, "name": "A"}, fh)
    b = LuaBridge({"state_file": sf})
    a = b.read()
    assert a.get("name") == "A"
    assert b.read() is a, "sin cambiar mtime debe devolver el cacheado"
    time.sleep(0.02)
    with open(sf, "w") as fh:
        _json.dump({"connected": True, "name": "B"}, fh)
    assert b.read().get("name") == "B", "al cambiar el fichero debe releer"
    print("OK lua_bridge: cache de estado por mtime")


def test_capture_counts_once_per_corpse() -> None:
    """El contador 'ball' cuenta 1 por cuerpo (intento), no 1 por throw."""
    bb = Blackboard()
    st = make_state()
    st.creatures = [Creature(cid=7, name="Shiny X", pos=Vec3(100, 100, 7), hp_pct=100,
                             kind=CreatureKind.MONSTER, shiny=True)]
    st.defeated = [{"id": "7", "name": "Shiny X", "x": 100, "y": 100, "z": 7}]
    bb.update_corpses(st)
    bb.notes.setdefault("looted_ids", set()).add("7")
    st.creatures = []
    cap = CaptureBehavior({"enabled": True, "catch_all": True, "ball_item": 2652,
                           "interval": 0.0, "max_throws": 5, "range": 4}, {})
    assert cap.evaluate(st, bb) is True
    inp = RecInput()
    cap.act(st, bb, inp)
    cap.act(st, bb, inp)   # varios throws al mismo cuerpo
    assert bb.counters.get("ball") == 1, f"debe contar 1 por cuerpo: {bb.counters}"
    assert len(inp.ball_throws) >= 2, "si lanza varias balls"
    print("OK capture: cuenta 1 por cuerpo (no por throw)")


def test_crisis_disabled() -> None:
    bb = Blackboard()
    st = make_state()
    st.connected = False
    assert CrisisBehavior({"enabled": True}, {}).evaluate(st, bb) is True
    assert CrisisBehavior({"enabled": False}, {}).evaluate(st, bb) is False
    print("OK crisis desactivada no actua (ni con desconexion)")


class _FakeBridge:
    shiny_file = ""
    pokemon_skills_file = ""
    ignore_file = ""

    def __init__(self, data: dict) -> None:
        self._data = data

    def read(self) -> dict:
        return self._data


def test_spec_id_parsing() -> None:
    data = {
        "connected": True, "name": "P", "x": 1, "y": 2, "z": 7, "hp": 10, "maxhp": 10,
        "creatures": [
            {"id": "spec3", "name": "Raro", "x": 3, "y": 3, "z": 7, "hp": 100, "monster": True},
            {"id": "42", "name": "Ok", "x": 4, "y": 4, "z": 7, "hp": 100, "monster": True},
        ],
    }
    st = LuaStateSource(_FakeBridge(data)).read_state()
    assert st.connected, "un id no numerico no debe tumbar la lectura"
    assert len(st.creatures) == 2
    print("OK id no numerico ('spec3') se parsea sin romper")


def main() -> int:
    test_loot_adjacent()
    test_loot_no_duplicates()
    test_capture_shiny_after_loot()
    test_capture_nonshiny_off()
    test_fight_mode_lure()
    test_fight_mode_ignored_without_lure()
    test_revive_atomic()
    test_revive_rec_not_cancelled()
    test_revive_first_flag()
    test_summon_picks_free_tile()
    test_summon_never_on_player()
    test_revive_waits_after_aoe()
    test_lure_summon_orders()
    test_capture_uses_lua_ball()
    test_capture_range()
    test_capture_counts_once_per_corpse()
    test_buff_on_screen()
    test_buff_respects_gate()
    test_buff_gate()
    test_lure_combo_custom()
    test_lure_idle_to_hold()
    test_lure_hold_requires_all_in_range()
    test_lure_fight_returns_to_hold()
    test_lure_gather_timeout()
    test_aoe_cooldown_spacing()
    test_luabridge_state_cache()
    test_lure_summon_arrival_grace()
    test_route_calls_pokemon()
    test_route_start_idle_toggle()
    test_revive_uses_route_slot()
    test_revive_urgent_when_dead()
    test_loot_yields_when_revive_urgent()
    test_revive_only_when_in_range_cleared()
    test_revive_stun_bypass()
    test_revive_no_stun_only_trigger()
    test_revive_exhausted_fallback()
    test_revive_no_double_after_lag()
    test_gate_ignores_far_enemy()
    test_gate_strict_requires_all_in_range()
    test_panic_source_and_skills()
    test_revives_left()
    test_aoe_cast_order_respects_combo()
    test_shiny_observe()
    test_crisis_disabled()
    test_spec_id_parsing()
    print("\nBEHAVIORS OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
