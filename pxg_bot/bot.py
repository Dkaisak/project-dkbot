from __future__ import annotations

import time
from typing import Optional

from .behaviors import Blackboard, build_behaviors
from .humanizer import Humanizer
from .models import CreatureKind, GameState
from .pathfinding import Route


class Bot:
    def __init__(self, source, inp, cfg: dict, verbose: bool = False):
        self.source = source
        self.inp = inp
        self.cfg = cfg
        self.verbose = verbose
        self.ticks = 0
        self.loops = 0
        self.humanizer = Humanizer(cfg.get("settings", {}).get("humanizer", {}))
        route = self._build_route(cfg)
        self.route = route
        self.bb = Blackboard(route=route, humanizer=self.humanizer)
        self.behaviors = build_behaviors(cfg, route)
        self.break_until = 0.0
        self.session_play_until: Optional[float] = None
        if self.humanizer.enabled and self.humanizer.cfg.get("session_enabled"):
            self.session_play_until = time.time() + self.humanizer.uniform_range(
                self.humanizer.cfg.get("play_minutes", [45, 120])
            ) * 60

    @staticmethod
    def _build_route(cfg: dict) -> Optional[Route]:
        rcfg = cfg.get("route") or {}
        if not rcfg.get("waypoints"):
            return None
        return Route.from_config(
            rcfg["waypoints"],
            loop=bool(rcfg.get("loop", True)),
            ping_pong=bool(rcfg.get("ping_pong", False)),
        )

    def tick(self):
        state = self.source.read_state()
        self._manage_pause(state)
        chosen = None
        for behavior in self.behaviors:
            if behavior.name != "crisis" and self.bb.paused:
                break
            try:
                if not behavior.evaluate(state, self.bb):
                    continue
                if (
                    behavior.name != "crisis"
                    and self.humanizer.enabled
                    and self.humanizer.chance(self.humanizer.cfg.get("micro_idle_chance", 0))
                ):
                    break
                behavior.act(state, self.bb, self.inp)
                chosen = behavior.name
                break
            except Exception as exc:
                if self.verbose:
                    print(f"[!] {behavior.name}: {exc}")
        self.ticks += 1
        return state, chosen

    def _manage_pause(self, state: GameState) -> None:
        crisis_cfg = self.cfg.get("behaviors", {}).get("crisis", {})
        if self.bb.human_seen:
            still_there = False
            for c in state.creatures:
                if c.is_self:
                    continue
                if c.is_player or c.kind == CreatureKind.PLAYER:
                    if state.player.pos.distance(c.pos) <= int(crisis_cfg.get("player_range", 7)):
                        still_there = True
                        break
            if not still_there:
                self.bb.human_seen = False
        if self.bb.paused and not self.bb.human_seen:
            self.bb.paused = False
            self.bb.stop_reason = ""

    def _should_break(self) -> bool:
        humanizer = self.humanizer
        if not humanizer.enabled:
            return False
        now = time.time()
        if now < self.break_until:
            return True
        if self.session_play_until is not None and now >= self.session_play_until:
            duration = humanizer.uniform_range(humanizer.cfg.get("break_minutes", [5, 25])) * 60
            self.break_until = now + duration
            self.session_play_until = now + duration + humanizer.uniform_range(
                humanizer.cfg.get("play_minutes", [45, 120])
            ) * 60
            return True
        if humanizer.chance(humanizer.cfg.get("pause_chance", 0)):
            self.break_until = now + humanizer.uniform_range(humanizer.cfg.get("pause_seconds", [3, 12]))
            return True
        return False

    def _sleep_seconds(self, base: float) -> float:
        if self.humanizer.enabled:
            return max(0.01, self.humanizer.jitter(base, "tick_jitter_pct"))
        return base

    def run(self, max_ticks: Optional[int] = None) -> None:
        tick_seconds = float(self.cfg.get("settings", {}).get("tick_seconds", 0.1))
        while max_ticks is None or self.loops < max_ticks:
            self.loops += 1
            if self._should_break():
                if self.verbose:
                    remaining = max(0, int(self.break_until - time.time()))
                    print(f"[break] pausa humana, {remaining}s restantes")
                time.sleep(self._sleep_seconds(tick_seconds))
                continue
            state, chosen = self.tick()
            if self.verbose:
                print(self._format(state, chosen))
            time.sleep(self._sleep_seconds(tick_seconds))

    def _format(self, state: GameState, chosen: Optional[str]) -> str:
        p = state.player
        flag = f"PAUSA({self.bb.stop_reason})" if self.bb.paused else (chosen or "idle")
        return (
            f"t={self.ticks:04d} pos=({p.pos.x},{p.pos.y},{p.pos.z}) "
            f"hp={p.hp_pct}% mp={p.mp_pct}% enemigos={sum(1 for c in state.creatures if c.attackable)} "
            f"-> {flag}"
        )
