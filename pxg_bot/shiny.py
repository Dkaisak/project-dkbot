"""Deteccion de shinies.

El cliente no expone un flag: el shiny usa un outfit (lookType) distinto al del
mismo Pokemon normal (ej. Rattata normal 36, shiny 512). Mantenemos una tabla
persistente nombre->outfit_normal (el mas frecuente observado) y marcamos como
shiny cualquier criatura cuyo outfit difiera, una vez que conocemos el normal.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from .models import CreatureKind


class ShinyTable:
    def __init__(self, path: str, min_obs: int = 3):
        self.path = path
        self.min_obs = min_obs
        self.counts: dict[str, dict[int, int]] = {}
        self._dirty = False
        self.load()

    def load(self) -> None:
        if not self.path:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            for name, m in (data.get("counts") or {}).items():
                self.counts[str(name)] = {int(k): int(v) for k, v in m.items()}
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        if not self._dirty or not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            data = {"counts": {n: {str(k): v for k, v in m.items()} for n, m in self.counts.items()}}
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.replace(tmp, self.path)
            self._dirty = False
        except OSError:
            pass

    def normal(self, name: str) -> Optional[int]:
        m = self.counts.get(name)
        if not m:
            return None
        outfit, cnt = max(m.items(), key=lambda kv: kv[1])
        return outfit if cnt >= self.min_obs else None

    def observe(self, creatures) -> None:
        for c in creatures:
            if c.outfit is None or c.is_player or c.is_npc or c.is_self:
                continue
            if c.kind != CreatureKind.MONSTER:
                continue
            m = self.counts.setdefault(c.name, {})
            m[c.outfit] = m.get(c.outfit, 0) + 1
            self._dirty = True

    def is_shiny(self, c) -> bool:
        if c.outfit is None or c.is_player or c.is_npc or c.is_self:
            return False
        base = self.normal(c.name)
        return base is not None and c.outfit != base
