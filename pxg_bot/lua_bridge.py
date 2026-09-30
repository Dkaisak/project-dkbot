from __future__ import annotations

import json
import os
import time
from typing import Optional

from .input import BaseInput
from .models import Creature, CreatureKind, GameState, Player, Vec3

# Otc::Direction (cliente) por indice de direccion del bot (0=N,1=NE,...,7=NW).
DEFAULT_DIRECTION_MAP = {0: 0, 1: 4, 2: 1, 3: 5, 4: 2, 5: 6, 6: 3, 7: 7}


def ot_to_our(ot_dir: int) -> int:
    for ours, ot in DEFAULT_DIRECTION_MAP.items():
        if ot == ot_dir:
            return ours
    return -1


class LuaBridge:
    def __init__(self, cfg: dict):
        self.state_file = cfg.get("state_file", "")
        self.cmd_file = cfg.get("cmd_file", "")
        self.direction_map = {int(k): int(v) for k, v in cfg.get("direction_map", DEFAULT_DIRECTION_MAP).items()}
        self._last_mtime = 0.0

    def read(self) -> dict:
        try:
            with open(self.state_file, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}

    def send(self, line: str) -> None:
        if not self.cmd_file:
            return
        try:
            with open(self.cmd_file, "w", encoding="utf-8") as handle:
                handle.write(line.rstrip("\n") + "\n")
        except OSError:
            pass

    def fresh(self, max_age: float = 1.0) -> bool:
        try:
            return (time.time() - os.path.getmtime(self.state_file)) <= max_age
        except OSError:
            return False


class LuaStateSource:
    def __init__(self, bridge: LuaBridge):
        self.bridge = bridge

    def read_state(self) -> GameState:
        data = self.bridge.read()
        if not data or not data.get("connected", False):
            return GameState(player=Player(), connected=False, timestamp=time.time())

        player = Player(
            name=data.get("name", ""),
            pos=Vec3(int(data.get("x", 0)), int(data.get("y", 0)), int(data.get("z", 0))),
            hp=int(data.get("hp", 0)),
            max_hp=int(data.get("maxhp", 0)),
            level=int(data.get("level", 0)),
            direction=ot_to_our(int(data.get("dir", 0))),
            alive=int(data.get("hp", 0)) > 0,
        )
        creatures: list[Creature] = []
        seen: set = set()
        pname = data.get("name", "")
        ppos = (int(data.get("x", 0)), int(data.get("y", 0)), int(data.get("z", 0)))

        def add_creature(cid: int, c: dict) -> None:
            kind = CreatureKind.UNKNOWN
            if c.get("player"):
                kind = CreatureKind.PLAYER
            elif c.get("npc"):
                kind = CreatureKind.NPC
            elif c.get("monster"):
                kind = CreatureKind.MONSTER
            pos = Vec3(int(c.get("x", 0)), int(c.get("y", 0)), int(c.get("z", 0)))
            key = (pos.x, pos.y, pos.z, c.get("name", ""))
            if key in seen:
                return
            seen.add(key)
            creatures.append(
                Creature(
                    cid=cid,
                    name=c.get("name", ""),
                    pos=pos,
                    hp_pct=int(c.get("hp", 0)),
                    kind=kind,
                    is_player=bool(c.get("player")),
                    is_npc=bool(c.get("npc")),
                    is_wild=kind == CreatureKind.MONSTER,
                    is_self=(c.get("name") == pname and (pos.x, pos.y, pos.z) == ppos),
                )
            )

        for c in data.get("creatures", []):
            add_creature(int(c.get("id", 0)), c)
        for tile in data.get("nearby", []):
            for c in tile.get("creatures", []):
                add_creature(len(creatures) + 1, c)

        controlling = data.get("controlling", "")
        if controlling:
            for cr in creatures:
                if cr.name == controlling and not cr.is_player:
                    cr.is_summon = True
                    cr.is_wild = False

        battle = data.get("battle", [])
        for entry in battle:
            add_creature(
                len(creatures) + 1,
                {
                    "name": entry.get("name", ""),
                    "x": entry.get("x", 0), "y": entry.get("y", 0), "z": entry.get("z", 0),
                    "monster": not entry.get("own", False),
                    "hp": 100,
                },
            )
        if battle:
            own = battle[0]
            own_pos = Vec3(int(own.get("x", 0)), int(own.get("y", 0)), int(own.get("z", 0)))
            own_name = own.get("name", "")
            match = [cr for cr in creatures if cr.name == own_name and cr.pos == own_pos]
            if not match:
                cands = [cr for cr in creatures if cr.name == own_name and not cr.is_player and not cr.is_self]
                match = sorted(cands, key=lambda c: c.pos.distance(own_pos))[:1]
            for cr in match:
                cr.is_summon = True
                cr.is_wild = False
        state = GameState(player=player, creatures=creatures, timestamp=time.time())
        state.in_battle = bool(data.get("attacking")) or any(c.kind == CreatureKind.MONSTER for c in creatures)
        state.attacking_name = data.get("attacking_name", "")
        state.moves = data.get("moves", [])
        state.bag = data.get("bag", [])
        cam = data.get("camera")
        state.camera = (int(cam["x"]), int(cam["y"])) if cam else None
        state.map_rect = data.get("map_rect")
        state.connected = True
        return state


class LuaInput(BaseInput):
    def __init__(self, bridge: LuaBridge):
        self.bridge = bridge

    def move(self, direction: int) -> None:
        ot_dir = self.bridge.direction_map.get(direction)
        if ot_dir is not None:
            self.bridge.send(f"walk {ot_dir}")

    def walk_to(self, target: Vec3) -> None:
        self.bridge.send(f"nav {target.x} {target.y} {target.z}")

    def loot(self) -> None:
        self.bridge.send("loot")

    def press(self, key: str) -> None:
        self.bridge.send(f"key {key}")

    def click_tile(self, target: Vec3, context: dict) -> None:
        self.bridge.send(f"attackat {target.x} {target.y} {target.z}")

    def say(self, text: str) -> None:
        self.bridge.send(f"say {text}")

    def stop(self) -> None:
        self.bridge.send("stop")


class HybridInput(BaseInput):
    """Movimiento/target por Lua; hotkeys de skills por X11 (eventos reales)."""

    def __init__(self, lua_input: "LuaInput", key_input):
        self.lua = lua_input
        self.key = key_input

    def move(self, direction: int) -> None:
        self.lua.move(direction)

    def click_tile(self, target: Vec3, context: dict) -> None:
        self.lua.click_tile(target, context)

    def say(self, text: str) -> None:
        self.lua.say(text)

    def loot(self) -> None:
        self.lua.loot()

    def stop(self) -> None:
        self.lua.stop()

    def press(self, key: str) -> None:
        self.key.press(str(key))

    def _tile_screen(self, target: Vec3, context: dict):
        camera = context.get("camera")
        rect = context.get("map_rect")
        if not camera or not rect:
            return None
        try:
            wx, wy = self.key.window_origin()
        except Exception:
            wx, wy = 0, 0
        tile = getattr(self.key, "tile_size", 32)
        tile_x = int(getattr(self.key, "settings", {}).get("tile_size_x", tile))
        off = getattr(self.key, "settings", {}).get("mouse_offset_tiles", [0, 0])
        px = getattr(self.key, "settings", {}).get("mouse_offset_px", [0, 0])
        sx = (wx + int(rect.get("x", 0)) + int(rect.get("w", 0)) // 2
              + (target.x - camera[0]) * tile_x + tile_x // 2 + int(float(off[0]) * tile_x)
              + int(px[0]))
        sy = (wy + int(rect.get("y", 0)) + int(rect.get("h", 0)) // 2
              + (target.y - camera[1]) * tile + tile // 2 + int(float(off[1]) * tile)
              + int(px[1]))
        return sx, sy

    def mouse_to_tile(self, target: Vec3, context: dict) -> None:
        coords = self._tile_screen(target, context)
        if coords:
            self.key.mouse_move(*coords)

    def click_at(self, target: Vec3, context: dict) -> None:
        coords = self._tile_screen(target, context)
        if coords:
            button = int(getattr(self.key, "settings", {}).get("loot_button", 1))
            self.key.mouse_click(coords[0], coords[1], button)
