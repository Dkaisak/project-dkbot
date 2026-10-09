from __future__ import annotations

import json
import os
import time
from typing import Optional

from .input import BaseInput
from .models import Creature, CreatureKind, GameState, Player, Pokemon, Vec3

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
        self.shiny_file = cfg.get("shiny_file", "")
        self.pokemon_skills_file = cfg.get("pokemon_skills_file", "")
        self.ignore_file = cfg.get("ignore_file", "")
        self.clans_file = cfg.get("clans_file", "")
        self.direction_map = {int(k): int(v) for k, v in cfg.get("direction_map", DEFAULT_DIRECTION_MAP).items()}
        self._last_mtime = 0.0
        self._cached: Optional[dict] = None

    def read(self) -> dict:
        # cache por mtime: el agente reescribe el estado cada ~200 ms, pero el bot
        # lo lee cada tick (~50 ms). Evita abrir+parsear el JSON 4 veces de mas.
        try:
            mtime = os.path.getmtime(self.state_file)
        except OSError:
            return {}
        if mtime == self._last_mtime and self._cached is not None:
            return self._cached
        try:
            with open(self.state_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        self._last_mtime = mtime
        self._cached = data
        return data

    def send(self, line: str) -> None:
        if not self.cmd_file:
            return
        try:
            with open(self.cmd_file, "a", encoding="utf-8") as handle:
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
        from .shiny import ShinyTable

        self.shiny = ShinyTable(bridge.shiny_file) if bridge.shiny_file else None
        self._last_shiny_save = 0.0
        from .pokemon_skills import PokemonSkills

        self.pokemon_skills = (PokemonSkills(bridge.pokemon_skills_file)
                               if bridge.pokemon_skills_file else None)
        self._last_skills_save = 0.0
        from .ignore import IgnoreTable

        self.ignore = IgnoreTable(bridge.ignore_file) if bridge.ignore_file else None
        self._last_ignore_save = 0.0
        from .clans import ClanTable

        clans_file = getattr(bridge, "clans_file", "")
        self.clans = ClanTable(clans_file) if clans_file else None

    def read_state(self) -> GameState:
        data = self.bridge.read()
        if self.ignore is not None:
            self.ignore.maybe_reload()
        if not data or not data.get("connected", False):
            return GameState(player=Player(), connected=False, timestamp=time.time(),
                             online=bool(data.get("online", False)),
                             conn_ok=bool(data.get("conn_ok", False)),
                             selector=bool(data.get("selector", False)))

        player = Player(
            name=data.get("name", ""),
            pos=Vec3(int(data.get("x", 0)), int(data.get("y", 0)), int(data.get("z", 0))),
            hp=int(data.get("hp", 0)),
            max_hp=int(data.get("maxhp", 0)),
            level=int(data.get("level", 0)),
            exp=int(data.get("exp", 0) or 0),
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
            uid = str(c.get("id", "") or "")
            name = c.get("name", "")
            ignored = bool(self.ignore is not None and self.ignore.is_ignored(name, uid))
            try:
                skull = int(c.get("skull") or 0)
            except (TypeError, ValueError):
                skull = 0
            try:
                emblem = int(c.get("emblem") or 0)
            except (TypeError, ValueError):
                emblem = 0
            clan = self.clans.clan_for(skull) if self.clans is not None else ""
            creatures.append(
                Creature(
                    cid=cid,
                    name=name,
                    pos=pos,
                    hp_pct=int(c.get("hp", 0)),
                    kind=kind,
                    is_player=bool(c.get("player")),
                    is_npc=bool(c.get("npc")),
                    is_wild=kind == CreatureKind.MONSTER,
                    is_self=(c.get("name") == pname and (pos.x, pos.y, pos.z) == ppos),
                    outfit=c.get("outfit"),
                    uid=uid,
                    ignored=ignored,
                    skull=skull,
                    emblem=emblem,
                    clan=clan,
                )
            )

        for c in data.get("creatures", []):
            try:
                cid = int(c.get("id", 0))
            except (TypeError, ValueError):
                cid = 0
            add_creature(cid, c)
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
        if self.shiny is not None:
            self.shiny.observe(creatures)
            for cr in creatures:
                cr.shiny = self.shiny.is_shiny(cr)
            if time.time() - self._last_shiny_save > 10:
                self.shiny.save()
                self._last_shiny_save = time.time()
        if self.ignore is not None and time.time() - self._last_ignore_save > 10:
            self.ignore.save()
            self._last_ignore_save = time.time()
        state = GameState(player=player, creatures=creatures,
                          timestamp=float(getattr(self.bridge, "_last_mtime", 0.0) or 0.0))
        state.in_battle = bool(data.get("attacking")) or any(c.kind == CreatureKind.MONSTER for c in creatures)
        state.attacking_name = data.get("attacking_name", "")
        state.is_walking = bool(data.get("is_walking", False))
        state.spawn_blocked = bool(data.get("spawn_blocked", False))
        state.captured = bool(data.get("captured", False))
        cv = data.get("captures")
        state.captures = int(cv) if isinstance(cv, (int, float)) else -1
        state.capture_name = str(data.get("capture_name", "") or "")
        lv = data.get("loots")
        state.loots = int(lv) if isinstance(lv, (int, float)) else -1
        state.loot_msg = str(data.get("loot_msg", "") or "")
        state.active_pokemon_name = str(data.get("active_pokemon", "") or "")
        state.moves = data.get("moves", [])
        state.party = [
            Pokemon(slot=int(p.get("slot", 0)),
                    name=str(p.get("name") or p.get("id") or ""),
                    hp_pct=int(p.get("hp", 0)), alive=int(p.get("hp", 0)) > 0,
                    active=bool(p.get("active")))
            for p in (data.get("party", []) or [])
        ]
        if self.pokemon_skills is not None:
            self.pokemon_skills.observe(state.active_pokemon_name, state.moves)
            state.skill_order = self.pokemon_skills.order_for(state.active_pokemon_name)
            state.lure_order = self.pokemon_skills.lure_order_for(state.active_pokemon_name)
            if time.time() - self._last_skills_save > 10:
                self.pokemon_skills.save()
                self._last_skills_save = time.time()
        state.bag = data.get("bag", [])
        state.bag_counts = data.get("bag_counts", {}) or {}
        state.server_msgs = data.get("server_msgs", [])
        state.defeated = data.get("defeated", [])
        fm = data.get("fight_mode")
        state.fight_mode = int(fm) if isinstance(fm, (int, float)) else None
        cam = data.get("camera")
        state.camera = (int(cam["x"]), int(cam["y"])) if cam else None
        state.map_rect = data.get("map_rect")
        state.tile_size = data.get("tile_size")
        state.visible = data.get("visible")
        state.slot_pos = data.get("slot_pos", {}) or {}
        wp = data.get("win_pos") or {}
        state.win_pos = (int(wp.get("x", 0)), int(wp.get("y", 0)))
        state.nav_result = str(data.get("nav_result", "") or "")
        pp = data.get("pokemon_pos")
        state.pokemon_pos = (int(pp["x"]), int(pp["y"]), int(pp["z"])) if pp else None
        ph = data.get("pokemon_hp")
        state.pokemon_hp = int(ph) if isinstance(ph, (int, float)) else None
        state.dead = bool(data.get("dead", False))
        state.online = bool(data.get("online", False))
        state.conn_ok = bool(data.get("conn_ok", False))
        state.death_window = bool(data.get("death_window", False))
        state.has_teleport = bool(data.get("has_teleport", False))
        state.can_teleport = bool(data.get("can_teleport", False))
        state.in_combat = bool(data.get("in_combat", False))
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

    def walk_path(self, waypoints: list, z: int) -> None:
        parts = [str(int(z))]
        for w in waypoints:
            parts.append(str(int(w.x)))
            parts.append(str(int(w.y)))
        if len(parts) > 1:
            self.bridge.send("navpath " + " ".join(parts))

    def turn(self, direction: int) -> None:
        self.bridge.send(f"turn {int(direction)}")

    def hotkey(self, key: str) -> None:
        self.bridge.send(f"hotkey {key}")

    def stand_near(self, target: Vec3) -> None:
        self.bridge.send(f"standnear {target.x} {target.y} {target.z}")

    def stand_near_many(self, bodies: list, z: int) -> None:
        parts = [str(z)]
        for b in bodies:
            parts.append(str(b[0]))
            parts.append(str(b[1]))
        self.bridge.send("standnearmany " + " ".join(parts))

    def loot(self) -> None:
        self.bridge.send("loot")

    def spell(self, slot: str) -> None:
        self.bridge.send(f"spell {slot}")

    def press(self, key: str) -> None:
        self.spell(key)

    def click_tile(self, target: Vec3, context: dict) -> None:
        # combate: fijar objetivo (Lua), no mueve al personaje
        self.bridge.send(f"attackat {target.x} {target.y} {target.z}")

    def mouse_to_tile(self, target: Vec3, context: dict) -> None:
        # el clic por Lua no necesita mover cursor fisico; no-op
        pass

    def click_at(self, target: Vec3, context: dict) -> None:
        # clic derecho exclusivo para loot/captura (Lua)
        self.bridge.send(f"rclick {target.x} {target.y} {target.z}")

    def say(self, text: str) -> None:
        self.bridge.send(f"say {text}")

    def stop(self) -> None:
        self.bridge.send("stop")

    def middle_click(self, target: Vec3) -> None:
        self.bridge.send(f"mclick {target.x} {target.y} {target.z}")

    def click_slot(self, slot: int) -> None:
        self.bridge.send(f"clickslot {int(slot)}")

    def set_fight_mode(self, mode: int) -> None:
        # 1=Ofensivo, 2=Equilibrado, 3=Defensivo (FightModes del cliente)
        self.bridge.send(f"setfightmode {int(mode)}")

    def ball(self, item_id: int, target: Vec3) -> None:
        # aplica la ball al Thing del tile, sin mover el cursor (Lua)
        self.bridge.send(f"ball {int(item_id)} {target.x} {target.y} {target.z}")

    def call_slot(self, slot: int) -> None:
        self.bridge.send(f"callslot {int(slot)}")

    def pokestop(self, method: str = "func", arg: str = "") -> None:
        line = "pokestop " + str(method)
        if arg:
            line += " " + str(arg)
        self.bridge.send(line)

    def revive(self, slot: int, item_id: int = 2269) -> None:
        # El agente aplica el item al slot por pokeId (Pokeball), SIN mover el
        # cursor fisico -> no necesita X11.
        self.bridge.send(f"reviveslot {int(slot)} {int(item_id)}")

    def order(self, target: Vec3) -> None:
        # order: ordena el pokemon propio (ownsummon) al tile. El agente lo
        # resuelve por coordenadas (tileToScreen + getMapThingByMousePosition),
        # SIN mover el cursor fisico -> no necesita X11.
        self.bridge.send(f"order {int(target.x)} {int(target.y)} {int(target.z)}")

    def open_game(self, name: str = "") -> None:
        # reconecta desde el selector de personajes (elige el personaje)
        line = "opengame"
        if name:
            line += " " + str(name)
        self.bridge.send(line)

    def dismiss_death(self) -> None:
        # cierra la ventana de muerte (sin recuperar)
        self.bridge.send("deathdismiss")

    def teleport(self, destination: str) -> None:
        # habilidad Teleport por chat: h"<destino> (requiere el pokemon fuera)
        self.bridge.send(f"teleport {destination}")

    def left_click(self, target: Vec3) -> None:
        # clic izquierdo en un tile (p. ej. usar/pisar una escalera)
        self.bridge.send(f"lclick {int(target.x)} {int(target.y)} {int(target.z)}")

    def fly(self, slot=None) -> None:
        # entra en modo vuelo (orderonself con un pokemon volador fuera)
        line = "fly"
        if slot:
            line += " " + str(int(slot))
        self.bridge.send(line)

    def fly_up(self) -> None:
        self.bridge.send("flyup")

    def fly_down(self) -> None:
        self.bridge.send("flydown")

    def fly_to(self, z: int) -> None:
        # sube/baja en vuelo hasta el z objetivo (agente: repite flyup/flydown)
        self.bridge.send(f"flyto {int(z)}")

    def order_self(self) -> None:
        # ordena el ownsummon sobre el personaje (toggle de vuelo / desmontar)
        self.bridge.send("orderonself")

