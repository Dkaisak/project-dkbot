from __future__ import annotations

import time
from typing import Optional

from .memory import MemoryReadError
from .models import Creature, CreatureKind, GameState, Item, Player, Pokemon, Tile, Vec3
from .offsets import Offsets, hexint, read_field


class MemoryStateSource:
    def __init__(self, process, cfg: dict):
        self.proc = process
        self.cfg = cfg.get("offsets", cfg)
        self.off = Offsets(process, self.cfg)

    def read_state(self) -> GameState:
        player = self._read_player()
        state = GameState(player=player, timestamp=time.time())
        state.creatures = self._read_creatures()
        state.party = self._read_party()
        state.inventory = self._read_inventory()
        state.tiles = self._read_tiles(player)
        state.in_battle = any(c.attackable for c in state.creatures)
        return state

    def _player_struct(self) -> int:
        return self.off.base("player_base")

    def _read_player(self) -> Player:
        cfg = self.cfg.get("player", {})
        fields = cfg.get("fields", {})
        p = Player()
        try:
            base = self._player_struct()
        except Exception:
            return p
        p.name = read_field(self.proc, base, fields.get("name"), "") or ""
        p.pos = Vec3(
            int(read_field(self.proc, base, fields.get("x"), 0) or 0),
            int(read_field(self.proc, base, fields.get("y"), 0) or 0),
            int(read_field(self.proc, base, fields.get("z"), 0) or 0),
        )
        p.hp = int(read_field(self.proc, base, fields.get("hp"), 0) or 0)
        p.max_hp = int(read_field(self.proc, base, fields.get("max_hp"), 0) or 0)
        p.mp = int(read_field(self.proc, base, fields.get("mp"), 0) or 0)
        p.max_mp = int(read_field(self.proc, base, fields.get("max_mp"), 0) or 0)
        p.level = int(read_field(self.proc, base, fields.get("level"), 0) or 0)
        p.exp = int(read_field(self.proc, base, fields.get("exp"), 0) or 0)
        p.direction = int(read_field(self.proc, base, fields.get("direction"), 4) or 0)
        alive = read_field(self.proc, base, fields.get("alive"), None)
        p.alive = bool(alive) if alive is not None else (p.hp > 0)
        return p

    def _array_base(self, parent_base: int, arr: dict) -> Optional[int]:
        if "from" in arr and arr["from"] != "player_base":
            base = self.off.base(arr["from"])
        else:
            base = self._player_struct()
        loc = base + hexint(arr.get("offset", 0))
        if arr.get("deref"):
            loc = self.proc.read_pointer(loc)
        return loc

    def _entry_count(self, array_base: int, parent_base: int, arr: dict) -> int:
        count_spec = arr.get("count")
        if not count_spec:
            return int(arr.get("count_fixed", 0))
        base_name = arr.get("count_base")
        if base_name == "array":
            source = array_base
        elif base_name:
            try:
                source = self.off.base(base_name)
            except Exception:
                source = parent_base
        else:
            source = parent_base
        return int(read_field(self.proc, source, count_spec, 0) or 0)

    def _entry_address(self, array_base: int, arr: dict, index: int) -> int:
        stride = hexint(arr.get("stride", 0))
        addr = array_base + index * stride
        if arr.get("pointer_array"):
            addr = self.proc.read_pointer(addr)
        return addr

    def _read_creatures(self) -> list[Creature]:
        arr = self.cfg.get("battle_list")
        if not arr:
            return []
        result: list[Creature] = []
        try:
            parent = self._player_struct()
            array_base = self._array_base(parent, arr)
            count = min(self._entry_count(array_base, parent, arr), int(arr.get("max_entries", 250)))
        except Exception:
            return result
        fields = arr.get("fields", {})
        for i in range(max(0, count)):
            try:
                entry = self._entry_address(array_base, arr, i)
                cid = int(read_field(self.proc, entry, fields.get("id"), 0) or 0)
                if cid == 0 and not arr.get("allow_empty_cid", False):
                    continue
                hp_pct = int(read_field(self.proc, entry, fields.get("hp_pct"), 0) or 0)
                if hp_pct <= 0 and fields.get("hp_pct") is not None:
                    hp_pct = 0
                creature = Creature(
                    cid=cid,
                    name=read_field(self.proc, entry, fields.get("name"), "") or "",
                    pos=Vec3(
                        int(read_field(self.proc, entry, fields.get("x"), 0) or 0),
                        int(read_field(self.proc, entry, fields.get("y"), 0) or 0),
                        int(read_field(self.proc, entry, fields.get("z"), 0) or 0),
                    ),
                    hp_pct=hp_pct,
                    kind=self._kind(read_field(self.proc, entry, fields.get("kind"), 0)),
                )
                creature.is_player = bool(read_field(self.proc, entry, fields.get("is_player"), 0))
                creature.is_npc = bool(read_field(self.proc, entry, fields.get("is_npc"), 0))
                creature.is_summon = bool(read_field(self.proc, entry, fields.get("is_summon"), 0))
                creature.is_wild = bool(read_field(self.proc, entry, fields.get("is_wild"), 0))
                creature.is_self = bool(read_field(self.proc, entry, fields.get("is_self"), 0))
                result.append(creature)
            except (MemoryReadError, OSError):
                continue
        return result

    @staticmethod
    def _kind(value) -> CreatureKind:
        try:
            return CreatureKind(int(value))
        except (ValueError, TypeError):
            return CreatureKind.UNKNOWN

    def _read_party(self) -> list[Pokemon]:
        arr = self.cfg.get("party")
        if not arr:
            return []
        result: list[Pokemon] = []
        try:
            parent = self._player_struct()
            array_base = self._array_base(parent, arr)
            count = min(self._entry_count(array_base, parent, arr), int(arr.get("max_entries", 6)))
        except Exception:
            return result
        fields = arr.get("fields", {})
        for i in range(max(0, count)):
            try:
                entry = self._entry_address(array_base, arr, i)
                result.append(
                    Pokemon(
                        slot=int(read_field(self.proc, entry, fields.get("slot"), i) or i),
                        name=read_field(self.proc, entry, fields.get("name"), "") or "",
                        hp_pct=int(read_field(self.proc, entry, fields.get("hp_pct"), 0) or 0),
                        level=int(read_field(self.proc, entry, fields.get("level"), 0) or 0),
                        active=bool(read_field(self.proc, entry, fields.get("active"), 0)),
                        alive=bool(read_field(self.proc, entry, fields.get("alive"), 1)),
                        in_field=bool(read_field(self.proc, entry, fields.get("in_field"), 0)),
                    )
                )
            except (MemoryReadError, OSError):
                continue
        return result

    def _read_inventory(self) -> list[Item]:
        arr = self.cfg.get("inventory")
        if not arr:
            return []
        result: list[Item] = []
        try:
            parent = self._player_struct()
            array_base = self._array_base(parent, arr)
            count = min(self._entry_count(array_base, parent, arr), int(arr.get("max_entries", 20)))
        except Exception:
            return result
        fields = arr.get("fields", {})
        for i in range(max(0, count)):
            try:
                entry = self._entry_address(array_base, arr, i)
                item_id = int(read_field(self.proc, entry, fields.get("item_id"), 0) or 0)
                if item_id == 0:
                    continue
                result.append(
                    Item(
                        slot=i,
                        item_id=item_id,
                        count=int(read_field(self.proc, entry, fields.get("count"), 1) or 0),
                        name=read_field(self.proc, entry, fields.get("name"), "") or "",
                    )
                )
            except (MemoryReadError, OSError):
                continue
        return result

    def _read_tiles(self, player: Player) -> dict:
        arr = self.cfg.get("map")
        if not arr:
            return {}
        tiles: dict = {}
        try:
            parent = self._player_struct()
            array_base = self._array_base(parent, arr)
            radius = int(arr.get("radius", 8))
        except Exception:
            return tiles
        fields = arr.get("fields", {})
        stride = hexint(arr.get("stride", 0))
        width = radius * 2 + 1
        for i in range(width * width):
            try:
                entry = array_base + i * stride
                if arr.get("pointer_array"):
                    entry = self.proc.read_pointer(entry)
                tx = int(read_field(self.proc, entry, fields.get("x"), 0) or 0)
                ty = int(read_field(self.proc, entry, fields.get("y"), 0) or 0)
                walkable = bool(read_field(self.proc, entry, fields.get("walkable"), 1))
                tiles[(tx, ty, player.pos.z)] = Tile(
                    pos=Vec3(tx, ty, player.pos.z),
                    walkable=walkable,
                    ground_id=int(read_field(self.proc, entry, fields.get("ground_id"), 0) or 0),
                )
            except (MemoryReadError, OSError):
                continue
        return tiles
