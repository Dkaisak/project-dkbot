from __future__ import annotations

import heapq
from typing import Callable, Optional

from .models import Vec3

NEIGHBORS = [
    (-1, -1), (0, -1), (1, -1),
    (-1, 0), (1, 0),
    (-1, 1), (0, 1), (1, 1),
]


def astar(
    start: Vec3,
    goal: Vec3,
    walkable: Callable[[int, int], bool],
    max_nodes: int = 20000,
) -> Optional[list[Vec3]]:
    if start.x == goal.x and start.y == goal.y:
        return []
    if start.z != goal.z:
        return None

    start_t = (start.x, start.y)
    goal_t = (goal.x, goal.y)
    open_heap = [(0, start_t)]
    came_from: dict[tuple[int, int], tuple[int, int]] = {}
    g_score = {start_t: 0}
    visited = 0

    while open_heap:
        _, current = heapq.heappop(open_heap)
        if current == goal_t:
            return _reconstruct(came_from, current, start.z)
        visited += 1
        if visited > max_nodes:
            return None
        cx, cy = current
        for dx, dy in NEIGHBORS:
            nx, ny = cx + dx, cy + dy
            if (nx, ny) == goal_t or (walkable(nx, ny) and _diag_ok(walkable, cx, cy, dx, dy)):
                tentative = g_score[current] + (1 if dx == 0 or dy == 0 else 1)
                if tentative < g_score.get((nx, ny), 1 << 30):
                    came_from[(nx, ny)] = current
                    g_score[(nx, ny)] = tentative
                    heapq.heappush(open_heap, (tentative + _h(nx, ny, goal_t), (nx, ny)))
    return None


def _diag_ok(walkable, cx, cy, dx, dy) -> bool:
    if dx == 0 or dy == 0:
        return True
    return walkable(cx + dx, cy) and walkable(cx, cy + dy)


def _h(x: int, y: int, goal: tuple[int, int]) -> int:
    return max(abs(x - goal[0]), abs(y - goal[1]))


def _reconstruct(came_from, current, z: int) -> list[Vec3]:
    path = [Vec3(current[0], current[1], z)]
    while current in came_from:
        current = came_from[current]
        path.append(Vec3(current[0], current[1], z))
    path.reverse()
    return path


class Route:
    def __init__(self, waypoints: list[Vec3], loop: bool = True, ping_pong: bool = False,
                 actions: Optional[list] = None):
        if not waypoints:
            raise ValueError("la ruta no puede estar vacía")
        self.waypoints = waypoints
        self.loop = loop
        self.ping_pong = ping_pong
        self.index = 0
        self.forward = True
        # Accion opcional por waypoint: "up"/"down" = en este punto hay que
        # subir/bajar de piso (la casilla es la escalera del piso actual).
        self.actions: list = list(actions) if actions else [None] * len(waypoints)
        if len(self.actions) < len(self.waypoints):
            self.actions += [None] * (len(self.waypoints) - len(self.actions))

    @staticmethod
    def _parse_action(value) -> Optional[str]:
        raw = str(value).strip().lower() if value is not None else ""
        return {"up": "up", "subir": "up", "+": "up",
                "down": "down", "bajar": "down", "-": "down"}.get(raw)

    @classmethod
    def from_config(cls, cfg: list[list[int]], loop: bool = True, ping_pong: bool = False) -> "Route":
        waypoints = [Vec3(int(p[0]), int(p[1]), int(p[2]) if len(p) > 2 else 0) for p in cfg]
        actions = [cls._parse_action(p[3]) if len(p) > 3 else None for p in cfg]
        return cls(waypoints, loop=loop, ping_pong=ping_pong, actions=actions)

    @property
    def current(self) -> Vec3:
        return self.waypoints[self.index]

    @property
    def current_action(self) -> Optional[str]:
        if 0 <= self.index < len(self.actions):
            return self.actions[self.index]
        return None

    def action_at(self, index: int) -> Optional[str]:
        if 0 <= index < len(self.actions):
            return self.actions[index]
        return None

    def is_finished(self) -> bool:
        return not self.loop and self.index >= len(self.waypoints) - 1

    def advance(self) -> None:
        if self.ping_pong:
            if self.forward and self.index >= len(self.waypoints) - 1:
                self.forward = False
            elif not self.forward and self.index <= 0:
                self.forward = True
            self.index += 1 if self.forward else -1
        else:
            self.index += 1
            if self.index >= len(self.waypoints):
                self.index = 0 if self.loop else len(self.waypoints) - 1

    def next_direction(self, player_pos: Vec3, arrive_distance: int = 0) -> Optional[int]:
        wp = self.current
        if player_pos.distance(wp) <= arrive_distance:
            self.advance()
            wp = self.current
        direction = player_pos.direction_to(wp)
        if direction < 0:
            self.advance()
            return player_pos.direction_to(self.current)
        return direction

    def plan(self, player_pos: Vec3, walkable, arrive_distance: int = 0) -> Optional[list[Vec3]]:
        wp = self.current
        if player_pos.distance(wp) <= arrive_distance:
            self.advance()
            wp = self.current
        return astar(player_pos, wp, walkable)
