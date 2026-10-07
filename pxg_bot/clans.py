"""Tabla de clanes: mapea el *skull* de un jugador a su clan.

En PokeXGames el simbolo del clan se dibuja en el nameplate como el `skull` de
la criatura (ids **custom 50-88**, p. ej. `custom_62`). El cliente NO traduce ese
id a nombre (lo manda el servidor), asi que mantenemos una tabla persistente y
editable `skull -> clan`.

`pxg_clans.json`:  {"clans": {"62": "wingeon", "63": "malefic", ...}}

Semilla conocida: `62 = wingeon`. El resto se completa observando jugadores.
"""
from __future__ import annotations

import json
import os
from typing import Optional

# Clanes del juego (para referencia / validacion en la GUI).
KNOWN_CLANS = [
    "naturia", "gardestrike", "malefic", "wingeon", "raibolt", "psycraft",
    "orebound", "seavell", "volcanic", "ironhard",
]

# Semilla confirmada en vivo.
DEFAULT_CLANS = {
    62: "wingeon",
}


class ClanTable:
    def __init__(self, path: str = ""):
        self.path = path
        self.map: dict[str, str] = {str(k): str(v) for k, v in DEFAULT_CLANS.items()}
        self._mtime = None
        self.load()

    def load(self) -> None:
        if not self.path:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return
        clans = data.get("clans", data) if isinstance(data, dict) else {}
        if isinstance(clans, dict):
            for k, v in clans.items():
                self.map[str(k)] = str(v)
        try:
            self._mtime = os.path.getmtime(self.path)
        except OSError:
            self._mtime = None

    def maybe_reload(self) -> None:
        """Recarga si el fichero cambio (edicion externa / GUI)."""
        if not self.path:
            return
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return
        if mtime != self._mtime:
            self.map = {str(k): str(v) for k, v in DEFAULT_CLANS.items()}
            self.load()

    def clan_for(self, skull) -> str:
        if skull is None or skull == "":
            return ""
        try:
            return self.map.get(str(int(skull)), "")
        except (TypeError, ValueError):
            return self.map.get(str(skull), "")

    def set(self, skull, clan: str) -> None:
        try:
            self.map[str(int(skull))] = str(clan or "")
        except (TypeError, ValueError):
            pass

    def save(self) -> None:
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump({"clans": self.map}, handle, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
            self._mtime = os.path.getmtime(self.path)
        except OSError:
            pass

    def as_dict(self) -> dict:
        return {"clans": dict(self.map), "known": KNOWN_CLANS}
