"""Tabla aprendida `nombre de item -> id` (para la whitelist de loot).

El cliente **no expone nombres de item** (ni `ThingType` ni `Item` los tienen),
pero el **mensaje de botin del servidor** si trae los nombres, p. ej.:

    Botin de Charizard: straws (20) y essences of fire (9).

El agente captura ademas **que ids desaparecen** al lootear (el diff de items
alrededor antes/despues de `collectLoot`). Cuando el numero de nombres y de ids
coincide, se mapean por orden. Asi el usuario puede escribir NOMBRES en la
whitelist y el bot los traduce a ids para decidir que cuerpos lootear.
"""
from __future__ import annotations

import json
import os
import re

# separadores: "a, b y c" (es) / "a, b and c" (en)
_SPLIT = re.compile(r"\s+y\s+|\s+and\s+|,|;", re.I)
_COUNT = re.compile(r"\(\s*\d+\s*\)")


class LootItems:
    def __init__(self, path: str):
        self.path = path
        self.map: dict[str, int] = {}   # nombre(minusculas) -> id
        self._dirty = False
        self.load()

    def load(self) -> None:
        if not self.path:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            for name, iid in (data.get("items") or {}).items():
                try:
                    self.map[str(name).lower()] = int(iid)
                except (TypeError, ValueError):
                    pass
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        if not self._dirty or not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"items": self.map}, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
            self._dirty = False
        except OSError:
            pass

    @staticmethod
    def parse_names(msg: str) -> list:
        """Nombres de item del mensaje de botin."""
        m = re.search(r"bot[ií]n[^:]*:\s*(.+)$", msg or "", re.I)
        part = (m.group(1) if m else (msg or "")).strip()
        part = part.rstrip(".").strip()
        names = []
        for tok in _SPLIT.split(part):
            tok = _COUNT.sub("", tok).strip().strip(".").strip()
            if tok:
                names.append(tok)
        return names

    def observe(self, msg: str, ids: list) -> None:
        """Aprende nombre->id cuando el nº de nombres y de ids coincide."""
        names = self.parse_names(msg)
        if not names or not ids or len(names) != len(ids):
            return
        for name, iid in zip(names, ids):
            try:
                iid = int(iid)
            except (TypeError, ValueError):
                continue
            key = name.lower()
            if self.map.get(key) != iid:
                self.map[key] = iid
                self._dirty = True

    def set(self, name: str, iid) -> None:
        key = str(name).strip().lower()
        if not key:
            return
        try:
            iid = int(iid)
        except (TypeError, ValueError):
            return
        if self.map.get(key) != iid:
            self.map[key] = iid
            self._dirty = True

    def remove(self, name: str) -> None:
        if self.map.pop(str(name).strip().lower(), None) is not None:
            self._dirty = True

    def id_for(self, name: str):
        return self.map.get(str(name).strip().lower())

    def as_dict(self) -> dict:
        return dict(self.map)
