"""Mapa persistente Pokemon -> habilidades y orden de combo.

Cada Pokemon tiene ataques distintos. Cuando un Pokemon esta activo, el agente
expone sus skills (name, aoe, effect, key); aqui las guardamos por nombre. Ademas
guardamos un "orden" (combo) por Pokemon, editable desde la GUI.
"""
from __future__ import annotations

import json
import os


class PokemonSkills:
    def __init__(self, path: str):
        self.path = path
        self.data: dict[str, dict] = {}
        self.order: dict[str, list] = {}
        self.lure_order: dict[str, list] = {}
        self._dirty = False
        self._order_mtime = 0.0
        self.load()

    def load(self) -> None:
        if not self.path:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                d = json.load(handle)
            self.data = d.get("pokemon", {}) if isinstance(d, dict) else {}
            self.order = d.get("order", {}) if isinstance(d, dict) else {}
            self.lure_order = d.get("lure_order", {}) if isinstance(d, dict) else {}
            self._order_mtime = os.path.getmtime(self.path)
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        if not self._dirty or not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            # preservar el orden editado en la GUI (recargarlo antes de escribir)
            self._reload_order()
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump({"pokemon": self.data, "order": self.order,
                           "lure_order": self.lure_order},
                          handle, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
            self._order_mtime = os.path.getmtime(self.path)
            self._dirty = False
        except OSError:
            pass

    def _reload_order(self) -> None:
        if not self.path:
            return
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return
        if mtime == self._order_mtime:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                d = json.load(handle)
            if isinstance(d, dict):
                self.order = d.get("order", {}) or {}
                self.lure_order = d.get("lure_order", {}) or {}
            self._order_mtime = mtime
        except (OSError, ValueError):
            pass

    def observe(self, name: str, moves: list) -> None:
        if not name or not moves:
            return
        skills = self.data.get(name, {})
        changed = name not in self.data
        for m in moves:
            key = str(m.get("key", ""))
            if not key:
                continue
            entry = {
                "name": str(m.get("name", "")),
                "aoe": bool(m.get("aoe")),
                "effect": str(m.get("effect", "")),
            }
            if skills.get(key) != entry:
                skills[key] = entry
                changed = True
        if changed:
            self.data[name] = skills
            self._dirty = True

    def order_for(self, name: str) -> list:
        """Orden (combo) configurado para un Pokemon; recarga si la GUI lo cambio."""
        self._reload_order()
        return list(self.order.get(name, []))

    def lure_order_for(self, name: str) -> list:
        """Combo propio del lure (skills y orden) para un Pokemon. Vacio = sin
        combo propio (el lure usa la logica por defecto)."""
        self._reload_order()
        return list(self.lure_order.get(name, []))

    def aoe_keys(self, name: str) -> list:
        skills = self.data.get(name, {})
        return [k for k, v in skills.items() if v.get("aoe")]
