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
    def __init__(self, path: str, min_obs: int = 5):
        self.path = path
        self.min_obs = min_obs
        self.counts: dict[str, dict[int, int]] = {}
        # outfits aceptados como NORMALES por especie (allow-list). Sirve para
        # variantes de un mismo Pokemon que NO son shiny y que el heuristico
        # "outfit != mas visto" marcaria en falso.
        self.normals: dict[str, set] = {}
        self._dirty = False
        self._seen: set[str] = set()
        self.load()

    def load(self) -> None:
        if not self.path:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            for name, m in (data.get("counts") or {}).items():
                self.counts[str(name)] = {int(k): int(v) for k, v in m.items()}
            for name, arr in (data.get("normals") or {}).items():
                self.normals[str(name)] = {int(o) for o in arr}
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        if not self._dirty or not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            data = {
                "counts": {n: {str(k): v for k, v in m.items()} for n, m in self.counts.items()},
                "normals": {n: sorted(o) for n, o in self.normals.items() if o},
            }
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
        ranked = sorted(m.items(), key=lambda kv: kv[1], reverse=True)
        outfit, cnt = ranked[0]
        if cnt < self.min_obs:
            # pocas observaciones: aun no sabemos cual es el outfit normal. Marcar
            # aqui generaba falsos positivos (p. ej. Fearow con 4 muestras).
            return None
        # Ambiguedad: si hay un segundo outfit con un conteo significativo, la
        # especie tiene variantes legitimas (formas/regiones) y no podemos decir
        # cual es shiny -> no marcar nada (mejor no avisar que avisar mal).
        if len(ranked) > 1:
            second = ranked[1][1]
            if second >= max(2, int(cnt * 0.2)):
                return None
        return outfit

    def observe(self, creatures) -> None:
        def key(c) -> str:
            uid = str(getattr(c, "uid", "") or "")
            if uid:
                return uid
            return f"{c.name}:{c.pos.x}:{c.pos.y}:{c.pos.z}"

        # limpiar de _seen las criaturas que ya no estan (nuevo encuentro -> cuenta)
        self._seen &= {key(c) for c in creatures}
        for c in creatures:
            # no contar jugadores, npcs, uno mismo ni MI PROPIO pokemon (summon),
            # que esta en pantalla cada tick y contaminaba la tabla.
            if c.outfit is None or c.is_player or c.is_npc or c.is_self or c.is_summon:
                continue
            if c.kind != CreatureKind.MONSTER:
                continue
            k = key(c)
            if k in self._seen:
                continue  # ya contado en este encuentro (dedupe por instancia)
            self._seen.add(k)
            m = self.counts.setdefault(c.name, {})
            m[c.outfit] = m.get(c.outfit, 0) + 1
            self._dirty = True

    def is_shiny(self, c) -> bool:
        if (c.outfit is None or c.is_player or c.is_npc or c.is_self
                or getattr(c, "is_summon", False)):
            return False
        base = self.normal(c.name)
        return base is not None and c.outfit != base
