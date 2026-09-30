"""Cliente falso: mantiene en memoria estructuras con el mismo layout que
espera el lector, para validar el pipeline completo sin el juego real.

Imprime una linea JSON con las direcciones y offsets al arrancar.
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import time

N_CREATURES = 8
N_PARTY = 6


class FakePlayer(ctypes.Structure):
    _fields_ = [
        ("hp", ctypes.c_int32), ("max_hp", ctypes.c_int32),
        ("mp", ctypes.c_int32), ("max_mp", ctypes.c_int32),
        ("x", ctypes.c_int32), ("y", ctypes.c_int32), ("z", ctypes.c_int32),
        ("level", ctypes.c_int32), ("exp", ctypes.c_int32),
        ("direction", ctypes.c_uint8), ("alive", ctypes.c_uint8),
        ("name", ctypes.c_char * 32),
    ]


class FakeCreature(ctypes.Structure):
    _fields_ = [
        ("cid", ctypes.c_int32),
        ("name", ctypes.c_char * 32),
        ("x", ctypes.c_int32), ("y", ctypes.c_int32), ("z", ctypes.c_int32),
        ("hp_pct", ctypes.c_uint8), ("kind", ctypes.c_uint8),
        ("is_player", ctypes.c_uint8), ("is_npc", ctypes.c_uint8),
        ("is_summon", ctypes.c_uint8), ("is_wild", ctypes.c_uint8),
        ("is_self", ctypes.c_uint8),
    ]


class FakePokemon(ctypes.Structure):
    _fields_ = [
        ("slot", ctypes.c_int32),
        ("name", ctypes.c_char * 32),
        ("hp_pct", ctypes.c_uint8), ("active", ctypes.c_uint8),
        ("alive", ctypes.c_uint8), ("in_field", ctypes.c_uint8),
        ("level", ctypes.c_int32),
    ]


class Layout:
    def __init__(self):
        self.player = FakePlayer()
        self.player_ptr = ctypes.c_void_p(ctypes.addressof(self.player))
        self.creatures = (FakeCreature * N_CREATURES)()
        self.creature_ptrs = (ctypes.c_void_p * N_CREATURES)()
        for i in range(N_CREATURES):
            self.creature_ptrs[i] = ctypes.addressof(self.creatures[i])
        self.battle_array_ptr = ctypes.c_void_p(ctypes.addressof(self.creature_ptrs))
        self.battle_count = ctypes.c_int32(0)
        self.party = (FakePokemon * N_PARTY)()

    def info(self) -> dict:
        player_fields = ["hp", "max_hp", "mp", "max_mp", "x", "y", "z",
                         "level", "exp", "direction", "alive", "name"]
        creature_fields = ["cid", "name", "x", "y", "z", "hp_pct", "kind",
                           "is_player", "is_npc", "is_summon", "is_wild", "is_self"]
        pokemon_fields = ["slot", "name", "hp_pct", "active", "alive", "in_field", "level"]
        return {
            "pid": os.getpid(),
            "pointer_size": ctypes.sizeof(ctypes.c_void_p),
            "player_base": ctypes.addressof(self.player_ptr),
            "battle_array": ctypes.addressof(self.battle_array_ptr),
            "battle_count": ctypes.addressof(self.battle_count),
            "party_base": ctypes.addressof(self.party),
            "creature_stride": ctypes.sizeof(FakeCreature),
            "pokemon_stride": ctypes.sizeof(FakePokemon),
            "player_fields": {n: getattr(FakePlayer, n).offset for n in player_fields},
            "creature_fields": {n: getattr(FakeCreature, n).offset for n in creature_fields},
            "pokemon_fields": {n: getattr(FakePokemon, n).offset for n in pokemon_fields},
        }


def _clear_creature(c: FakeCreature) -> None:
    c.cid = 0
    c.name = b""
    c.x = c.y = 0
    c.z = 7
    c.hp_pct = 0
    c.kind = 0
    c.is_player = c.is_npc = c.is_summon = c.is_wild = c.is_self = 0


def _set_creature(c, cid, name, x, y, z, hp, kind, is_player=0, is_wild=0) -> None:
    c.cid = cid
    c.name = name.encode("latin-1")[:31]
    c.x, c.y, c.z = x, y, z
    c.hp_pct = hp
    c.kind = kind
    c.is_player = is_player
    c.is_wild = is_wild
    c.is_npc = c.is_summon = c.is_self = 0


def update(layout: Layout, phase: int) -> None:
    p = layout.player
    p.hp, p.max_hp = 100, 100
    p.mp, p.max_mp = 50, 50
    p.x, p.y, p.z = 100, 100, 7
    p.level, p.direction, p.alive, p.name = 10, 4, 1, b"Hero"
    p.exp = 0x0BADF00D

    for i in range(N_CREATURES):
        _clear_creature(layout.creatures[i])
    layout.battle_count.value = 0
    for i in range(N_PARTY):
        pk = layout.party[i]
        pk.slot = i
        pk.name = b""
        pk.hp_pct = pk.active = pk.alive = pk.in_field = 0
        pk.level = 0
    pk = layout.party[0]
    pk.slot, pk.name, pk.hp_pct, pk.active, pk.alive, pk.level = 0, b"Pikachu", 100, 1, 1, 10

    if 3 <= phase < 8:
        hp = [100, 80, 55, 35, 20][min(phase - 3, 4)]
        _set_creature(layout.creatures[0], 1001, "Rattata", 104, 100, 7, hp, 4, is_wild=1)
        layout.battle_count.value = 1
    if 8 <= phase < 10:
        layout.party[0].hp_pct = 30
    if 10 <= phase < 13:
        _set_creature(layout.creatures[0], 2002, "Enemy", 102, 100, 7, 100, 1, is_player=1)
        layout.battle_count.value = 1
    if 15 <= phase < 17:
        _set_creature(layout.creatures[0], 1001, "Rattata", 101, 100, 7, 0, 4, is_wild=1)
        layout.battle_count.value = 1
    if 18 <= phase < 20:
        p.hp = 5


def main() -> None:
    period = 0.05
    layout = Layout()
    print(json.dumps(layout.info()), flush=True)
    start = time.time()
    try:
        while True:
            phase = int((time.time() - start) / period) % 20
            update(layout, phase)
            time.sleep(period)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
