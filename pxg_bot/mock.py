from __future__ import annotations

import time

from .models import Creature, CreatureKind, GameState, Player, Pokemon, Vec3


class MockStateSource:
    """Fuente de estado scriptada para probar la lógica sin el cliente."""

    def __init__(self, cfg: dict, verbose: bool = False):
        self.cfg = cfg
        self.verbose = verbose
        self.t = 0

    def read_state(self) -> GameState:
        self.t += 1
        phase = self.t % 20
        player = Player(
            name="Hero", pos=Vec3(100, 100, 7), hp=100, max_hp=100,
            mp=50, max_mp=50, level=10, alive=True,
        )
        creatures: list[Creature] = []
        party = [Pokemon(slot=0, name="Pikachu", hp_pct=100, level=10, active=True, alive=True)]

        if 3 <= phase < 8:
            hp = [100, 80, 55, 35, 20][phase - 3]
            creatures.append(
                Creature(cid=1001, name="Rattata", pos=Vec3(104, 100, 7),
                         hp_pct=hp, kind=CreatureKind.POKEMON, is_wild=True)
            )
        if phase == 8:
            party = [Pokemon(slot=0, name="Pikachu", hp_pct=30, level=10, active=True, alive=True)]
        if 10 <= phase < 13:
            creatures.append(
                Creature(cid=2002, name="Enemy", pos=Vec3(102, 100, 7),
                         hp_pct=100, kind=CreatureKind.PLAYER, is_player=True)
            )
        if phase == 15:
            creatures.append(
                Creature(cid=1001, name="Rattata", pos=Vec3(101, 100, 7),
                         hp_pct=0, kind=CreatureKind.POKEMON, is_wild=True)
            )
        if phase == 18:
            player.hp = 5

        return GameState(
            player=player,
            creatures=creatures,
            party=party,
            timestamp=time.time(),
            connected=phase != 16,
            in_battle=any(c.attackable for c in creatures),
        )
