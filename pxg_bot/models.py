from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional

DIR_VECTORS = {
    0: (0, -1),
    1: (1, -1),
    2: (1, 0),
    3: (1, 1),
    4: (0, 1),
    5: (-1, 1),
    6: (-1, 0),
    7: (-1, -1),
}
DIR_BY_VECTOR = {v: k for k, v in DIR_VECTORS.items()}


class CreatureKind(IntEnum):
    UNKNOWN = 0
    PLAYER = 1
    NPC = 2
    MONSTER = 3
    POKEMON = 4


@dataclass(frozen=True)
class Vec3:
    x: int
    y: int
    z: int = 0

    def __add__(self, other: "Vec3") -> "Vec3":
        return Vec3(self.x + other.x, self.y + other.y, self.z + other.z)

    def __sub__(self, other: "Vec3") -> "Vec3":
        return Vec3(self.x - other.x, self.y - other.y, self.z - other.z)

    def distance(self, other: "Vec3") -> int:
        if self.z != other.z:
            return 255
        return max(abs(self.x - other.x), abs(self.y - other.y))

    def direction_to(self, other: "Vec3") -> int:
        dx = (other.x > self.x) - (other.x < self.x)
        dy = (other.y > self.y) - (other.y < self.y)
        return DIR_BY_VECTOR.get((dx, dy), -1)

    def step_towards(self, other: "Vec3") -> "Vec3":
        sx = (other.x > self.x) - (other.x < self.x)
        sy = (other.y > self.y) - (other.y < self.y)
        return Vec3(self.x + sx, self.y + sy, self.z)


@dataclass
class Player:
    name: str = ""
    pos: Vec3 = field(default_factory=lambda: Vec3(0, 0, 0))
    hp: int = 0
    max_hp: int = 0
    mp: int = 0
    max_mp: int = 0
    level: int = 0
    exp: int = 0
    direction: int = 4
    alive: bool = True

    @property
    def hp_pct(self) -> int:
        if self.max_hp <= 0:
            return 0
        return max(0, min(100, round(self.hp * 100 / self.max_hp)))

    @property
    def mp_pct(self) -> int:
        if self.max_mp <= 0:
            return 0
        return max(0, min(100, round(self.mp * 100 / self.max_mp)))


@dataclass
class Creature:
    cid: int
    name: str
    pos: Vec3
    hp_pct: int
    kind: CreatureKind = CreatureKind.UNKNOWN
    is_self: bool = False
    is_wild: bool = False
    is_summon: bool = False
    is_player: bool = False
    is_npc: bool = False
    visible: bool = True
    outfit: Optional[int] = None
    shiny: bool = False
    uid: str = ""
    ignored: bool = False

    @property
    def attackable(self) -> bool:
        return (
            not self.is_self
            and not self.is_npc
            and not self.is_summon
            and not self.ignored
            and self.hp_pct > 0
            and (self.kind in (CreatureKind.MONSTER, CreatureKind.POKEMON) or self.is_wild)
        )


@dataclass
class Pokemon:
    slot: int
    name: str
    hp_pct: int
    level: int = 0
    active: bool = False
    alive: bool = True
    in_field: bool = False


@dataclass
class Item:
    slot: int
    item_id: int
    count: int = 0
    name: str = ""


@dataclass
class Tile:
    pos: Vec3
    walkable: bool = True
    ground_id: int = 0


@dataclass
class GameState:
    player: Player
    creatures: list[Creature] = field(default_factory=list)
    party: list[Pokemon] = field(default_factory=list)
    inventory: list[Item] = field(default_factory=list)
    tiles: dict[tuple[int, int, int], Tile] = field(default_factory=dict)
    connected: bool = True
    in_battle: bool = False
    attacking_name: str = ""
    is_walking: bool = False
    spawn_blocked: bool = False
    captured: bool = False
    active_pokemon_name: str = ""
    skill_order: list = field(default_factory=list)
    moves: list = field(default_factory=list)
    bag: list = field(default_factory=list)
    server_msgs: list = field(default_factory=list)
    defeated: list = field(default_factory=list)
    camera: Optional[tuple] = None
    map_rect: Optional[dict] = None
    tile_size: Optional[dict] = None
    visible: Optional[dict] = None
    slot_pos: dict = field(default_factory=dict)
    win_pos: tuple = (0, 0)
    nav_result: str = ""
    pokemon_pos: Optional[tuple] = None
    pokemon_hp: Optional[int] = None
    timestamp: float = 0.0

    def creature(self, cid: int) -> Optional[Creature]:
        for c in self.creatures:
            if c.cid == cid:
                return c
        return None

    def active_pokemon(self) -> Optional[Pokemon]:
        for p in self.party:
            if p.active:
                return p
        return None
