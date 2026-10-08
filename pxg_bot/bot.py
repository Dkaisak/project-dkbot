from __future__ import annotations

import atexit
import json
import os
import time
from typing import Optional

from . import control as control_io
from .behaviors import Blackboard, build_behaviors
from .humanizer import Humanizer
from .models import CreatureKind, GameState, Player, Vec3
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
        self.bb = Blackboard(route=route, humanizer=self.humanizer,
                             ignore=getattr(self.source, "ignore", None))
        self.control_file = control_io.control_path(cfg)
        self.status_file = control_io.status_path(cfg)
        self._pid_file = self._derive_pid_file()
        self.world = self._build_world(cfg)
        self._densify_route()
        self.llm = self._build_llm(cfg)
        self.telemetry = self._build_telemetry(cfg)
        self.behaviors = build_behaviors(cfg, route, self.world, llm=self.llm)
        self._revive_behavior = next((b for b in self.behaviors if b.name == "revive"), None)
        self._orig_enabled = {b.name: b.cfg.get("enabled", True) for b in self.behaviors}
        rcfg0 = cfg.get("route") or {}
        self._set_route_pokemon(rcfg0.get("pokemon", ""), rcfg0.get("pokemon_slot"))
        self.control_paused = False
        self._control_mtime = 0.0
        self._control_checked = 0.0
        self._route_fp = None
        self._last_order = None
        self.last_chosen = None
        self.started_at = time.time()
        self.break_until = 0.0
        self.session_play_until: Optional[float] = None
        if self.humanizer.enabled and self.humanizer.cfg.get("session_enabled"):
            self.session_play_until = time.time() + self.humanizer.uniform_range(
                self.humanizer.cfg.get("play_minutes", [30, 90])
            ) * 60

    def _derive_pid_file(self) -> str:
        base = self.status_file or self.control_file or ""
        if not base:
            return ""
        return os.path.join(os.path.dirname(base), "pxg_bot.pid")

    def _build_llm(self, cfg: dict):
        llm_cfg = cfg.get("llm", {}) or {}
        if not llm_cfg.get("enabled", False):
            return None
        from .llm import build_controller

        return build_controller(llm_cfg, cfg)

    def _build_telemetry(self, cfg: dict):
        from .telemetry import Telemetry

        return Telemetry(cfg)

    def _build_world(self, cfg: dict):
        base = self.status_file or self.control_file or ""
        if not base:
            return None
        from .otmm import Minimap
        from .world import WorldMap

        path = os.path.join(os.path.dirname(base), "pxg_world.json")
        ecfg = cfg.get("behaviors", {}).get("explore", {})
        otmm = None
        mm_file = cfg.get("lua", {}).get("minimap_file", "")
        if mm_file:
            otmm = Minimap(mm_file)
            if not otmm.ensure():
                otmm = None
        world = WorldMap(path, radius=int(ecfg.get("radius", 60)),
                         patrol_step=int(ecfg.get("patrol_step", 4)),
                         coverage_radius=int(ecfg.get("coverage_radius", 2)),
                         otmm=otmm)
        world.load()
        return world

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
        try:
            state = self.source.read_state()
        except Exception as exc:
            # un estado malformado no debe matar el bot: se trata como desconexion
            if self.verbose:
                print(f"[!] read_state: {exc}")
            state = GameState(player=Player(), connected=False)
        self._manage_pause(state)
        # la capa LLM solo observa: decide en su hilo, sin bloquear el tick
        if self.llm is not None:
            self.llm.observe(state, self._llm_extra())
        # urgencia de revive: si el pokemon de la ruta esta debilitado, revive manda
        self.bb.notes["revive_urgent"] = self._revive_is_urgent(state)
        # sin revives -> logout + detener (proactivo, en cuanto llega a 0)
        self._check_revives_out(state)
        if not (self.bb.paused or self.control_paused):
            self.bb.update_corpses(state)
        chosen = None
        for behavior in self.behaviors:
            if behavior.name != "crisis" and (self.bb.paused or self.control_paused):
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
        if self.telemetry is not None:
            self.telemetry.observe(state, chosen, self.bb)
        self.ticks += 1
        self.last_chosen = chosen
        return state, chosen

    def refresh_control(self, force: bool = False) -> None:
        """Lee el canal de control (si cambio) y aplica pausa/toggles/explore."""
        if not self.control_file:
            return
        now = time.time()
        if not force and now - self._control_checked < 0.2:
            return
        self._control_checked = now
        try:
            mtime = os.path.getmtime(self.control_file)
        except OSError:
            return
        if not force and mtime == self._control_mtime:
            return
        self._control_mtime = mtime
        data = control_io.read_control(self.control_file)
        self.control_paused = bool(data.get("paused", False))
        for behavior in self.behaviors:
            override = (data.get("behaviors", {}) or {}).get(behavior.name, {})
            behavior.cfg["enabled"] = bool(override.get("enabled", self._orig_enabled.get(behavior.name, True)))
        explore = data.get("explore")
        if isinstance(explore, dict) and explore:
            self.cfg.setdefault("behaviors", {}).setdefault("explore", {}).update(explore)
        capture = data.get("capture")
        if isinstance(capture, dict) and capture:
            for behavior in self.behaviors:
                if behavior.name == "capture":
                    behavior.cfg.update(capture)
        combat = data.get("combat")
        if isinstance(combat, dict) and combat:
            for behavior in self.behaviors:
                if behavior.name == "combat":
                    behavior.cfg.update(combat)
        revive = data.get("revive")
        if isinstance(revive, dict) and revive:
            for behavior in self.behaviors:
                if behavior.name == "revive":
                    behavior.cfg.update(revive)
        loot = data.get("loot")
        if isinstance(loot, dict) and loot:
            for behavior in self.behaviors:
                if behavior.name == "loot":
                    behavior.cfg.update(loot)
        route = data.get("route")
        if isinstance(route, dict) and route.get("waypoints") is not None:
            self._apply_route(route)
        # parametros del behavior de ruta (idle de inicio, etc.) en caliente
        rbeh = data.get("route_behavior")
        if isinstance(rbeh, dict) and rbeh:
            for behavior in self.behaviors:
                if behavior.name == "route":
                    behavior.cfg.update(rbeh)
        order = data.get("order")
        if isinstance(order, (list, tuple)) and len(order) >= 2:
            key = (int(order[0]), int(order[1]))
            if key != self._last_order:
                self._last_order = key
                z = int(order[2]) if len(order) > 2 else 0
                self.inp.order(Vec3(int(order[0]), int(order[1]), z))

    def _apply_route(self, route: dict) -> None:
        wps = route.get("waypoints") or []
        pokemon = str(route.get("pokemon", "") or "")
        pslot = route.get("pokemon_slot")
        fp = (tuple(tuple(int(v) for v in w) for w in wps),
              bool(route.get("loop", True)), bool(route.get("ping_pong", False)),
              bool(route.get("enabled", True)), pokemon, pslot)
        if fp == self._route_fp:
            return
        self._route_fp = fp
        self.route = Route.from_config(wps, loop=fp[1], ping_pong=fp[2]) if wps else None
        self._densify_route()
        self.bb.route = self.route
        for behavior in self.behaviors:
            if behavior.name == "route":
                behavior.route = self.route
                behavior.cfg["enabled"] = bool(route.get("enabled", True))
        self._set_route_pokemon(pokemon, pslot)

    def _set_route_pokemon(self, name, slot) -> None:
        for behavior in self.behaviors:
            if behavior.name == "route":
                behavior.set_pokemon(name, slot)
            elif behavior.name == "revive":
                # el revive se vincula al slot del Pokemon de la ruta
                behavior.set_route_slot(slot)

    def _revive_is_urgent(self, state) -> bool:
        """Revive de alta prioridad (debilitado / combo agotado / vivos
        stuneados): loot y captura ceden para no robarle el turno."""
        rb = self._revive_behavior
        if rb is None:
            return False
        return rb.urgent(state, self.bb)

    def _check_revives_out(self, state) -> None:
        """Si se acaban los revives -> logout y detener el bot.

        Proactivo: en cuanto la cuenta llega a 0 (tras haber visto >0). Solo actua
        si hay datos fiables de inventario (bag_counts) y el revive esta activo."""
        if self.bb.notes.get("revives_out_done"):
            return
        rb = self._revive_behavior
        if rb is None or not rb.cfg.get("enabled", True):
            return
        if not rb.cfg.get("disconnect_when_out", False):
            return
        left = rb.revives_left(state)
        if left is None:
            return  # sin datos fiables: no decidir
        if left > 0:
            self.bb.notes["revives_seen"] = max(int(self.bb.notes.get("revives_seen", 0)), int(left))
            return
        if int(self.bb.notes.get("revives_seen", 0)) <= 0:
            return  # nunca vimos revives: no desconectar por si acaso
        self.inp.press(str(rb.cfg.get("logout_key", "F12")))
        self.bb.paused = True
        self.bb.stop_reason = "sin revives"
        self.bb.notes["revives_out_done"] = True
        self.bb.notes["quit"] = True
        if self.verbose:
            print("[revive] sin revives -> logout y detener el bot")

    def _densify_route(self) -> None:
        """Sustituye los waypoints por el camino real (astar/otmm) para que la
        ruta siga el corredor y no corte esquinas."""
        route = self.route
        wm = self.world
        otmm = getattr(wm, "otmm", None) if wm is not None else None
        if route is None or wm is None or otmm is None or not getattr(otmm, "ready", False):
            return
        rcfg = self.cfg.get("behaviors", {}).get("route", {})
        if not rcfg.get("densify", True):
            return
        wps = route.waypoints
        n = len(wps)
        if n < 2:
            return
        dense: list[Vec3] = []
        for i in range(n):
            a = wps[i]
            b = wps[(i + 1) % n] if route.loop else (wps[i + 1] if i + 1 < n else None)
            if not dense or dense[-1] != a:
                dense.append(a)
            if b is None:
                continue
            path = wm.plan_to(a, b.x, b.y)
            if path and len(path) > 2:
                for p in path[1:-1]:
                    if dense and dense[-1].x == p.x and dense[-1].y == p.y and dense[-1].z == p.z:
                        continue
                    dense.append(p)
            if len(dense) > 5000:
                break
        if dense:
            route.waypoints = dense

    def write_status(self, chosen: Optional[str], state: Optional[GameState]) -> None:
        if not self.status_file:
            return
        payload = {
            "loop": self.loops,
            "ticks": self.ticks,
            "chosen": chosen or "idle",
            "paused": bool(self.bb.paused or self.control_paused),
            "control_paused": bool(self.control_paused),
            "human_seen": bool(self.bb.human_seen),
            "reason": self.bb.stop_reason,
            "uptime": round(time.time() - self.started_at, 1),
            "connected": bool(getattr(state, "connected", False)) if state else False,
            "humanizer": self.humanizer.profile,
            "counters": dict(self.bb.counters),
            "llm": self.llm.status() if self.llm is not None else {"enabled": False},
            "telemetry": self.telemetry.status() if self.telemetry is not None else {"enabled": False},
            "updated": time.time(),
        }
        try:
            tmp = self.status_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(tmp, self.status_file)
        except OSError:
            pass

    def _llm_extra(self) -> dict:
        """Contexto extra (no presente en GameState) para el LLM: estado de ruta."""
        if self.route is not None:
            return {"route": {"index": self.route.index,
                              "total": len(self.route.waypoints),
                              "loop": bool(self.route.loop)}}
        return {"route": None}

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

    def _should_break(self, busy: bool = False) -> bool:
        humanizer = self.humanizer
        if not humanizer.enabled:
            return False
        # las pausas/descansos no deben cortar una accion en curso
        if humanizer.cfg.get("idle_only_pause", True) and busy:
            return False
        now = time.time()
        if now < self.break_until:
            return True
        if self.session_play_until is not None and now >= self.session_play_until:
            duration = humanizer.uniform_range(humanizer.cfg.get("break_minutes", [1, 5])) * 60
            self.break_until = now + duration
            self.session_play_until = now + duration + humanizer.uniform_range(
                humanizer.cfg.get("play_minutes", [30, 90])
            ) * 60
            return True
        if humanizer.chance(humanizer.cfg.get("pause_chance", 0)):
            self.break_until = now + humanizer.uniform_range(humanizer.cfg.get("pause_seconds", [1, 4]))
            return True
        return False

    def _sleep_seconds(self, base: float) -> float:
        if self.humanizer.enabled:
            return max(0.01, self.humanizer.jitter(base, "tick_jitter_pct"))
        return base

    def run(self, max_ticks: Optional[int] = None) -> None:
        tick_seconds = float(self.cfg.get("settings", {}).get("tick_seconds", 0.1))
        self._write_pid()
        if self.llm is not None:
            self.llm.start()
            if self.verbose and not self.llm.available:
                print(f"[llm] inactivo: {self.llm.error}")
        try:
            while max_ticks is None or self.loops < max_ticks:
                self.loops += 1
                self.refresh_control()
                busy = self.last_chosen not in (None, "idle", "break")
                if self._should_break(busy):
                    self.write_status("break", None)
                    if self.verbose:
                        remaining = max(0, int(self.break_until - time.time()))
                        print(f"[break] pausa humana, {remaining}s restantes")
                    time.sleep(self._sleep_seconds(tick_seconds))
                    continue
                state, chosen = self.tick()
                self.write_status(chosen, state)
                if self.bb.notes.get("quit"):
                    if self.verbose:
                        print("[revive] deteniendo el bot")
                    break
                # mirar alrededor en idle (en vez de quedarse totalmente quieto)
                if (
                    not busy
                    and chosen in (None, "idle")
                    and self.humanizer.enabled
                    and self.bb.ready("look", 1.0)
                    and self.humanizer.should_look_around()
                ):
                    self.inp.turn(self.humanizer.choice([0, 1, 2, 3]))
                    self.bb.mark("look")
                if self.verbose:
                    print(self._format(state, chosen))
                time.sleep(self._sleep_seconds(tick_seconds))
        finally:
            if self.llm is not None:
                self.llm.stop()
            if self.telemetry is not None:
                self.telemetry.close()
            if self.world is not None:
                self.world.save()
            self._remove_pid()

    def _write_pid(self) -> None:
        if not self._pid_file:
            return
        try:
            with open(self._pid_file, "w", encoding="utf-8") as handle:
                handle.write(str(os.getpid()))
            atexit.register(self._remove_pid)
        except OSError:
            pass

    def _remove_pid(self) -> None:
        if not self._pid_file:
            return
        try:
            with open(self._pid_file, "r", encoding="utf-8") as handle:
                if handle.read().strip() != str(os.getpid()):
                    return
            os.remove(self._pid_file)
        except OSError:
            pass

    def _format(self, state: GameState, chosen: Optional[str]) -> str:
        p = state.player
        pend = self.bb.notes.get("corpses_pending", [])
        pd = min((max(abs(p.pos.x - c["x"]), abs(p.pos.y - c["y"])) for c in pend), default=-1)
        eds = [p.pos.distance(c.pos) for c in state.creatures if c.attackable]
        ed = min(eds) if eds else -1
        vis = state.visible or {}
        hw = int(vis.get("w", 21)) // 2
        hh = int(vis.get("h", 11)) // 2
        ve = [c for c in state.creatures if c.attackable
              and abs(c.pos.x - p.pos.x) <= hw and abs(c.pos.y - p.pos.y) <= hh]
        pp = getattr(state, "pokemon_pos", None)
        rx, ry = (pp[0], pp[1]) if pp else (p.pos.x, p.pos.y)
        pkm = f"{rx},{ry}"
        edx = max((max(abs(c.pos.x - rx), abs(c.pos.y - ry)) for c in ve), default=-1)
        rp = next((q.hp_pct for q in state.party if q.slot == 3), None)
        _aoe = [m for m in (state.moves or []) if m.get("aoe")]
        rcd = any(isinstance(m.get("pct"), (int, float)) and m["pct"] < 100 for m in _aoe)
        rve = sum(1 for c in state.creatures if c.attackable
                  and abs(c.pos.x - p.pos.x) <= hw and abs(c.pos.y - p.pos.y) <= hh)
        rpnd = bool(self.bb.notes.get("revive_pending"))
        pkhp = getattr(state, "pokemon_hp", None)
        stun = any("stun" in str(m.get("effect", "")).lower()
                   and isinstance(m.get("pct"), (int, float)) and m["pct"] < 100
                   for m in (state.moves or []))
        if self.control_paused:
            flag = "PAUSA(ui)"
        elif self.bb.paused:
            flag = f"PAUSA({self.bb.stop_reason})"
        else:
            flag = chosen or "idle"
        return (
            f"t={self.ticks:04d} pos=({p.pos.x},{p.pos.y},{p.pos.z}) "
            f"hp={p.hp_pct}% mp={p.mp_pct}% enemigos={sum(1 for c in state.creatures if c.attackable)} "
            f"loot={len(self.bb.notes.get('corpses_pending', []))} lootd={pd} def={len(state.defeated)} "
            f"fm={getattr(state, 'fight_mode', None)} "
            f"loott={self.bb.notes.get('loot_phase_elapsed', 0.0):.1f} "
            f"cov={self.bb.notes.get('loot_cur', -1)}/{self.bb.notes.get('loot_best', -1)} "
            f"bopt={self.bb.notes.get('loot_bopt')} "
            f"wp={getattr(self.route, 'index', -1) if self.route else -1} "
            f"lure={self.bb.notes.get('lure_state', '-')} "
            f"pks={self.bb.notes.get('pokestop_n', 0)} "
            f"skord={','.join(str(k) for k in (getattr(state, 'skill_order', []) or [])[:5])} "
            f"skill={self.bb.notes.get('skill_sent', '-')} ed={ed} edx={edx} pkm={pkm} pkhp={pkhp} "
            f"rv={rp}/{int(rcd)}/{rve} rpnd={int(rpnd)} stun={int(stun)} "
            f"nav={getattr(state, 'nav_result', '')} "
            f"-> {flag}"
        )
