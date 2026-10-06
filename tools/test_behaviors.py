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
                               LootBehavior, ReviveBehavior, SummonBehavior, RouteBehavior)
from pxg_bot.lua_bridge import LuaStateSource
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

    def set_fight_mode(self, mode: int) -> None:
        self.modes.append(int(mode))

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
    bb.notes["lure_state"] = "wait"
    cb.act(st, bb, inp)
    assert inp.orders, "en 'wait' debe ordenar el ownsummon cerca del player"
    ox, oy, oz = inp.orders[0]
    mm = _FakeOtmm()
    assert all(mm.pathable(ox + dx, oy + dy, oz)
               for dx in (-1, 0, 1) for dy in (-1, 0, 1))
    print("OK lure: ordena el ownsummon cerca (vecinos libres) antes de pelear")


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


def test_lure_group_threshold() -> None:
    """El lure no caza enemigos sueltos: necesita un grupito (lure_enter_min)."""
    st1 = make_state()
    st1.pokemon_pos = (100, 100, 7)
    st1.moves = [{"key": "7", "aoe": True, "effect": "damage", "pct": 100, "name": "Air Vortex"}]
    st1.creatures = [Creature(cid=1, name="E1", pos=Vec3(103, 100, 7), hp_pct=100,
                              kind=CreatureKind.MONSTER, is_wild=True)]
    cb = CombatBehavior({"lure_aoe": True, "lure_visible_min": 5, "lure_enter_min": 2,
                         "lure_light_wait": 1.5, "ready_pct": 100, "cooldown": 0.0}, {})
    # 1 enemigo -> NO ataca (sigue la ruta): no caza de uno en uno
    assert cb.evaluate(st1, Blackboard()) is False, "no debe cazar a un enemigo suelto"
    # 2 enemigos -> grupito -> lure ligero (pokestop+summon) y pelea en area
    st2 = make_state()
    st2.pokemon_pos = (100, 100, 7)
    st2.moves = st1.moves
    st2.creatures = st1.creatures + [
        Creature(cid=2, name="E2", pos=Vec3(104, 100, 7), hp_pct=100,
                 kind=CreatureKind.MONSTER, is_wild=True),
    ]
    bb2 = Blackboard()
    assert cb.evaluate(st2, bb2) is True, "con 2 debe entrar en lure ligero"
    assert bb2.notes.get("lure_state") == "wait"
    assert bb2.notes.get("lure_light") is True
    # configurable: con lure_enter_min=3, 2 enemigos ya no bastan
    cb3 = CombatBehavior({"lure_aoe": True, "lure_visible_min": 5, "lure_enter_min": 3}, {})
    assert cb3.evaluate(st2, Blackboard()) is False, "lure_enter_min debe respetarse"
    print("OK lure: no caza sueltos, necesita grupito (lure_enter_min)")


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
    """Al iniciar la ruta, el bot saca (callslot) el pokemon elegido."""
    bb = Blackboard()
    st = make_state()
    st.party = [Pokemon(slot=2, name="Shiny fearow", hp_pct=100)]
    st.active_pokemon_name = ""
    rb = RouteBehavior({"enabled": True}, {}, Route.from_config([[110, 100, 7]]))
    rb.set_pokemon("Shiny fearow", None)
    inp = RecInput()
    rb.act(st, bb, inp)
    assert inp.calls == [2], f"debe sacar el slot 2: {inp.calls}"
    # ya esta activo -> no lo saca (evita retirarlo)
    st.active_pokemon_name = "Shiny fearow"
    rb2 = RouteBehavior({"enabled": True}, {}, Route.from_config([[110, 100, 7]]))
    rb2.set_pokemon("Shiny fearow", None)
    inp2 = RecInput()
    rb2.act(st, bb, inp2)
    assert inp2.calls == [], f"si ya esta activo no debe llamar: {inp2.calls}"
    # sin nombre (no capturado): usa el slot de reserva
    st.active_pokemon_name = ""
    rb3 = RouteBehavior({"enabled": True}, {}, Route.from_config([[110, 100, 7]]))
    rb3.set_pokemon("", 4)
    inp3 = RecInput()
    rb3.act(st, bb, inp3)
    assert inp3.calls == [4], f"fallback por slot: {inp3.calls}"
    print("OK ruta: saca el pokemon elegido al iniciar (por nombre o slot)")


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
    """El buff respeta el gate: no sale si no estan todos a rango."""
    bb = Blackboard()
    st = make_state()
    st.pokemon_pos = (100, 100, 7)
    st.moves = [{"key": "9", "aoe": False, "effect": "buff/nevermiss", "pct": 100, "name": "Confide"},
                {"key": "7", "aoe": True, "effect": "damage", "pct": 100, "name": "Air Vortex"}]
    # enemigos a distancia 1, 4 y 7 -> no todos a rango 3
    st.creatures = [Creature(cid=i, name=f"E{i}", pos=Vec3(101 + i * 3, 100, 7), hp_pct=100,
                             kind=CreatureKind.MONSTER, is_wild=True) for i in range(3)]
    cb = CombatBehavior({"buff_on_screen": True, "buff_visible_min": 3,
                         "require_all_close": True, "attack_range": 3,
                         "ready_pct": 100, "cooldown": 0.0}, {})
    inp = RecInput()
    cb.act(st, bb, inp)
    assert "9" not in inp.skills, f"el buff no debe salir si no todos a rango: {inp.skills}"
    print("OK buff: respeta el gate (no sale si no todos a rango)")


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
    test_revive_waits_after_aoe()
    test_lure_summon_orders()
    test_capture_uses_lua_ball()
    test_buff_on_screen()
    test_buff_respects_gate()
    test_lure_combo_custom()
    test_lure_group_threshold()
    test_route_calls_pokemon()
    test_revive_uses_route_slot()
    test_revive_urgent_when_dead()
    test_loot_yields_when_revive_urgent()
    test_revive_only_when_in_range_cleared()
    test_revive_stun_bypass()
    test_revive_exhausted_fallback()
    test_gate_ignores_far_enemy()
    test_gate_strict_requires_all_in_range()
    test_aoe_cast_order_respects_combo()
    test_shiny_observe()
    test_crisis_disabled()
    test_spec_id_parsing()
    print("\nBEHAVIORS OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
