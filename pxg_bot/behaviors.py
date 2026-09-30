from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .models import Creature, CreatureKind, GameState, Vec3
from .pathfinding import Route


@dataclass
class Blackboard:
    route: Optional[Route] = None
    target_cid: Optional[int] = None
    paused: bool = False
    stop_reason: str = ""
    human_seen: bool = False
    action_times: dict = field(default_factory=dict)
    notes: dict = field(default_factory=dict)
    humanizer: object = None

    def ready(self, name: str, cooldown: float) -> bool:
        if self.humanizer is not None:
            cooldown = self.humanizer.jitter_up(cooldown, "cooldown_jitter_pct")
        return time.time() - self.action_times.get(name, 0.0) >= cooldown

    def mark(self, name: str) -> None:
        self.action_times[name] = time.time()


def nearest(state: GameState, predicate: Callable[[Creature], bool], max_range: Optional[int] = None):
    best = None
    best_d = 1 << 30
    for c in state.creatures:
        if not predicate(c):
            continue
        d = state.player.pos.distance(c.pos)
        if max_range is not None and d > max_range:
            continue
        if d < best_d:
            best_d = d
            best = c
    return best


def is_enemy(c: Creature) -> bool:
    return c.attackable and not c.is_player


class Behavior:
    name = "behavior"
    priority = 0

    def __init__(self, cfg: dict, settings: dict):
        self.cfg = cfg or {}
        self.settings = settings or {}

    def evaluate(self, state: GameState, bb: Blackboard) -> bool:
        return False

    def act(self, state: GameState, bb: Blackboard, inp) -> None:
        raise NotImplementedError


class CrisisBehavior(Behavior):
    name = "crisis"
    priority = 100

    def evaluate(self, state: GameState, bb: Blackboard) -> bool:
        s = self.cfg
        if not state.connected:
            bb.stop_reason = "cliente desconectado"
            return True
        if s.get("pause_on_player", True):
            for c in state.creatures:
                if c.is_self:
                    continue
                if c.is_player or c.kind == CreatureKind.PLAYER:
                    if state.player.pos.distance(c.pos) <= int(s.get("player_range", 7)):
                        bb.human_seen = True
                        bb.stop_reason = "jugador/GM detectado"
                        return True
        if state.player.hp_pct <= int(s.get("panic_hp_pct", 10)):
            bb.stop_reason = "HP crítica"
            return True
        return False

    def act(self, state: GameState, bb: Blackboard, inp) -> None:
        s = self.cfg
        if not state.connected:
            if bb.ready("reconnect", float(s.get("reconnect_cooldown", 5))):
                inp.press(s.get("reconnect_key", "F1"))
                bb.mark("reconnect")
            return
        if bb.human_seen:
            bb.paused = True
            if s.get("logout_on_player"):
                inp.press(s.get("logout_key", "F12"))
            return
        threat = nearest(state, is_enemy, max_range=int(s.get("flee_detect_range", 5)))
        if threat is not None and bb.ready("flee", 0.3):
            away = Vec3(
                state.player.pos.x * 2 - threat.pos.x,
                state.player.pos.y * 2 - threat.pos.y,
                state.player.pos.z,
            )
            direction = state.player.pos.direction_to(away)
            if direction >= 0:
                inp.move(direction)
            bb.mark("flee")


class HealingBehavior(Behavior):
    name = "heal"
    priority = 90

    def evaluate(self, state: GameState, bb: Blackboard) -> bool:
        if bb.paused or not self.cfg.get("enabled", True):
            return False
        s = self.cfg
        cd = float(s.get("cooldown", 1.0))
        if state.player.hp_pct <= int(s.get("player_hp_pct", 60)) and bb.ready("player_heal", cd):
            return True
        pk = state.active_pokemon()
        if pk and pk.hp_pct <= int(s.get("pokemon_hp_pct", 40)) and bb.ready("pokemon_heal", cd):
            return True
        return False

    def act(self, state: GameState, bb: Blackboard, inp) -> None:
        s = self.cfg
        if state.player.hp_pct <= int(s.get("player_hp_pct", 60)):
            inp.press(s.get("player_heal_key", "F2"))
            bb.mark("player_heal")
            return
        pk = state.active_pokemon()
        if pk and pk.hp_pct <= int(s.get("pokemon_hp_pct", 40)):
            inp.press(s.get("pokemon_heal_key", "F3"))
            bb.mark("pokemon_heal")


class CaptureBehavior(Behavior):
    name = "capture"
    priority = 85

    def evaluate(self, state: GameState, bb: Blackboard) -> bool:
        if bb.paused or not self.cfg.get("enabled", True):
            return False
        if not bb.notes.get("corpse"):
            return False
        if any(c.attackable for c in state.creatures):
            return False
        return True

    def act(self, state: GameState, bb: Blackboard, inp) -> None:
        _, x, y, z = bb.notes["corpse"]
        pos = Vec3(x, y, z)
        if state.player.pos.distance(pos) <= int(self.cfg.get("range", 1)):
            if bb.ready("ball", float(self.cfg.get("interval", 0.8))):
                inp.mouse_to_tile(pos, {"camera": state.camera, "map_rect": state.map_rect})
                time.sleep(0.05)
                inp.press(self.cfg.get("ball_key", "|"))
                bb.notes["balls"] = bb.notes.get("balls", 0) + 1
                bb.mark("ball")
                if bb.notes["balls"] >= int(self.cfg.get("max_throws", 6)):
                    bb.notes.pop("corpse", None)
                    bb.notes["balls"] = 0
        else:
            if bb.ready("goto_corpse", 0.35):
                inp.walk_to(pos)
                bb.mark("goto_corpse")


class CombatBehavior(Behavior):
    name = "combat"
    priority = 70

    def evaluate(self, state: GameState, bb: Blackboard) -> bool:
        if bb.paused or not self.cfg.get("enabled", True):
            return False
        lt = bb.notes.get("last_target")
        if lt:
            lname, lx, ly, lz, lhp = lt
            still = any(
                c.kind == CreatureKind.MONSTER and c.name == lname
                and abs(c.pos.x - lx) <= 1 and abs(c.pos.y - ly) <= 1
                for c in state.creatures
            )
            if not still:
                bb.notes["corpse"] = (lname, lx, ly, lz)
                bb.notes["loot_corpse"] = (lname, lx, ly, lz)
                bb.notes["balls"] = 0
                bb.notes["loot_count"] = 0
                bb.notes["bag_sig"] = None
                bb.notes.pop("last_target", None)
        target = nearest(state, is_enemy, max_range=int(self.cfg.get("detect_range", 8)))
        if target is None:
            bb.notes.pop("tname", None)
            return False
        bb.notes["tname"] = target.name
        bb.notes["last_target"] = (target.name, target.pos.x, target.pos.y, target.pos.z, target.hp_pct)
        return True

    def act(self, state: GameState, bb: Blackboard, inp) -> None:
        s = self.cfg
        name = bb.notes.get("tname")
        if not name:
            return
        candidates = [c for c in state.creatures if c.name == name and is_enemy(c)]
        if not candidates:
            bb.notes.pop("tname", None)
            return
        target = min(candidates, key=lambda c: state.player.pos.distance(c.pos))
        if state.player.pos.distance(target.pos) > int(s.get("attack_range", 1)):
            direction = state.player.pos.direction_to(target.pos)
            if direction >= 0 and bb.ready("approach", float(s.get("approach_cooldown", 0.15))):
                inp.move(direction)
                bb.mark("approach")
            return

        now = time.time()
        if state.attacking_name != name:
            if now - bb.notes.get("tlast", 0.0) > float(s.get("retarget_interval", 1.5)):
                inp.click_tile(target.pos, {"player": state.player})
                bb.notes["tlast"] = now
            return

        threshold = float(s.get("ready_pct", 100))
        if bb.ready("attack", float(s.get("cooldown", 0.3))):
            if state.moves:
                ready = [m["key"] for m in state.moves
                         if isinstance(m.get("pct"), (int, float)) and m["pct"] >= threshold]
            else:
                ready = [str(k) for k in s.get("moves", ["1", "2", "3", "4"])]
            if ready:
                if bb.humanizer is not None and bb.humanizer.chance(0.2):
                    key = bb.humanizer.choice(ready)
                else:
                    index = bb.notes.get("move_idx", 0)
                    key = ready[index % len(ready)]
                    bb.notes["move_idx"] = index + 1
                inp.press(key)
                bb.mark("attack")


class LootBehavior(Behavior):
    name = "loot"
    priority = 88

    def evaluate(self, state: GameState, bb: Blackboard) -> bool:
        if bb.paused or not self.cfg.get("enabled", True):
            return False
        return bool(bb.notes.get("loot_corpse"))

    def act(self, state: GameState, bb: Blackboard, inp) -> None:
        corpse = bb.notes.get("loot_corpse")
        if not corpse:
            return
        _, x, y, z = corpse
        pos = Vec3(x, y, z)
        context = {"camera": state.camera, "map_rect": state.map_rect}
        if state.player.pos.distance(pos) > int(self.cfg.get("range", 8)):
            direction = state.player.pos.direction_to(pos)
            if direction >= 0 and bb.ready("loot_move", float(self.cfg.get("move_interval", 0.35))):
                inp.move(direction)
                bb.mark("loot_move")
            return
        if bb.ready("loot", float(self.cfg.get("interval", 1.2))):
            sig = tuple(state.bag)
            prev = bb.notes.get("bag_sig")
            if prev is not None and sig == prev:
                bb.notes.pop("loot_corpse", None)
                bb.notes["bag_sig"] = None
                return
            inp.mouse_to_tile(pos, context)
            time.sleep(0.05)
            inp.click_at(pos, context)
            if self.cfg.get("use_key", True):
                inp.press(self.cfg.get("loot_key", "e"))
            bb.mark("loot")
            bb.notes["bag_sig"] = sig
            count = bb.notes.get("loot_count", 0) + 1
            bb.notes["loot_count"] = count
            if count >= int(self.cfg.get("max_loots", 6)):
                bb.notes.pop("loot_corpse", None)
                bb.notes["bag_sig"] = None


def make_walkable(state: GameState, static_map: Optional[list] = None):
    if static_map:
        rows = static_map

        def walk_static(x: int, y: int) -> bool:
            if 0 <= y < len(rows) and 0 <= x < len(rows[y]):
                return rows[y][x] not in "#X"
            return False

        return walk_static
    if state.tiles:
        z = state.player.pos.z

        def walk_tiles(x: int, y: int) -> bool:
            tile = state.tiles.get((x, y, z))
            return True if tile is None else tile.walkable

        return walk_tiles
    return lambda x, y: True


class RouteBehavior(Behavior):
    name = "route"
    priority = 10

    def __init__(self, cfg: dict, settings: dict, route: Optional[Route]):
        super().__init__(cfg, settings)
        self.route = route

    def evaluate(self, state: GameState, bb: Blackboard) -> bool:
        if bb.paused or not self.cfg.get("enabled", True) or self.route is None:
            return False
        return True

    def act(self, state: GameState, bb: Blackboard, inp) -> None:
        waypoint = self.route.current
        arrive = int(self.cfg.get("arrive_distance", 1))
        if state.player.pos.distance(waypoint) <= arrive:
            self.route.advance()
            bb.notes.pop("route_key", None)
            return
        if bb.ready("route", float(self.cfg.get("step_delay", 0.35))):
            inp.walk_to(waypoint)
            bb.mark("route")


def build_behaviors(cfg: dict, route: Optional[Route]) -> list[Behavior]:
    behaviors_cfg = cfg.get("behaviors", {})
    settings = cfg.get("settings", {})
    behaviors: list[Behavior] = [
        CrisisBehavior(behaviors_cfg.get("crisis", {}), settings),
        HealingBehavior(behaviors_cfg.get("healing", {}), settings),
        CaptureBehavior(behaviors_cfg.get("capture", {}), settings),
        CombatBehavior(behaviors_cfg.get("combat", {}), settings),
        LootBehavior(behaviors_cfg.get("loot", {}), settings),
        RouteBehavior(behaviors_cfg.get("route", {}), settings, route),
    ]
    behaviors.sort(key=lambda b: b.priority, reverse=True)
    return behaviors
