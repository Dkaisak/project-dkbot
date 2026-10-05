"""Lista de criaturas ignoradas (inatacables).

El cliente no expone ningun flag para NPCs o pokemon de NPC: comparten
``isMonster`` y a veces el mismo nombre/outfit que los salvajes (p.ej. Manectric
salvaje usa el mismo outfit 1871 que el de un NPC). Por eso mantenemos una lista
persistente con dos criterios:

- ``names``: nombres a ignorar siempre (NPCs como "Andrea").
- ``ids``: ids de criatura concretos, auto-aprendidos cuando el bot ataca y no
  consigue hacer dano tras unos segundos. No afecta a los salvajes de la misma
  especie, que si reciben dano.
"""
from __future__ import annotations

import json
import os


class IgnoreTable:
    def __init__(self, path: str):
        self.path = path
        self.names: set[str] = set()
        self.ids: set[str] = set()
        self._dirty = False
        self._mtime = None
        self.load()

    def load(self) -> None:
        if not self.path:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            self.names = {str(n) for n in (data.get("names") or [])}
            self.ids = {str(i) for i in (data.get("ids") or [])}
        except (OSError, ValueError):
            return
        try:
            self._mtime = os.path.getmtime(self.path)
        except OSError:
            self._mtime = None

    def maybe_reload(self) -> None:
        # recarga si la GUI (u otro proceso) modifico el archivo
        if not self.path:
            return
        try:
            m = os.path.getmtime(self.path)
        except OSError:
            return
        if m != self._mtime:
            self._dirty = False
            self.load()

    def save(self) -> None:
        if not self._dirty or not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            data = {"names": sorted(self.names), "ids": sorted(self.ids)}
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
            os.replace(tmp, self.path)
            self._dirty = False
            try:
                self._mtime = os.path.getmtime(self.path)
            except OSError:
                self._mtime = None
        except OSError:
            pass

    def is_ignored(self, name: str, uid: str = "") -> bool:
        return (name in self.names) or (bool(uid) and uid in self.ids)

    def add_name(self, name: str) -> None:
        name = str(name or "").strip()
        if name and name not in self.names:
            self.names.add(name)
            self._dirty = True

    def remove_name(self, name: str) -> None:
        if name in self.names:
            self.names.discard(name)
            self._dirty = True

    def add_id(self, uid: str) -> None:
        uid = str(uid or "")
        if uid and uid != "0" and uid not in self.ids:
            self.ids.add(uid)
            self._dirty = True

    def remove_id(self, uid: str) -> None:
        uid = str(uid or "")
        if uid in self.ids:
            self.ids.discard(uid)
            self._dirty = True

    def as_dict(self) -> dict:
        return {"names": sorted(self.names), "ids": sorted(self.ids)}
