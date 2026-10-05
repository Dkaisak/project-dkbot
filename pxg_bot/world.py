"""Mapa mundial para la exploracion/patrulla.

Fuente de transitabilidad: el minimapa OTClient (.otmm) si esta disponible
(cubre el mundo entero con flags NotWalkable/NotPathable). Si no, cae a los
tiles que el jugador ha pisado (ground truth).

Cobertura: se marcan como "cubiertos" los tiles dentro de `coverage_radius` de
cada posicion visitada; los objetivos son los tiles transitables de la zona aun
no cubiertos. Cuando no quedan, se completan zonas no vistas (fronteras) y luego
se patrulla en bucle.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from .models import Vec3
from .pathfinding import astar

CARDINALS = ((0, -1), (0, 1), (-1, 0), (1, 0))


class WorldMap:
    def __init__(self, path: str, radius: int = 60, patrol_step: int = 4,
                 coverage_radius: int = 2, otmm=None):
        self.path = path
        self.radius = radius
        self.patrol_step = patrol_step
        self.coverage_radius = coverage_radius
        self.otmm = otmm
        self.z: Optional[int] = None
        self.home: Optional[tuple[int, int, int]] = None
        self.known: set[tuple[int, int]] = set()      # tiles pisados
        self.blocked: set[tuple[int, int]] = set()
        self.covered: set[tuple[int, int]] = set()
        self.patrol: list[list[int]] = []
        self._dirty = False
        self._zone_key = None
        self._zone: list[tuple[int, int]] = []
        self._frontier_key = None
        self._frontiers: list[tuple[int, int]] = []

    # --- transitabilidad ---
    def walkable(self, x: int, y: int) -> bool:
        if self.otmm is not None and self.otmm.ready and self.z is not None:
            return self.otmm.pathable(x, y, self.z)
        return (x, y) in self.known and (x, y) not in self.blocked

    def within(self, x: int, y: int) -> bool:
        if not self.home:
            return True
        hx, hy, _ = self.home
        return max(abs(x - hx), abs(y - hy)) <= self.radius

    # --- zona (tiles transitables dentro del radio) ---
    def zone_tiles(self) -> list[tuple[int, int]]:
        if self.otmm is None or not self.otmm.ready or self.z is None or not self.home:
            return []
        key = (self.home, self.radius, self.z, self.otmm._mtime)
        if key == self._zone_key:
            return self._zone
        hx, hy, _ = self.home
        r = self.radius
        tiles = []
        for x in range(hx - r, hx + r + 1):
            for y in range(hy - r, hy + r + 1):
                if self.otmm.pathable(x, y, self.z):
                    tiles.append((x, y))
        self._zone = tiles
        self._zone_key = key
        return tiles

    def coverage_targets(self) -> list[tuple[int, int]]:
        return [t for t in self.zone_tiles() if t not in self.covered]

    def frontiers(self) -> list[tuple[int, int]]:
        if self.otmm is None or not self.otmm.ready or self.z is None:
            out = set()
            for (x, y) in self.known:
                for dx, dy in CARDINALS:
                    nx, ny = x + dx, y + dy
                    if (nx, ny) in self.known or (nx, ny) in self.blocked:
                        continue
                    if self.within(nx, ny):
                        out.add((nx, ny))
            return list(out)
        key = (self.home, self.radius, self.z, self.otmm._mtime)
        if key == self._frontier_key:
            return self._frontiers
        out = set()
        for (x, y) in self.zone_tiles():
            for dx, dy in CARDINALS:
                nx, ny = x + dx, y + dy
                if self.within(nx, ny) and not self.otmm.seen(nx, ny, self.z):
                    out.add((nx, ny))
        self._frontiers = list(out)
        self._frontier_key = key
        return self._frontiers

    # --- cambios ---
    def set_otmm(self, mm) -> None:
        self.otmm = mm
        self._zone_key = None
        self._frontier_key = None

    def visit(self, pos: Vec3) -> None:
        if self.z is None:
            self.z = pos.z
        if pos.z != self.z:
            return
        if self.home is None:
            self.home = (pos.x, pos.y, pos.z)
        tile = (pos.x, pos.y)
        if tile not in self.known:
            self.known.add(tile)
            self._dirty = True
        r = max(0, self.coverage_radius)
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                self.covered.add((pos.x + dx, pos.y + dy))
        self._dirty = True
        step = max(1, self.patrol_step)
        if not self.patrol or max(abs(pos.x - self.patrol[-1][0]),
                                  abs(pos.y - self.patrol[-1][1])) >= step:
            self.patrol.append([pos.x, pos.y, pos.z])

    def mark_blocked(self, x: int, y: int) -> None:
        self.blocked.add((x, y))
        self.known.discard((x, y))
        self._dirty = True

    # --- planificacion ---
    def plan_to(self, start: Vec3, goal_x: int, goal_y: int, max_nodes: int = 20000):
        goal = Vec3(goal_x, goal_y, start.z)
        return astar(start, goal, self.walkable, max_nodes=max_nodes)

    def plan_coverage(self, start: Vec3, blacklist: set, limit: int = 12):
        targets = self.coverage_targets()
        if not targets:
            return None
        targets = [t for t in targets if t not in blacklist]
        targets.sort(key=lambda p: max(abs(p[0] - start.x), abs(p[1] - start.y)))
        for (gx, gy) in targets[:limit]:
            path = self.plan_to(start, gx, gy)
            if path:
                return path
        return None

    def plan_to_frontier(self, start: Vec3, blacklist: set, limit: int = 40):
        candidates = [t for t in self.frontiers() if t not in blacklist]
        if not candidates:
            return None
        candidates.sort(key=lambda p: max(abs(p[0] - start.x), abs(p[1] - start.y)))
        for (gx, gy) in candidates[:limit]:
            path = self.plan_to(start, gx, gy)
            if path:
                return path
        return None

    # --- persistencia ---
    def save(self) -> None:
        if not self.path or not self._dirty:
            return
        data = {
            "z": self.z,
            "home": list(self.home) if self.home else None,
            "radius": self.radius,
            "coverage_radius": self.coverage_radius,
            "known": [list(t) for t in sorted(self.known)],
            "blocked": [list(t) for t in sorted(self.blocked)],
            "covered": [list(t) for t in sorted(self.covered)],
            "patrol": self.patrol,
        }
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.replace(tmp, self.path)
            self._dirty = False
        except OSError:
            pass

    def load(self) -> None:
        if not self.path:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return
        self.z = data.get("z")
        home = data.get("home")
        self.home = tuple(home) if home else None
        self.radius = int(data.get("radius", self.radius))
        self.coverage_radius = int(data.get("coverage_radius", self.coverage_radius))
        self.known = {tuple(t) for t in data.get("known", [])}
        self.blocked = {tuple(t) for t in data.get("blocked", [])}
        self.covered = {tuple(t) for t in data.get("covered", [])}
        self.patrol = data.get("patrol", [])
