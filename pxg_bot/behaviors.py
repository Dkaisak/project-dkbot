from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .models import Creature, CreatureKind, GameState, Vec3
from .pathfinding import Route


@dataclass
class Blackboard:
    route: Optional[Route] = None
    target_cid: Optional[int] = None
    paused: bool = False
    stop_reason: str = ""
    human_seen: bool = False
    action_times: dict = field(default_factory=dict)
    notes: dict = field(default_factory=dict)
    counters: dict = field(default_factory=dict)
    humanizer: object = None
    ignore: object = None
    loot_items: object = None

    def ready(self, name: str, cooldown: float) -> bool:
        if self.humanizer is not None:
            cooldown = self.humanizer.jitter_up(cooldown, "cooldown_jitter_pct")
        return time.time() - self.action_times.get(name, 0.0) >= cooldown

    def mark(self, name: str) -> None:
        self.action_times[name] = time.time()

    def count(self, name: str, n: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + int(n)

    def update_corpses(self, state: GameState) -> None:
        # deteccion de cuerpos desacoplada de la prioridad de los behaviors: el
        # agente reporta cada cuerpo derrotado durante un solo tick, asi que hay
        # que cosecharlo siempre, aunque en ese tick actue revive/loot/etc.
        # teleport/respawn: si el player salta muchos tiles de golpe (o cambia de
        # piso), los cuerpos pendientes quedan inalcanzables -> se descartan para
        # no perseguir cuerpos al otro lado del mapa.
        p = state.player.pos
        prev = self.notes.get("player_last_pos")
        if prev is not None and p.distance(Vec3(prev[0], prev[1], prev[2])) > 12:
            self.notes["corpses_pending"] = []
            self.notes.pop("collect_rec", None)
        self.notes["player_last_pos"] = (p.x, p.y, p.z)
        seen = self.notes.setdefault("defeated_seen", set())
        looted = self.notes.setdefault("looted_ids", set())
        pending = self.notes.setdefault("corpses_pending", [])
        shiny_pos = self.notes.setdefault("shiny_pos", {})
        shiny_ids = self.notes.setdefault("shiny_ids", set())
        for c in state.creatures:
            if getattr(c, "shiny", False) and c.kind == CreatureKind.MONSTER:
                shiny_pos[(c.name, c.pos.x, c.pos.y)] = c.pos.z
                uid = str(getattr(c, "uid", "") or "")
                if uid:
                    shiny_ids.add(uid)
                self.notes["shiny_last"] = {"name": c.name, "id": uid,
                                            "x": c.pos.x, "y": c.pos.y, "z": c.pos.z}
        for d in state.defeated:
            did = str(d.get("id", ""))
            if not did or did in seen:
                continue
            seen.add(did)
            if did in looted:
                continue
            dname = d.get("name", "?")
            dx, dy = int(d.get("x", 0)), int(d.get("y", 0))
            items = d.get("items") or []
            # shiny por id (robusto) o por nombre+posicion (respaldo)
            was_shiny = (did in shiny_ids) or any(
                name == dname and abs(x - dx) <= 1 and abs(y - dy) <= 1
                for (name, x, y) in shiny_pos
            )
            pending.append({
                "id": did, "name": dname, "x": dx, "y": dy, "z": int(d.get("z", 0)),
                "items": items,
            })
            self.count("kill")
            # meta de cada cuerpo: se captura (ball) tras lotear si es shiny o catch_all
            self.notes.setdefault("corpse_meta", {})[did] = {
                "name": dname, "x": dx, "y": dy, "z": int(d.get("z", 0)),
                "shiny": was_shiny, "items": items,
            }


def nearest(state: GameState, predicate: Callable[[Creature], bool], max_range: Optional[int] = None):
    best = None
    best_d = 1 << 30
    for c in state.creatures:
        if not predicate(c):
            continue
        d = state.player.pos.distance(c.pos)
        if max_range is not None and d > max_range:
            continue
        if d < best_d:
            best_d = d
            best = c
    return best


def is_enemy(c: Creature) -> bool:
    return c.attackable and not c.is_player


def enemies_on_screen(state: GameState) -> bool:
    """True si hay algun enemigo atacable dentro de la camara (pantalla)."""
    vis = state.visible or {}
    hw = int(vis.get("w", 21)) // 2
    hh = int(vis.get("h", 11)) // 2
    px, py = state.player.pos.x, state.player.pos.y
    for c in state.creatures:
        if is_enemy(c) and abs(c.pos.x - px) <= hw and abs(c.pos.y - py) <= hh:
            return True
    return False


def summon_near_tile(state: GameState, otmm, radius: int = 4, enemies=None):
    """Devuelve el tile valido para ordenar el ownsummon: 8 vecinos libres
    (caminables en el otmm), no ocupado por criaturas y a >= 2 tiles del player
    (nunca encima ni a 1). None si no hay ninguno.

    Con `enemies` (lista de `Vec3`), ademas de acotar por radio se sesga el
    destino hacia el lado donde esta la masa de enemigos (el tile valido mas
    cercano a su centroide); sin enemigos, criterio clasico (el mas cercano al
    player)."""
    if otmm is None or not getattr(otmm, "ready", False):
        return None
    p = state.player.pos
    occupied = {(c.pos.x, c.pos.y) for c in state.creatures
                if c.pos.z == p.z and not c.is_self}

    def free(x: int, y: int) -> bool:
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                if not otmm.pathable(x + dx, y + dy, p.z):
                    return False
        return True

    # centroide de los enemigos: marca el "lado" hacia el que atraer al summon
    cen = None
    if enemies:
        cen = (sum(e.x for e in enemies) / len(enemies),
               sum(e.y for e in enemies) / len(enemies))

    # nunca a 1 tile del personaje (ni encima): minimo Chebyshev 2
    r_min = 2
    # Se busca por ANILLOS, del mas cercano al personaje hacia fuera: en cuanto
    # hay casilla valida en un anillo, se usa ESA (lo mas cerca posible del
    # player). Antes se cogia la casilla mas cercana al centroide enemigo aunque
    # estuviera en un anillo mayor -> el summon quedaba lejos y los cuerpos
    # caian lejos (dificiles de lootear). Dentro del anillo elegido, el sesgo a
    # enemigos solo desempata hacia el lado con mas enemigos.
    for r in range(r_min, max(r_min, radius) + 1):
        ring = []
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                if max(abs(dx), abs(dy)) != r:
                    continue
                x, y = p.x + dx, p.y + dy
                # NUNCA el tile del propio personaje (r>=r_min ya lo excluye;
                # explicito por seguridad para que `order` no apile el summon)
                if x == p.x and y == p.y:
                    continue
                if (x, y) in occupied:
                    continue
                if not otmm.pathable(x, y, p.z):
                    continue
                if not free(x, y):
                    continue
                ring.append((x, y))
        if not ring:
            continue
        if cen is None:
            x, y = ring[0]
            return Vec3(x, y, p.z)
        x, y = min(ring, key=lambda t: (t[0] - cen[0]) ** 2 + (t[1] - cen[1]) ** 2)
        return Vec3(x, y, p.z)
    return None


class Behavior:
    name = "behavior"
    priority = 0

    def __init__(self, cfg: dict, settings: dict):
        self.cfg = cfg or {}
        self.settings = settings or {}

    def evaluate(self, state: GameState, bb: Blackboard) -> bool:
        return False

    def act(self, state: GameState, bb: Blackboard, inp) -> None:
        raise NotImplementedError


class CrisisBehavior(Behavior):
    name = "crisis"
    priority = 100

    def evaluate(self, state: GameState, bb: Blackboard) -> bool:
        s = self.cfg
        if not s.get("enabled", True):
            # el toggle "crisis" desactiva absolutamente todo (desconexion,
            # jugador/GM, HP critica, huida y logout)
            return False
        if not state.connected:
            bb.stop_reason = "cliente desconectado"
            return True
        if s.get("pause_on_player", True):
            for c in state.creatures:
                if c.is_self:
                    continue
                if c.is_player or c.kind == CreatureKind.PLAYER:
                    if state.player.pos.distance(c.pos) <= int(s.get("player_range", 7)):
                        bb.human_seen = True
                        bb.stop_reason = "jugador/GM detectado"
                        return True
        if state.player.hp_pct <= int(s.get("panic_hp_pct", 10)):
            bb.stop_reason = "HP crítica"
            return True
        return False

    def act(self, state: GameState, bb: Blackboard, inp) -> None:
        s = self.cfg
        if not state.connected:
            if bb.ready("reconnect", float(s.get("reconnect_cooldown", 5))):
                inp.press(s.get("reconnect_key", "F1"))
                bb.mark("reconnect")
            return
        if bb.human_seen:
            bb.paused = True
            if s.get("logout_on_player"):
                inp.press(s.get("logout_key", "F12"))
            return
        threat = nearest(state, is_enemy, max_range=int(s.get("flee_detect_range", 5)))
        if threat is not None and bb.ready("flee", 0.3):
            away = Vec3(
                state.player.pos.x * 2 - threat.pos.x,
                state.player.pos.y * 2 - threat.pos.y,
                state.player.pos.z,
            )
            direction = state.player.pos.direction_to(away)
            if direction >= 0:
                inp.move(direction)
            bb.mark("flee")


class HealingBehavior(Behavior):
    name = "heal"
    priority = 90

    def evaluate(self, state: GameState, bb: Blackboard) -> bool:
        if bb.paused or not self.cfg.get("enabled", True):
            return False
        s = self.cfg
        cd = float(s.get("cooldown", 1.0))
        if state.player.hp_pct <= int(s.get("player_hp_pct", 60)) and bb.ready("player_heal", cd):
            return True
        pk = state.active_pokemon()
        if pk and pk.hp_pct <= int(s.get("pokemon_hp_pct", 40)) and bb.ready("pokemon_heal", cd):
            return True
        return False

    def act(self, state: GameState, bb: Blackboard, inp) -> None:
        s = self.cfg
        if state.player.hp_pct <= int(s.get("player_hp_pct", 60)):
            inp.press(s.get("player_heal_key", "F2"))
            bb.mark("player_heal")
            return
        pk = state.active_pokemon()
        if pk and pk.hp_pct <= int(s.get("pokemon_hp_pct", 40)):
            inp.press(s.get("pokemon_heal_key", "F3"))
            bb.mark("pokemon_heal")


class CaptureBehavior(Behavior):
    name = "capture"
    priority = 85

    def _pending(self, bb: Blackboard) -> list:
        return bb.notes.setdefault("capture_pending", [])

    def _name_set(self, key: str) -> set:
        raw = self.cfg.get(key)
        if isinstance(raw, str):
            raw = raw.replace(";", ",").split(",")
        if not isinstance(raw, (list, tuple)):
            return set()
        return {str(n).strip().lower() for n in raw if str(n).strip()}

    def _promote(self, bb: Blackboard) -> None:
        # La captura es una FASE: corre cuando la fase de loot ya concluyo (loot
        # tiene mayor prioridad y bloquea mientras le queden cuerpos). NO depende
        # de que cada cuerpo este looteado: si el loot esta desactivado, o lo
        # abandono, la captura procesa igualmente los cuerpos validos.
        meta = bb.notes.get("corpse_meta")
        if not meta:
            return
        pending = self._pending(bb)
        for cid in list(meta.keys()):
            m = meta.pop(cid)
            if self._valid(m):
                pending.append({"id": cid, "n": 0, **m})
                bb.count("ball")   # 1 por cuerpo (no por cada throw)

    def _valid(self, t: dict) -> bool:
        # filtro por nombre: `exclude` gana; si hay `names`, solo esos.
        name = str(t.get("name", "")).strip().lower()
        if name and name in self._name_set("exclude"):
            return False
        names = self._name_set("names")
        if names:
            return name in names
        if t.get("shiny"):
            return True
        return bool(self.cfg.get("enabled", True)) and bool(self.cfg.get("catch_all", False))

    def evaluate(self, state: GameState, bb: Blackboard) -> bool:
        if bb.paused:
            return False
        # una secuencia de revive en curso es atomica: captura cede
        if bb.notes.get("revive_rec") or bb.notes.get("revive_urgent") or bb.notes.get("revive_fast"):
            return False
        self._promote(bb)
        pending = self._pending(bb)
        pending[:] = [t for t in pending if self._valid(t)]
        if not pending:
            return False
        # primero pelear: no capturar mientras queden enemigos en pantalla
        # (lanzar balls a media pelea deja al Pokemon sin skills -> muertes)
        if enemies_on_screen(state):
            return False
        return True

    def act(self, state: GameState, bb: Blackboard, inp) -> None:
        pending = self._pending(bb)
        if not pending:
            return
        # capturado (mensaje del servidor): el cuerpo se va. El recuento lo hace
        # el bot con el contador del agente (`Bot._count_captures`), para contar
        # tambien capturas que no vengan de la cola (p. ej. manuales o no-shiny).
        captured = getattr(state, "captured", False)
        # toggle `stop_on_capture`: al confirmarse la captura, dejar de lanzar
        # balls a ese cuerpo. Usa el latch del contador del bot (`capture_stop_at`)
        # porque el pulso de un tick se pierde si captura cede ante enemigos.
        if self.cfg.get("stop_on_capture", False):
            stop_at = bb.notes.get("capture_stop_at", 0.0)
            if stop_at and (time.time() - stop_at) <= float(self.cfg.get("stop_on_capture_secs", 5.0)):
                bb.notes.pop("capture_stop_at", None)
                captured = True
        if captured:
            pending.pop(0)
            return
        t = pending[0]
        pos = Vec3(int(t["x"]), int(t["y"]), int(t["z"]))
        # acercarse al cuerpo antes de lanzar
        if state.player.pos.distance(pos) > int(self.cfg.get("range", 1)):
            if bb.ready("cap_goto", 0.35):
                inp.walk_to(pos)
                bb.mark("cap_goto")
            return
        # lanzar la ball: Lua aplica el item al Thing del tile (sin mover el cursor)
        if bb.ready("ball", float(self.cfg.get("interval", 1.0))):
            inp.ball(int(self.cfg.get("ball_item", 2652)), pos)
            bb.mark("ball")
            t["n"] = int(t.get("n", 0)) + 1
            if t["n"] >= int(self.cfg.get("max_throws", 5)):
                pending.pop(0)


class CombatBehavior(Behavior):
    name = "combat"
    priority = 70

    def __init__(self, cfg: dict, settings: dict, world=None):
        super().__init__(cfg, settings)
        self.world = world

    def evaluate(self, state: GameState, bb: Blackboard) -> bool:
        if bb.paused or not self.cfg.get("enabled", True):
            return False
        if bb.notes.get("spawn_backoff"):
            return True
        # el buff se dispara con >= N enemigos en pantalla, aunque no haya lure
        # (si no, con pocos enemigos combate no actua y el buff no sale)
        if self._buff_wanted(state, bb):
            return True
        if self._spawn_blocked(state, bb):
            tgt = bb.notes.get("last_target")
            if tgt:
                self._begin_spawn_backoff(bb, tgt)
                return bool(bb.notes.get("spawn_backoff"))
        # lure: caminar hasta juntar X visibles -> hold (parar+agrupar) -> fight
        # (pokestop cuando el summon llego y todos a rango) -> resume.
        if self.cfg.get("lure_aoe", False) and self._has_aoe(state):
            lstate = bb.notes.get("lure_state")
            vis = self._engaging_enemies(state)
            xmin = int(self.cfg.get("lure_visible_min", 5))
            # panico (vida baja): atacar ya, en cualquier fase
            if self._panic(state) and len(vis) >= 1:
                bb.notes["lure_state"] = "fight"
                return True
            if lstate == "fight":
                if not any(is_enemy(c) for c in state.creatures):
                    bb.notes["lure_state"] = "resume"
                return True
            if lstate == "hold":
                wait = bool(bb.notes.get("lure_hold_wait"))
                # No se abandona mientras haya enemigos en pantalla: primero se
                # REMATAN (el que quedo vivo o el nuevo que llego). Solo se vuelve
                # a la ruta cuando no queda ninguno.
                if len(vis) == 0:
                    bb.notes["lure_state"] = "resume"
                    return True
                # pokestop solo cuando el summon llego al order Y todos a rango
                if self._summon_arrived(state, bb) and self._attack_gate_ok(state):
                    bb.notes["lure_state"] = "fight"
                    bb.notes["lure_pokestop"] = True
                    return True
                # el hold normal abandona por tiempo; el del tope sigue esperando
                # (a que los de pantalla entren en rango: el panic ya salta arriba)
                if not wait and time.time() - bb.notes.get("lure_hold_start", time.time()) > float(self.cfg.get("hold_timeout", 15.0)):
                    bb.notes["lure_state"] = "resume"
                return True
            if lstate == "resume":
                return True
            # idle: caminar hasta juntar X visibles -> hold. Con tope: al aparecer
            # al menos 1 enemigo empieza a contar `lure_gather_timeout`; si no se
            # junta X a tiempo, tambien pasa a hold (con lo que haya). El gate
            # estricto sigue decidiendo cuando castear.
            if len(vis) >= xmin:
                return self._enter_hold(bb, xmin)
            if len(vis) >= 1:
                to = float(self.cfg.get("lure_gather_timeout", 10.0))
                if to > 0:  # <= 0 = sin tope (camina hasta juntar X)
                    if not bb.notes.get("lure_gather_start"):
                        bb.notes["lure_gather_start"] = time.time()
                    elif time.time() - bb.notes["lure_gather_start"] >= to:
                        # tope vencido sin juntar X: hold "con lo que haya" y, si
                        # `lure_tope_wait`, quedarse esperando a que los de pantalla
                        # entren en rango de ataque (sin abandonar por tiempo).
                        return self._enter_hold(bb, len(vis),
                                                wait=bool(self.cfg.get("lure_tope_wait", True)))
            else:
                bb.notes.pop("lure_gather_start", None)
            return False  # seguir la ruta (lureando)
        blacklist = bb.notes.setdefault("target_blacklist", {})
        now = time.time()
        for key in [k for k, exp in blacklist.items() if exp <= now]:
            blacklist.pop(key, None)
        rng = int(self.cfg.get("detect_range", 8))
        candidates = [c for c in state.creatures if is_enemy(c)
                      and state.player.pos.distance(c.pos) <= rng
                      and (c.name, c.pos.x, c.pos.y) not in blacklist]
        if not candidates:
            bb.notes.pop("tname", None)
            return False
        prev = bb.notes.get("tname")
        cur = prev
        names = {c.name for c in candidates}
        fresh = cur in names and now - bb.notes.get("tswitch", 0.0) < float(self.cfg.get("target_switch_secs", 4.0))
        if not fresh:
            if bb.humanizer is not None and bb.humanizer.cfg.get("target_variance", True):
                pick = bb.humanizer.pick_weighted(
                    candidates, lambda c: state.player.pos.distance(c.pos), top=3)
            else:
                pick = min(candidates, key=lambda c: state.player.pos.distance(c.pos))
            bb.notes["tname"] = pick.name
            bb.notes["tswitch"] = now
        if bb.notes.get("tname") != prev:
            bb.notes["engage_since"] = now
            bb.notes["engage_pos"] = (state.player.pos.x, state.player.pos.y)
        target = next((c for c in candidates if c.name == bb.notes["tname"]), candidates[0])
        bb.notes["last_target"] = (target.name, target.pos.x, target.pos.y, target.pos.z, target.hp_pct)
        return True

    # ---- spawn bloqueado: alejarse >= clear_dist y volver ----
    def _spawn_blocked(self, state: GameState, bb: Blackboard) -> bool:
        if not getattr(state, "spawn_blocked", False):
            return False
        now = time.time()
        # el mensaje responde a un intento de ataque reciente
        if now - bb.notes.get("attack_attempt", 0.0) > 3.0:
            return False
        # no re-disparar durante el cooldown tras un backoff
        if now - bb.notes.get("spawn_backoff_end", 0.0) < float(self.cfg.get("spawn_cooldown_secs", 8.0)):
            return False
        return True

    def _begin_spawn_backoff(self, bb: Blackboard, tgt: tuple) -> None:
        tx, ty, tz = int(tgt[1]), int(tgt[2]), int(tgt[3])
        retries = int(bb.notes.get("spawn_retries_done", 0))
        now = time.time()
        bb.notes["dmg_key"] = None
        if retries >= int(self.cfg.get("spawn_retries", 2)):
            bb.notes.setdefault("target_blacklist", {})[
                (tgt[0], tx, ty)
            ] = now + float(self.cfg.get("spawn_blacklist_secs", 20.0))
            bb.notes["spawn_retries_done"] = 0
            bb.notes["spawn_backoff_end"] = now
            bb.notes.pop("tname", None)
            bb.notes.pop("last_target", None)
            return
        bb.notes["spawn_retries_done"] = retries + 1
        bb.notes["spawn_backoff"] = {
            "tx": tx, "ty": ty, "tz": tz, "phase": "away",
            "until": now + float(self.cfg.get("spawn_away_timeout", 30.0)),
            "sent": 0.0, "key": None,
        }
        bb.notes.pop("tname", None)

    def _away_tile(self, bb: Blackboard, pos: Vec3, tx: int, ty: int, z: int, clear: int):
        dx = 1 if pos.x >= tx else -1
        dy = 1 if pos.y >= ty else -1
        if pos.x == tx:
            dx = bb.humanizer.choice([-1, 1]) if bb.humanizer else 1
        if pos.y == ty:
            dy = bb.humanizer.choice([-1, 1]) if bb.humanizer else 1
        dirs = [(dx, dy), (dx, 0), (0, dy), (dx, -dy), (-dx, dy), (-dx, -dy), (-dx, 0), (0, -dy)]
        otmm = getattr(self.world, "otmm", None) if self.world is not None else None
        for ox, oy in dirs:
            gx, gy = tx + ox * clear, ty + oy * clear
            if otmm is not None and getattr(otmm, "ready", False):
                if otmm.pathable(gx, gy, z):
                    return (gx, gy, z)
            else:
                return (gx, gy, z)
        return (pos.x + dx * clear, pos.y + dy * clear, z)

    def _goto(self, state: GameState, bb: Blackboard, inp, goal, now: float) -> None:
        gx, gy, gz = goal
        key = (gx, gy)
        if bb.notes.get("backoff_key") != key or (
            not state.is_walking and now - bb.notes.get("backoff_sent", 0.0) > float(self.cfg.get("resend_secs", 0.5))
        ):
            inp.walk_to(Vec3(gx, gy, gz))
            bb.notes["backoff_key"] = key
            bb.notes["backoff_sent"] = now

    def _run_spawn_backoff(self, state: GameState, bb: Blackboard, inp) -> None:
        bo = bb.notes["spawn_backoff"]
        now = time.time()
        tx, ty = bo["tx"], bo["ty"]
        dist = max(abs(state.player.pos.x - tx), abs(state.player.pos.y - ty))
        clear = int(self.cfg.get("spawn_clear_dist", 20))
        if bo["phase"] == "away":
            if dist >= clear or now > bo["until"]:
                bo["phase"] = "wait"
                bo["until"] = now + float(self.cfg.get("spawn_wait_secs", 1.0))
                return
            goal = self._away_tile(bb, state.player.pos, tx, ty, bo["tz"], clear)
            self._goto(state, bb, inp, goal, now)
        elif bo["phase"] == "wait":
            if now >= bo["until"]:
                bo["phase"] = "return"
                bo["until"] = now + float(self.cfg.get("spawn_return_timeout", 45.0))
                bb.notes["backoff_key"] = None
            return
        else:  # return
            engage = int(self.cfg.get("attack_range", 3)) + 1
            if dist <= engage or now > bo["until"]:
                bb.notes.pop("spawn_backoff", None)
                bb.notes["spawn_backoff_end"] = now
                bb.notes.pop("tname", None)
                return
            self._goto(state, bb, inp, (tx, ty, bo["tz"]), now)

    def _has_aoe(self, state: GameState) -> bool:
        return any(m.get("aoe") for m in (state.moves or []))

    def _visible_enemies(self, state: GameState) -> list:
        """Enemigos dentro del area visible de la pantalla (rectangulo de la camara)."""
        vis = state.visible or {}
        hw = int(vis.get("w", 21)) // 2
        hh = int(vis.get("h", 11)) // 2
        px, py = state.player.pos.x, state.player.pos.y
        return [c for c in state.creatures
                if is_enemy(c) and abs(c.pos.x - px) <= hw and abs(c.pos.y - py) <= hh]

    def _ref_pos(self, state: GameState) -> Optional[Vec3]:
        # referencia para medir distancias: SIEMPRE el pokemon propio (ownsummon).
        # Nunca el jugador. Si no se conoce el summon, se devuelve None y no se
        # ataca (mejor no pelear que pelear con la distancia equivocada).
        pp = getattr(state, "pokemon_pos", None)
        if pp:
            return Vec3(int(pp[0]), int(pp[1]), int(pp[2]))
        return None

    def _engaging_enemies(self, state: GameState) -> list:
        """Enemigos que cuentan para el enfrentamiento. Por defecto es **toda la
        pantalla visible** (`engage_radius <= 0`); si `engage_radius > 0`, ademas
        se limitan a ese radio alrededor del summon. Si no se conoce el summon,
        se devuelven los visibles."""
        visible = self._visible_enemies(state)
        ref = self._ref_pos(state)
        if ref is None:
            return visible
        r = int(self.cfg.get("engage_radius", 0))
        if r <= 0:
            return visible
        return [c for c in visible if ref.distance(c.pos) <= r]

    def _enough_in_range(self, state: GameState, rng: int) -> bool:
        """True si al menos `cast_min_in_range` enemigos que cuentan estan a
        <= rng del summon (o todos los que cuentan, si hay menos que el minimo).
        Sustituye al criterio estricto de 'todos cerca'."""
        engaging = self._engaging_enemies(state)
        if not engaging:
            return False
        ref = self._ref_pos(state)
        if ref is None:
            return False
        need = min(int(self.cfg.get("cast_min_in_range", 2)), len(engaging))
        in_attack = sum(1 for c in engaging if ref.distance(c.pos) <= rng)
        return in_attack >= need

    def _attack_gate_ok(self, state: GameState) -> bool:
        """Gate de ataque comun (lure, dirigido y buff): en panic siempre se
        puede; si require_all_close, TODOS los de pantalla a rango; si no, N."""
        if self._panic(state):
            return True
        arange = int(self.cfg.get("attack_range", 3))
        if self.cfg.get("require_all_close", False):
            return self._all_enemies_close(state, arange)
        return self._enough_in_range(state, arange)

    def _all_enemies_close(self, state: GameState, rng: int) -> bool:
        """True solo si TODOS los enemigos visibles estan a <= rng del pokemon propio."""
        vis = self._visible_enemies(state)
        if not vis:
            return False
        ref = self._ref_pos(state)
        if ref is None:
            return False
        return all(ref.distance(c.pos) <= rng for c in vis)

    def _panic(self, state: GameState) -> bool:
        # vida baja: lanzar todo sin esperar (el origen del HP es configurable)
        hp = self._panic_hp(state)
        return hp is not None and hp <= int(self.cfg.get("panic_hp", 25))

    def _panic_hp(self, state: GameState):
        """HP (%) que dispara el panico segun `panic_hp_source`:
        - summon: HP del pokemon propio (ownsummon) [por defecto]
        - active: HP del Pokemon activo de la party
        - min:    el menor de ambos (mas conservador)
        """
        summon = getattr(state, "pokemon_hp", None)
        active = None
        act = state.active_pokemon() if hasattr(state, "active_pokemon") else None
        if act is not None:
            active = getattr(act, "hp_pct", None)
        src = str(self.cfg.get("panic_hp_source", "summon")).lower()
        if src == "active":
            return active if active is not None else summon
        if src == "min":
            vals = [v for v in (summon, active) if v is not None]
            return min(vals) if vals else None
        return summon if summon is not None else active

    def _middle_click_empty(self, state: GameState, bb: Blackboard, inp) -> None:
        """Clic central en un tile vacio: indica al pokemon que puede moverse otra vez."""
        px, py, pz = state.player.pos.x, state.player.pos.y, state.player.pos.z
        occupied = {(c.pos.x, c.pos.y) for c in state.creatures}
        otmm = getattr(self.world, "otmm", None) if self.world is not None else None
        for dx, dy in [(0, 0), (1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (1, -1), (-1, 1)]:
            gx, gy = px + dx, py + dy
            if (gx, gy) in occupied:
                continue
            if otmm is not None and getattr(otmm, "ready", False):
                if not otmm.pathable(gx, gy, pz):
                    continue
            inp.middle_click(Vec3(gx, gy, pz))
            return
        inp.middle_click(Vec3(px, py, pz))

    def _note_skill(self, bb: Blackboard, state: GameState, key) -> None:
        # registra la skill enviada; si es AoE, el momento (para el revive) y si
        # tiene stun, la ventana en la que el enemigo queda aturdido.
        bb.notes["skill_sent"] = key
        bb.count("skill")
        # tiempo de combo: la PRIMERA skill del ciclo arranca el reloj; el revive
        # esperara a que pasen `combo_time` s para no cortar su animacion.
        if not bb.notes.get("combo_start"):
            bb.notes["combo_start"] = time.time()
        for m in (state.moves or []):
            if str(m.get("key")) == str(key):
                if m.get("aoe"):
                    bb.notes["last_aoe_at"] = time.time()
                if "stun" in str(m.get("effect", "")).lower():
                    bb.notes["last_stun_at"] = time.time()
                break

    def _buff_candidate(self, state: GameState):
        threshold = float(self.cfg.get("ready_pct", 100))
        for m in (state.moves or []):
            parts = [p.strip() for p in str(m.get("effect", "")).lower().split("/")]
            if "buff" in parts and isinstance(m.get("pct"), (int, float)) and m["pct"] >= threshold:
                return m.get("key")
        return None

    def _buff_wanted(self, state: GameState, bb: Blackboard) -> bool:
        # hay que lanzar el buff? (>= N enemigos EN PANTALLA, no por lure)
        if not self.cfg.get("buff_on_screen", True):
            return False
        nmin = int(self.cfg.get("buff_visible_min", 3))
        if len(self._visible_enemies(state)) < nmin:
            bb.notes.pop("buff_done", None)   # se rearma al bajar del minimo
            return False
        if bb.notes.get("buff_done"):
            return False
        return self._buff_candidate(state) is not None

    def _buff_gate_ok(self, state: GameState) -> bool:
        """Gate propio del buff segun `buff_gate`:
        - `screen` (def): no exige distancia; basta con `buff_visible_min` en
          pantalla (ya garantizado por `_buff_wanted`).
        - `range`: TODOS los de pantalla a `attack_range` del summon.
        - `combat`: usa el gate de combate (`require_all_close` /
          `cast_min_in_range`).
        El panico (vida baja) siempre permite."""
        if self._panic(state):
            return True
        gate = str(self.cfg.get("buff_gate", "screen")).lower()
        if gate == "range":
            return self._all_enemies_close(state, int(self.cfg.get("attack_range", 3)))
        if gate == "combat":
            return self._attack_gate_ok(state)
        return True

    def _maybe_buff(self, state: GameState, bb: Blackboard, inp) -> bool:
        if not self._buff_wanted(state, bb):
            return False
        key = self._buff_candidate(state)
        if key is None:
            return False
        # gate propio del buff (por defecto: en pantalla, sin exigir distancia)
        if not self._buff_gate_ok(state):
            return False
        if bb.ready("attack", float(self.cfg.get("cooldown", 0.3))):
            inp.press(key)
            self._note_skill(bb, state, key)
            bb.notes["buff_done"] = True
            bb.mark("attack")
            return True
        return False

    def _lure_summon(self, state: GameState, bb: Blackboard, inp) -> None:
        # antes de pelear: ordenar el ownsummon cerca del player (con los 8
        # vecinos libres) para que los cuerpos caigan cerca y el loot no quede lejos.
        if not self.cfg.get("lure_summon", True):
            return
        if bb.notes.get("lure_summon_done"):
            return
        otmm = getattr(self.world, "otmm", None) if self.world is not None else None
        enemies = None
        if self.cfg.get("lure_summon_toward_enemies", True):
            # sesgar el destino hacia la masa de enemigos (el lado con mas) para
            # que el summon se coloque entre el player y el grupo que se lurea
            enemies = [c.pos for c in self._engaging_enemies(state)]
        t = summon_near_tile(state, otmm, int(self.cfg.get("lure_summon_radius", 3)), enemies)
        if t is not None:
            inp.order(t)
            # el pokestop esperara a que el summon llegue a este tile
            bb.notes["lure_order_target"] = (t.x, t.y, t.z)
            bb.notes["lure_order_at"] = time.time()
        bb.notes["lure_summon_done"] = True

    def _summon_arrived(self, state: GameState, bb: Blackboard) -> bool:
        """True si el ownsummon esta en (o muy cerca de) el tile al que se le
        ordeno ir. Para no bloquear el lure, basta con estar a `summon_arrive_tolerance`
        tiles, o que pase `summon_grace_secs` desde el order (esta lo mas cerca
        que puede)."""
        tgt = bb.notes.get("lure_order_target")
        if not tgt:
            return True   # sin order (p. ej. lure_summon off): no bloquea
        pp = getattr(state, "pokemon_pos", None)
        if pp:
            tol = int(self.cfg.get("summon_arrive_tolerance", 1))
            d = max(abs(int(pp[0]) - int(tgt[0])),
                    abs(int(pp[1]) - int(tgt[1])),
                    abs(int(pp[2]) - int(tgt[2])))
            if d <= tol:
                return True
        at = bb.notes.get("lure_order_at", 0.0)
        return at > 0 and (time.time() - at) >= float(self.cfg.get("summon_grace_secs", 3.0))

    def _pokestop(self, state: GameState, bb: Blackboard, inp) -> None:
        method = str(self.cfg.get("pokestop_method", "func"))
        if method == "hotkey":
            inp.hotkey(str(self.cfg.get("pokestop_key", "R")))
        elif method == "talk":
            inp.pokestop("talk", str(self.cfg.get("pokestop_talk", "!pokestop")))
        else:
            inp.pokestop("func")
        bb.notes["pokestop_n"] = int(bb.notes.get("pokestop_n", 0)) + 1
        bb.count("pokestop")

    def _enter_hold(self, bb: Blackboard, min_count: Optional[int] = None,
                    wait: bool = False) -> bool:
        """Pasa el lure a `hold` (parar+agrupar) y limpia las notas de la fase.

        `min_count` es el minimo de enemigos que el hold exigira para seguir vivo:
        `lure_visible_min` si se junto X, o lo que hubiera si el hold viene del
        tope (`lure_gather_timeout`).

        `wait=True` (hold del tope): NO abandona por tiempo; se queda parado
        esperando a que los enemigos de pantalla entren en rango de ataque (o a
        que se vayan todos, o a que dispare el panico)."""
        bb.notes["lure_state"] = "hold"
        bb.notes["lure_hold_start"] = time.time()
        bb.notes["lure_hold_min"] = (int(min_count) if min_count
                                     else int(self.cfg.get("lure_visible_min", 5)))
        bb.notes["lure_hold_wait"] = bool(wait)
        bb.count("lure")
        for k in ("lure_summon_done", "lure_order_target", "lure_order_at",
                  "lure_stopped", "lure_pokestop", "lure_fight_lost",
                  "lure_resume_until", "lure_gather_start"):
            bb.notes.pop(k, None)
        return True

    def _lure_hold(self, state: GameState, bb: Blackboard, inp) -> None:
        # detener la navegacion de la ruta (una vez)
        if not bb.notes.get("lure_stopped"):
            inp.stop()
            bb.notes["lure_stopped"] = True
        # situar el ownsummon (una vez); el pokestop espera a que llegue
        self._lure_summon(state, bb, inp)

    def _lure_fight(self, state: GameState, bb: Blackboard, inp) -> None:
        if not self._attack_gate_ok(state):
            # gate caido (se separaron): si persiste, volver a hold a reagrupar
            now = time.time()
            lost = bb.notes.get("lure_fight_lost")
            if not lost:
                bb.notes["lure_fight_lost"] = now
            elif now - lost > float(self.cfg.get("fight_recover_secs", 1.5)):
                bb.notes["lure_state"] = "hold"
                bb.notes["lure_hold_start"] = now
                for k in ("lure_fight_lost", "lure_summon_done", "lure_order_target",
                          "lure_order_at", "lure_stopped", "lure_pokestop",
                          "lure_hold_wait"):
                    bb.notes.pop(k, None)
            return
        bb.notes.pop("lure_fight_lost", None)
        enemies = [c for c in state.creatures if is_enemy(c)]
        self._aoe_fight(state, bb, inp, enemies)

    def _aoe_fight(self, state: GameState, bb: Blackboard, inp, enemies) -> None:
        """Fase de pelea tras el lure: lanza AoE (fallback a skills de dano). Sin moverse."""
        s = self.cfg
        now = time.time()
        if not enemies:
            return
        threshold = float(s.get("ready_pct", 100))

        def is_ready(m):
            return isinstance(m.get("pct"), (int, float)) and m["pct"] >= threshold

        order = [str(k) for k in (getattr(state, "skill_order", []) or [])]
        prio = [str(p).lower() for p in (self.cfg.get("aoe_priority", []) or [])]

        def rank(m):
            k = str(m.get("key", ""))
            if k in order:
                return order.index(k)
            nm = str(m.get("name", "")).lower()
            for i, p in enumerate(prio):
                if p and p in nm:
                    return len(order) + i
            return len(order) + len(prio)

        panic = self._panic(state)
        panic_mode = str(s.get("panic_skills", "all")).lower() if panic else "all"
        use_single = bool(s.get("lure_use_single", True))
        lure_order = [str(k) for k in (getattr(state, "lure_order", []) or [])]
        by_key = {str(m.get("key", "")): m for m in state.moves}
        use_combo = bool(lure_order) and (not panic or panic_mode == "combo")
        if use_combo:
            # combo propio del lure (GUI): exactamente esas skills, en ese orden
            pool = [k for k in lure_order if k in by_key and is_ready(by_key[k])]
        else:
            # pool de skills listas ORDENADO POR EL COMBO del pokemon (no por la
            # barra): asi se respeta el orden configurado tambien con las de dano.
            ready_moves = []
            for m in state.moves:
                if not is_ready(m):
                    continue
                if panic and panic_mode == "everything":
                    ready_moves.append(m)   # en panico: TODAS (incl. buff/no-dano)
                    continue
                effect = str(m.get("effect", ""))
                if m.get("aoe"):
                    ready_moves.append(m)
                elif "damage" in effect and (use_single or panic):
                    ready_moves.append(m)
            ready_moves.sort(key=rank)
            pool = [m["key"] for m in ready_moves]
        if not pool:
            return
        # no re-lanzar la misma skill mientras el cliente aun no refleja su
        # cooldown: si no, el bot repite la misma (no-op) y el burst se frena
        # ~0.3-0.6 s por skill. Excluyendola, recorre las demas listas seguidas.
        guard = float(s.get("skill_repeat_guard", 0.3))
        pressed = bb.notes.setdefault("pressed_skills", {})
        for k in [k for k, t0 in pressed.items() if now - t0 > guard]:
            pressed.pop(k, None)
        pool = [k for k in pool if k not in pressed]
        if not pool:
            return
        # gate de ataque (estricto por defecto: todos a rango; panic lo salta)
        if not self._attack_gate_ok(state):
            return
        close = self._engaging_enemies(state)
        if not close:
            return
        # las skills AoE no necesitan fijar objetivo: se lanzan directas
        if bb.ready("attack", float(s.get("cooldown", 0.3))):
            # en orden: primero la AoE prioritaria (Air Vortex), luego el resto
            inp.press(pool[0])
            self._note_skill(bb, state, pool[0])
            pressed[pool[0]] = now
            bb.mark("attack")

    def act(self, state: GameState, bb: Blackboard, inp) -> None:
        if bb.notes.get("spawn_backoff"):
            self._run_spawn_backoff(state, bb, inp)
            return
        self._manage_fight_mode(bb, inp)
        # buff cuando hay >= N enemigos en pantalla (no depende del lure)
        if self._maybe_buff(state, bb, inp):
            return
        lstate = bb.notes.get("lure_state")
        if lstate == "hold":
            self._lure_hold(state, bb, inp)
            return
        if lstate == "fight":
            if bb.notes.pop("lure_pokestop", False):
                self._pokestop(state, bb, inp)
            # esperar a que el servidor aplique el modo ofensivo antes del burst
            if self._fight_mode_waiting(bb):
                return
            self._lure_fight(state, bb, inp)
            return
        if lstate == "resume":
            now = time.time()
            if not bb.notes.get("lure_resume_until"):
                self._middle_click_empty(state, bb, inp)
                bb.notes["lure_resume_until"] = now + float(self.cfg.get("resume_pause_secs", 1.0))
                return
            if now < bb.notes["lure_resume_until"]:
                return
            for k in ("lure_state", "lure_resume_until", "lure_summon_done",
                      "lure_order_target", "lure_order_at", "lure_stopped",
                      "lure_pokestop", "lure_fight_lost", "lure_hold_start",
                      "lure_hold_min", "lure_hold_wait"):
                bb.notes.pop(k, None)
            return
        s = self.cfg
        name = bb.notes.get("tname")
        if not name:
            return
        candidates = [c for c in state.creatures if c.name == name and is_enemy(c)]
        if not candidates:
            bb.notes.pop("tname", None)
            return
        target = min(candidates, key=lambda c: state.player.pos.distance(c.pos))
        ref = self._ref_pos(state)
        if ref is None:
            return
        if ref.distance(target.pos) > int(s.get("attack_range", 3)):
            # acercarse caminando (no es fijar objetivo)
            direction = state.player.pos.direction_to(target.pos)
            if direction >= 0 and not state.is_walking and bb.ready("approach", float(s.get("approach_cooldown", 0.15))):
                inp.move(direction)
                bb.mark("approach")
            return

        now = time.time()
        # NO se fija objetivo (no se envia attackat): se ataca directo
        hp = getattr(target, "hp_pct", None)
        key = (name, target.pos.x, target.pos.y)
        if bb.notes.get("dmg_key") != key:
            bb.notes["dmg_key"] = key
            bb.notes["dmg_hp"] = hp if hp is not None else 100
            bb.notes["dmg_since"] = now
        elif hp is not None and hp < bb.notes.get("dmg_hp", 100):
            bb.notes["dmg_hp"] = hp
            bb.notes["dmg_since"] = now
            bb.notes["spawn_retries_done"] = 0
        elif now - bb.notes.get("dmg_since", now) > float(s.get("no_damage_secs", 3.0)):
            # no pierde vida estando a rango: probablemente inatacable (NPC o
            # pokemon de NPC). Lo marcamos por su id para no volver a atacarlo.
            if bb.ignore is not None and getattr(target, "uid", ""):
                bb.ignore.add_id(target.uid)
                bb.notes.pop("tname", None)
                bb.notes.pop("last_target", None)
                bb.notes["dmg_key"] = None
                return
            self._begin_spawn_backoff(bb, (name, target.pos.x, target.pos.y, target.pos.z, hp))
            return

        # 3) atacar (respetando el orden de skills configurado en la GUI).
        # Gate de ataque (estricto por defecto: todos a rango; panic lo salta).
        if not self._attack_gate_ok(state):
            return
        threshold = float(s.get("ready_pct", 100))
        if bb.ready("attack", float(s.get("cooldown", 0.3))):
            if state.moves:
                ready = [m["key"] for m in state.moves
                         if isinstance(m.get("pct"), (int, float)) and m["pct"] >= threshold]
            else:
                ready = [str(k) for k in s.get("moves", ["1", "2", "3", "4"])]
            if ready:
                order = [str(k) for k in (getattr(state, "skill_order", []) or [])]
                if order:
                    def _rank(k):
                        return order.index(k) if k in order else len(order)
                    key = sorted(ready, key=_rank)[0]
                elif bb.humanizer is not None and bb.humanizer.cfg.get("skill_shuffle", True):
                    key = bb.humanizer.choice(ready)
                else:
                    index = bb.notes.get("move_idx", 0)
                    key = ready[index % len(ready)]
                    bb.notes["move_idx"] = index + 1
                inp.press(key)
                self._note_skill(bb, state, key)
                bb.notes["attack_attempt"] = now
                bb.mark("attack")

    def _manage_fight_mode(self, bb: Blackboard, inp) -> None:
        # Solo en el lure: Defensivo fuera del burst (para juntar enemigos y
        # lurear), Ofensivo justo cuando va a lanzar skills.
        if not self.cfg.get("lure_aoe", False) or not self.cfg.get("manage_fight_mode", True):
            return
        cast = int(self.cfg.get("cast_fight_mode", 1))
        idle = int(self.cfg.get("idle_fight_mode", 3))
        want = cast if bb.notes.get("lure_state") == "fight" else idle
        if want == bb.notes.get("fight_mode_set"):
            return
        inp.set_fight_mode(want)
        bb.notes["fight_mode_set"] = want
        if want == cast:
            bb.notes["fight_mode_at"] = time.time()

    def _fight_mode_waiting(self, bb: Blackboard) -> bool:
        # tras cambiar a Ofensivo, dar un momento a que el servidor lo aplique
        if not self.cfg.get("manage_fight_mode", True):
            return False
        delay = float(self.cfg.get("fight_mode_delay", 0.3))
        at = bb.notes.get("fight_mode_at", 0.0)
        return delay > 0 and at and (time.time() - at) < delay


class LootBehavior(Behavior):
    name = "loot"
    priority = 88

    def __init__(self, cfg: dict, settings: dict, world=None):
        super().__init__(cfg, settings)
        self.world = world

    def _pending(self, bb: Blackboard) -> list:
        return bb.notes.setdefault("corpses_pending", [])

    def _mark_looted(self, bb: Blackboard, cid: str) -> None:
        bb.notes.setdefault("looted_ids", set()).add(cid)

    def _reset_phase(self, bb: Blackboard) -> None:
        bb.notes["loot_phase_start"] = 0.0
        bb.notes["loot_phase_elapsed"] = 0.0
        bb.notes["loot_progress_at"] = 0.0
        bb.notes["loot_progress_d"] = None
        bb.notes["loot_prog_sig"] = None

    def _whitelist(self) -> set:
        """Items (nombre o id) que SIEMPRE se lootean, aunque el loot este off."""
        raw = self.cfg.get("whitelist")
        if isinstance(raw, str):
            raw = raw.replace(";", ",").split(",")
        if not isinstance(raw, (list, tuple)):
            return set()
        return {str(x).strip().lower() for x in raw if str(x).strip()}

    def _whitelist_ids(self, bb: Blackboard) -> set:
        """Whitelist (nombres/ids) + ids traducidos de los nombres via la tabla
        aprendida de los mensajes de botin (`bb.loot_items`)."""
        names = self._whitelist()
        ids = set(names)
        li = getattr(bb, "loot_items", None)
        if li is not None:
            for n in names:
                iid = li.id_for(n)
                if iid is not None:
                    ids.add(str(iid))
        return ids

    @staticmethod
    def _has_whitelisted(corpse: dict, idset: set) -> bool:
        for it in (corpse.get("items") or []):
            if str(it.get("id", "")).strip() in idset:
                return True
            if str(it.get("name", "")).strip().lower() in idset:
                return True
        return False

    def _coverage(self, tx: int, ty: int, pending: list) -> int:
        return sum(1 for c in pending if max(abs(tx - c["x"]), abs(ty - c["y"])) <= 1)

    @staticmethod
    def _candidate_tiles(pending: list):
        """Tiles que pueden cubrir algun cuerpo: la union de los 3x3 alrededor de
        cada cuerpo. Cualquier tile con cobertura>0 esta ahi, asi que no hace
        falta escanear toda la caja (que es enorme si los cuerpos estan lejos)."""
        tiles = set()
        for c in pending:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    tiles.add((c["x"] + dx, c["y"] + dy))
        return sorted(tiles)

    def _best_coverage(self, pending: list, walkable=None) -> int:
        if not pending:
            return 0
        best = 0
        for (x, y) in self._candidate_tiles(pending):
            if walkable is not None and not walkable(x, y):
                continue
            cov = self._coverage(x, y, pending)
            if cov > best:
                best = cov
        return best

    def _best_tile(self, pending: list, pos: Vec3, walkable=None):
        # tile que cubre mas cuerpos (Chebyshev 1); a igual cobertura, el mas
        # cercano. Es el mismo criterio que bestCoverTile del agente. `walkable`
        # (opcional) descarta tiles no transitables: no fijar un destino en pared.
        if not pending:
            return None
        best = None
        best_cov = 0
        best_d = 1 << 30
        for (x, y) in self._candidate_tiles(pending):
            if walkable is not None and not walkable(x, y):
                continue
            cov = self._coverage(x, y, pending)
            if cov <= 0:
                continue
            d = max(abs(pos.x - x), abs(pos.y - y))
            if cov > best_cov or (cov == best_cov and d < best_d):
                best_cov = cov
                best_d = d
                best = (x, y)
        return best

    def _reachable_target(self, state, pending, wm, otmm):
        """Tile de cobertura CON camino real (astar del bot).

        Recorre los candidatos por cobertura (y cercania) y devuelve el primero
        al que el astar llega, para no fijar un destino inalcanzable (que dejaba
        cuerpos vecinos sin recoger). Devuelve (target, path)."""
        pos = state.player.pos
        cands = []
        for (x, y) in self._candidate_tiles(pending):
            if not otmm.pathable(x, y, pos.z):
                continue
            cov = self._coverage(x, y, pending)
            if cov <= 0:
                continue
            cands.append((cov, max(abs(pos.x - x), abs(pos.y - y)), x, y))
        cands.sort(key=lambda c: (-c[0], c[1]))
        for _cov, _d, x, y in cands[:12]:
            path = wm.plan_to(pos, x, y)
            if path:
                return (x, y), path
        return None, None

    def _finalize(self, state, bb: Blackboard) -> None:
        """Marca como looteados los cuerpos de la ultima recogida.

        Confirmacion por evento: en cuanto el servidor manda un mensaje de botin
        (`state.loots` sube) se da por recogido — rapido, sin esperas mecanicas.
        Fallback: si no llega confirmacion en `confirm_timeout`, se marca igual
        (cuerpo vacio o ya recogido al pasar) para no quedarse colgado."""
        rec = bb.notes.get("collect_rec")
        if not rec:
            return
        now = time.time()
        elapsed = now - rec["t"]
        loots = int(getattr(state, "loots", -1)) if state is not None else -1
        loot0 = rec.get("loot0")
        confirmed = (loots >= 0 and loot0 is not None and loots > int(loot0))
        if confirmed:
            if elapsed < 0.12:
                return
        elif elapsed < float(self.cfg.get("confirm_timeout", 1.5)):
            return
        looted = bb.notes.setdefault("looted_ids", set())
        looted.update(rec["ids"])
        bb.count("looted", len(rec["ids"]))
        pending = self._pending(bb)
        pending[:] = [c for c in pending if c["id"] not in looted]
        bb.notes.pop("collect_rec", None)
        if not pending:
            self._reset_phase(bb)

    def _enemy_near(self, state: GameState) -> bool:
        # no caminar hacia cuerpos si hay un enemigo pegado (seguridad). Los
        # enemigos a media distancia no impiden lootear: si no, en un spawn
        # nunca se lotea nada.
        rng = int(self.cfg.get("enemy_range", 3))
        return nearest(state, is_enemy, max_range=rng) is not None

    def evaluate(self, state: GameState, bb: Blackboard) -> bool:
        if bb.paused:
            return False
        enabled = bool(self.cfg.get("enabled", True))
        wl = self._whitelist()
        # loot desactivado sin whitelist -> nada. Con whitelist -> solo los cuerpos
        # que contienen un item de la lista (aunque el loot este off).
        if not enabled and not wl:
            return False
        now = time.time()
        last_eval = bb.notes.get("loot_eval_at", 0.0)
        bb.notes["loot_eval_at"] = now
        ceded = bool(bb.notes.get("revive_rec") or bb.notes.get("revive_urgent")
                     or bb.notes.get("revive_fast"))
        fighting = enemies_on_screen(state)
        # El temporizador de fase NO debe avanzar mientras loot cede el turno
        # (revive), NO se evalua (crisis/pausa) o hay enemigos en pantalla (se esta
        # peleando). Si contara ese tiempo, al volver descartaria cuerpos
        # ALCANZABLES.
        if ceded or fighting or (last_eval and now - last_eval > 1.5):
            if bb.notes.get("loot_phase_start", 0.0):
                bb.notes["loot_progress_at"] = now
        # Loot es la ULTIMA accion antes de seguir la ruta: primero terminar de
        # matar a TODOS los enemigos en pantalla (si no, lotear a media pelea deja
        # al pokemon sin lanzar skills -> muertes) y ceder al revive.
        if ceded or fighting:
            return False
        self._finalize(state, bb)
        pending = self._pending(bb)
        if not enabled:
            idset = self._whitelist_ids(bb)
            pending[:] = [c for c in pending if self._has_whitelisted(c, idset)]
        if not pending:
            self._reset_phase(bb)
            bb.notes.pop("loot_nav_fp", None)
            return False
        px, py, pz = state.player.pos.x, state.player.pos.y, state.player.pos.z
        # solo cuerpos del mismo piso. NO se descartan por lejos: se camina a
        # ellos. `reach > 0` queda como tope opcional (0 = sin limite).
        reach = int(self.cfg.get("reach", 0) or 0)
        pending[:] = [c for c in pending
                      if c["z"] == pz
                      and (reach <= 0 or max(abs(px - c["x"]), abs(py - c["y"])) <= reach)]
        if not pending:
            self._reset_phase(bb)
            return False
        now = time.time()
        if bb.notes.get("loot_phase_start", 0.0) == 0.0:
            bb.notes["loot_phase_start"] = now
            bb.notes["loot_progress_at"] = now
        # la fase NO se mide como tiempo total: se abandona solo si se estanca
        # `phase_secs` SIN progreso (no acercarse al cuerpo pendiente mas cercano
        # ni cambiar el conjunto). Asi se camina a los cuerpos lejanos sin darse
        # por vencido a mitad de camino, y a la vez no se persigue un cuerpo
        # inalcanzable para siempre.
        sig = tuple(sorted(c["id"] for c in pending))
        dmin = min(max(abs(px - c["x"]), abs(py - c["y"])) for c in pending)
        prev_d = bb.notes.get("loot_progress_d")
        if sig != bb.notes.get("loot_prog_sig") or prev_d is None or dmin < prev_d:
            bb.notes["loot_progress_at"] = now
        bb.notes["loot_prog_sig"] = sig
        bb.notes["loot_progress_d"] = dmin
        bb.notes["loot_phase_elapsed"] = now - bb.notes["loot_phase_start"]
        if now - bb.notes.get("loot_progress_at", now) > float(self.cfg.get("phase_secs", 10.0)):
            for c in pending:
                self._mark_looted(bb, c["id"])
            pending.clear()
            self._reset_phase(bb)
            bb.notes.pop("loot_nav_fp", None)
            return False
        return True

    def act(self, state: GameState, bb: Blackboard, inp) -> None:
        pending = self._pending(bb)
        if not pending:
            return
        px, py = state.player.pos.x, state.player.pos.y
        pz = pending[0]["z"]
        wm = self.world
        otmm = getattr(wm, "otmm", None) if wm is not None else None
        walkable = None
        if otmm is not None and getattr(otmm, "ready", False):
            z0 = state.player.pos.z
            walkable = lambda x, y: otmm.pathable(x, y, z0)
        cur = self._coverage(px, py, pending)
        best = self._best_coverage(pending, walkable)
        bb.notes["loot_cur"] = cur
        bb.notes["loot_best"] = best
        bb.notes["loot_bopt"] = self._best_tile(pending, state.player.pos, walkable)

        # lootear SIEMPRE que haya algo adyacente: collectLoot recoge TODOS los
        # cuerpos vecinos de una vez. Antes se caminaba a la casilla de "maxima
        # cobertura" (que podia ser inalcanzable) y se dejaban cuerpos vecinos sin
        # recoger. Ahora es greedy; si no hay nada adyacente, se navega.
        if cur >= 1:
            # recoger sin parar (el auto-loot recoge por cercania); la
            # confirmacion llega con el mensaje de botin del servidor
            # (`state.loots`), asi no hace falta la parada + espera mecanica.
            if not bb.notes.get("collect_rec") and bb.ready("collect", float(self.cfg.get("collect_interval", 0.8))):
                inp.loot()
                bb.count("loot")
                bb.mark("collect")
                ids = [c["id"] for c in pending
                       if max(abs(px - c["x"]), abs(py - c["y"])) <= 1]
                bb.notes["collect_rec"] = {
                    "t": time.time(), "ids": ids,
                    "loot0": int(getattr(state, "loots", -1)),
                }
            return

        bodies = [[c["x"], c["y"]] for c in pending]
        # enviar el destino una sola vez (el agente lo recorre solo); reenviar
        # solo si cambia el conjunto de cuerpos o como red de seguridad.
        fp = tuple(sorted((c["x"], c["y"]) for c in pending))
        since = time.time() - bb.notes.get("loot_nav_t", 0.0)
        if fp != bb.notes.get("loot_nav_fp") or since > float(self.cfg.get("resend_secs", 4.0)):
            # navegar al mejor tile de cobertura QUE TENGA CAMINO real (no fijar
            # un destino inalcanzable)
            path = None
            if wm is not None and otmm is not None and getattr(otmm, "ready", False):
                _tgt, path = self._reachable_target(state, pending, wm, otmm)
            if path and len(path) > 1:
                # camino real (otmm) -> navpath; el agente lo sigue con autoWalk.
                # Se manda un tramo largo para no parar a mitad en cuerpos lejanos.
                inp.walk_path(path[1:1 + 200], pz)
            elif len(bodies) == 1:
                inp.stand_near(Vec3(bodies[0][0], bodies[0][1], pz))
            else:
                inp.stand_near_many(bodies, pz)
            bb.notes["loot_nav_fp"] = fp
            bb.notes["loot_nav_t"] = time.time()


class ReviveBehavior(Behavior):
    name = "revive"
    priority = 80

    def __init__(self, cfg: dict, settings: dict, combat_cfg: Optional[dict] = None):
        super().__init__(cfg, settings)
        self._revives_done = 0
        self.route_slot = None            # slot del Pokemon elegido para la ruta
        self.combat = combat_cfg or {}    # cfg del combate (attack_range, ready_pct)

    def set_route_slot(self, slot) -> None:
        """Vincula el revive al slot del Pokemon elegido para la ruta: si hay
        seleccion, se reviva ese slot; si no, cae a revive.slot de config."""
        self.route_slot = int(slot) if slot not in (None, "", 0, "0") else None

    # --- slot ---
    def _slot(self) -> int:
        if self.route_slot:
            return self.route_slot
        return int(self.cfg.get("slot", 3))

    def _slot_hp(self, state: GameState):
        for p in state.party:
            if p.slot == self._slot():
                return p.hp_pct
        return None

    def revives_left(self, state: GameState):
        """Cantidad de revives (item configurado) segun el inventario. Devuelve
        None si no hay datos fiables (agente viejo o backpack cerrado), para no
        desconectar por error."""
        item = int(self.cfg.get("item", 2269))
        bc = getattr(state, "bag_counts", None)
        if bc:
            val = bc.get(str(item))
            if val is None:
                val = bc.get(item)
            try:
                return int(val or 0)
            except (TypeError, ValueError):
                return None
        return None

    # --- combate: rango, combo, stun ---
    def _attack_range(self) -> int:
        if self.cfg.get("use_attack_range", True):
            return int(self.combat.get("attack_range", self.cfg.get("attack_range", 3)))
        return int(self.cfg.get("attack_range", 3))

    def _ready_pct(self) -> float:
        return float(self.combat.get("ready_pct", 100))

    def _ref(self, state: GameState) -> Vec3:
        # referencia de distancia: el pokemon propio; si no se conoce, el player
        pp = getattr(state, "pokemon_pos", None)
        if pp:
            return Vec3(int(pp[0]), int(pp[1]), int(pp[2]))
        return state.player.pos

    def _alive_in_range(self, state: GameState) -> int:
        """Enemigos atacables vivos dentro del rango de ataque del summon."""
        rng = self._attack_range()
        ref = self._ref(state)
        return sum(1 for c in state.creatures if is_enemy(c) and ref.distance(c.pos) <= rng)

    def _combo_keys(self, state: GameState) -> list:
        # combo efectivo: el del lure, si no el orden general, si no las AoE
        combo = [str(k) for k in (getattr(state, "lure_order", []) or [])]
        if not combo:
            combo = [str(k) for k in (getattr(state, "skill_order", []) or [])]
        if not combo:
            combo = [str(m.get("key")) for m in (state.moves or []) if m.get("aoe")]
        return combo

    def _combo_status(self, state: GameState):
        """(usado, agotado): alguna skill del combo en cooldown / ninguna lista."""
        by_key = {str(m.get("key")): m for m in (state.moves or [])}
        combo = [k for k in self._combo_keys(state) if k in by_key]
        if not combo:
            return False, False
        ready_pct = self._ready_pct()
        ready = [k for k in combo if isinstance(by_key[k].get("pct"), (int, float))
                 and by_key[k]["pct"] >= ready_pct]
        return (len(ready) < len(combo)), (len(ready) == 0)

    def _combo_time(self) -> float:
        try:
            return float(self.cfg.get("combo_time", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _combo_waiting(self, bb: Blackboard) -> bool:
        """True mientras no se haya cumplido el tiempo de combo (desde la primera
        skill del ciclo): no revivir todavia, para no cortar la animacion de las
        skills (si se corta, esa skill no llega a hacer dano)."""
        ct = self._combo_time()
        if ct <= 0:
            return False
        start = bb.notes.get("combo_start")
        if not start:
            return False
        return (time.time() - start) < ct

    def _stun_active(self, state: GameState, bb: Blackboard) -> bool:
        # proxy del stun del enemigo: se lanzo una skill de stun hace < stun_secs
        if not self.cfg.get("on_stun", True):
            return False
        last = bb.notes.get("last_stun_at", 0.0)
        return last > 0 and (time.time() - last) <= float(self.cfg.get("stun_secs", 2.5))

    def urgent(self, state: GameState, bb: Blackboard) -> bool:
        """Revive de alta prioridad (debilitado / agotado / combo en cooldown con
        enemigos vivos stuneados): loot y captura deben ceder para no robarle el
        turno. El stun solo cuenta si ademas hace falta revivir (combo en
        cooldown); por si solo NO es urgente."""
        hp = self._slot_hp(state)
        if hp is not None and hp <= 0:
            return True
        used, exhausted = self._combo_status(state)
        if exhausted:
            return True
        return (used and self._stun_active(state, bb)
                and self._alive_in_range(state) > 0)

    def evaluate(self, state: GameState, bb: Blackboard) -> bool:
        if bb.paused or not self.cfg.get("enabled", True):
            return False
        hp = self._slot_hp(state)
        dead = hp is not None and hp <= 0
        used, exhausted = self._combo_status(state)
        stun = self._stun_active(state, bb)
        rec = bb.notes.get("revive_rec")
        now = time.time()
        # tiempo de combo: si el combo ya no tiene skills en cooldown, el ciclo
        # termino -> reiniciar el reloj (la proxima primera skill lo arranca).
        if not used:
            bb.notes.pop("combo_start", None)
        # guarda de recuperacion: tras un revive el estado del juego tarda en
        # reflejarlo; no iniciar otra secuencia hasta verlo recuperado (vivo y
        # combo sin cooldowns). Evita el doble revive por lag.
        if not dead and not used:
            bb.notes["revive_ready"] = True
        # "hace falta" revivir: pokemon debilitado o alguna skill del combo en
        # cooldown. El stun NO dispara por si solo: solo facilita el caso B
        # (revivir con enemigos vivos). Si no hace falta, no se revive.
        need = dead or used
        if need:
            bb.notes["revive_pending"] = True
        elif not rec:
            bb.notes.pop("revive_pending", None)
            bb.notes.pop("revive_fast", None)
        if rec:
            # la secuencia ya esta en marcha: se completa SIEMPRE
            return True
        if not need:
            return False
        # si acaba de revivir y aun no se recupero, esperar (con reintento)
        if not bb.notes.get("revive_ready", True):
            if now - bb.notes.get("revive_done", 0.0) < float(self.cfg.get("revive_retry_secs", 3.0)):
                return False
        if dead:
            # URGENTE: pokemon debilitado -> revivir ya, lo mas rapido posible
            bb.notes["revive_fast"] = True
            return True

        # tiempo de combo: no revivir hasta que pasen `combo_time` s desde la
        # primera skill del ciclo (deja que las animaciones terminen y hagan dano).
        if self._combo_waiting(bb):
            return False

        in_range = self._alive_in_range(state)
        min_done = now - bb.notes.get("revive_done", 0.0)
        last_aoe = now - bb.notes.get("last_aoe_at", 0.0)

        if in_range == 0:
            # A) no queda enemigo vivo en rango de ataque -> revivir.
            #    Si ademas no hay skills (agotado), rapido.
            fast = exhausted
            if not fast and last_aoe < float(self.cfg.get("after_aoe_wait_secs", 1.0)):
                return False
            guard = (float(self.cfg.get("fast_min_interval_secs", 0.6)) if fast
                     else float(self.cfg.get("min_interval_secs", 8.0)))
            if min_done < guard:
                return False
            if fast:
                bb.notes["revive_fast"] = True
            return True
        if stun:
            # B) quedan vivos pero aturdidos -> revivir rapido (el stun dura poco)
            if last_aoe < float(self.cfg.get("after_aoe_wait_secs", 1.0)):
                return False
            if min_done < float(self.cfg.get("stun_min_interval_secs", 0.6)):
                return False
            bb.notes["revive_fast"] = True
            return True
        if exhausted:
            # C) sin skills del combo para lanzar -> resetear igual, lo mas rapido
            if min_done < float(self.cfg.get("fast_min_interval_secs", 0.6)):
                return False
            bb.notes["revive_fast"] = True
            return True
        # D) vivos sin stun y con skills listas -> seguir casteando
        return False

    def act(self, state: GameState, bb: Blackboard, inp) -> None:
        now = time.time()
        slot = self._slot()
        hp0 = self._slot_hp(state)
        dead = hp0 is not None and hp0 <= 0
        fast = dead or bool(bb.notes.get("revive_fast"))
        cd = float(self.cfg.get("click_delay", 0.15))
        vd = float(self.cfg.get("verify_delay", 0.5))
        ww = float(self.cfg.get("withdraw_wait_secs", 0.5))
        if fast:
            # revive urgente/rapido: minimizar cada espera
            cd = float(self.cfg.get("urgent_click_delay", 0.05))
            vd = float(self.cfg.get("urgent_verify_delay", 0.25))
            ww = float(self.cfg.get("urgent_withdraw_wait", 0.25))
        rec = bb.notes.get("revive_rec")
        if rec is None:
            bb.notes["revive_rec"] = {"slot": slot, "step": "recall", "t": now,
                                      "first": (self._revives_done == 0 and not fast)}
            return
        step = rec["step"]
        if step == "recall":
            # el pokemon debe estar guardado: si hay activo, retirarlo.
            # La PRIMERA vez se esperan 2 s ANTES de guardarlo; las siguientes no.
            if state.active_pokemon_name:
                pre = float(self.cfg.get("first_revive_wait_secs", 2.0)) if rec.get("first") else 0.0
                if now - rec["t"] >= cd + pre:
                    inp.click_slot(slot)
                    rec["step"] = "recall_wait"
                    rec["t"] = now
            else:
                rec["step"] = "revive"
                rec["t"] = now
            return
        if step == "recall_wait":
            # confirmar que quedo guardado (sin espera extra); tope ww
            if not state.active_pokemon_name or now - rec["t"] > ww:
                rec["step"] = "revive"
                rec["t"] = now
            return
        if step == "revive":
            # mueve el cursor al slot (XTest) y el agente aplica el revive
            inp.revive(slot, int(self.cfg.get("item", 2269)))
            rec["step"] = "call"
            rec["t"] = now
            return
        # call: sacar el pokemon tras revivir/resetear el cooldown
        if now - rec["t"] >= vd:
            inp.call_slot(slot)
            post = self.cfg.get("post_mode")
            if post is not None:
                # volver a defensivo para retomar el lure
                inp.set_fight_mode(int(post))
                bb.notes["fight_mode_set"] = int(post)
            bb.notes["revive_done"] = now
            bb.notes.pop("revive_rec", None)
            bb.notes.pop("revive_fast", None)
            # el ciclo del combo termina: reiniciar el reloj del tiempo de combo
            bb.notes.pop("combo_start", None)
            # exigir recuperacion (estado) antes de permitir otro revive
            bb.notes["revive_ready"] = False
            bb.count("revive")
            self._revives_done += 1


class RecoveryBehavior(Behavior):
    """Reconexion (siempre el mismo personaje) y recuperacion tras muerte.

    Flujo: al morir -> cierra la ventana de muerte (sin recuperar), espera el
    respawn, usa la habilidad **Teleport** (un pokemon del equipo, p. ej. Abra)
    para volver a la zona y sigue una **ruta de recall** grabada por el usuario
    hasta el **inicio de la ruta activa**, donde reanuda el farmeo.

    Prioridad por encima de crisis: mientras dura la recuperacion preempta todo.
    """

    name = "recovery"
    priority = 101

    def __init__(self, cfg: dict, settings: dict, route: Optional[Route] = None,
                 world=None, routes_file: str = ""):
        super().__init__(cfg, settings)
        self.route = route
        self.world = world
        self.routes_file = routes_file or ""
        self.locations_file = (os.path.join(os.path.dirname(routes_file), "pxg_locations.json")
                               if routes_file else "")
        self._recall: Optional[Route] = None

    # --- deteccion ---
    @staticmethod
    def _is_dead(state: GameState) -> bool:
        return bool(getattr(state, "dead", False) or getattr(state, "death_window", False))

    def evaluate(self, state: GameState, bb: Blackboard) -> bool:
        if bb.paused or not self.cfg.get("enabled", True):
            return False
        if not getattr(state, "connected", False):
            return True
        if bb.notes.get("rec_st") in ("dead", "teleport", "recall",
                                      "fly_up", "fly_move", "fly_down", "fly_off"):
            return True
        return self._is_dead(state)

    # --- ruta de recall ---
    def _load_recall(self, name: str) -> Optional[Route]:
        if not name or not self.routes_file or not os.path.exists(self.routes_file):
            return None
        try:
            with open(self.routes_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return None
        routes = data.get("routes", {}) if isinstance(data, dict) else {}
        entry = routes.get(name)
        if not entry or not entry.get("waypoints"):
            return None
        try:
            return Route.from_config(entry["waypoints"], loop=False)
        except (ValueError, TypeError, KeyError):
            return None

    # --- ubicacion de teleport mas cercana (ciudad) ---
    def _nearest_location(self):
        """Ubicacion de teleport (region, name, dist) mas cercana al inicio de la ruta."""
        if self.route is None or not self.route.waypoints:
            return None
        w = self.route.waypoints[0]
        path = self.locations_file
        if not path or not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as handle:
                locs = json.load(handle)
        except (OSError, ValueError):
            return None
        best = None
        best_d = 1 << 30
        for loc in locs or []:
            try:
                d = max(abs(int(loc["x"]) - w.x), abs(int(loc["y"]) - w.y))
            except (KeyError, TypeError, ValueError):
                continue
            if d < best_d:
                best_d = d
                best = loc
        if best is None:
            return None
        return {"region": str(best.get("region", "")), "name": str(best.get("name", "")),
                "x": best.get("x"), "y": best.get("y"), "dist": best_d}

    # --- fly ---
    def _fly_slot(self):
        slot = (self.cfg.get("fly", {}) or {}).get("pokemon_slot")
        try:
            return int(slot) if slot else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_flying(state: GameState) -> bool:
        # volando = hay pokemon activo pero su ownsummon no aparece (fusionado)
        return bool(getattr(state, "active_pokemon_name", "")) and not getattr(state, "pokemon_pos", None)

    def _fly_up(self, state: GameState, bb: Blackboard, inp, now: float) -> None:
        if not self._is_flying(state):
            if now - bb.notes.get("rec_fly_at", 0.0) > 2.0:
                inp.fly(self._fly_slot())
                bb.notes["rec_fly_at"] = now
            return
        if state.player.pos.z != 1:
            if now - bb.notes.get("rec_flyto_at", 0.0) > 0.5:
                inp.fly_to(1)
                bb.notes["rec_flyto_at"] = now
            return
        bb.notes["rec_st"] = "fly_move"
        bb.notes["rec_move_started"] = now
        bb.notes.pop("rec_move_key", None)

    def _fly_move(self, state: GameState, bb: Blackboard, inp, now: float) -> None:
        if not self._is_flying(state):
            bb.notes["rec_st"] = "fly_up"
            return
        if self.route is None or not self.route.waypoints:
            self._finish(bb)
            return
        w = self.route.waypoints[0]
        p = state.player.pos
        if max(abs(p.x - w.x), abs(p.y - w.y)) <= 2:
            bb.notes["rec_st"] = "fly_down"
            bb.notes.pop("rec_flyto_at", None)
            return
        key = (w.x, w.y)
        if (bb.notes.get("rec_move_key") != key
                or (not state.is_walking and now - bb.notes.get("rec_move_at", 0.0) > 1.0)):
            inp.walk_to(Vec3(w.x, w.y, 1))
            bb.notes["rec_move_key"] = key
            bb.notes["rec_move_at"] = now
        if now - bb.notes.get("rec_move_started", now) > float(self.cfg.get("fly_move_timeout_secs", 180.0)):
            self._finish(bb)

    def _fly_down(self, state: GameState, bb: Blackboard, inp, now: float) -> None:
        if self.route is None or not self.route.waypoints:
            self._finish(bb)
            return
        z = self.route.waypoints[0].z
        if state.player.pos.z != z:
            if now - bb.notes.get("rec_flyto_at", 0.0) > 0.5:
                inp.fly_to(z)
                bb.notes["rec_flyto_at"] = now
            return
        bb.notes["rec_st"] = "fly_off"
        bb.notes.pop("rec_off_at", None)

    def _fly_off(self, state: GameState, bb: Blackboard, inp, now: float) -> None:
        if not self._is_flying(state):
            self._finish(bb)
            return
        if now - bb.notes.get("rec_off_at", 0.0) > 1.5:
            inp.order_self()
            bb.notes["rec_off_at"] = now

    # --- transiciones ---
    def _enter_dead(self, state: GameState, bb: Blackboard, inp) -> None:
        bb.notes["rec_st"] = "dead"
        bb.notes["rec_death_at"] = time.time()
        w0 = None
        if self.route is not None and self.route.waypoints:
            w = self.route.waypoints[0]
            w0 = (w.x, w.y, w.z)
        bb.notes["rec_route_start"] = w0
        # pokemon de farmeo que estaba fuera (para volver a sacarlo tras teleportar)
        bb.notes["rec_farm_slot"] = self._active_slot(state)
        # ciudad de teleport mas cercana al inicio de la ruta (region + nombre)
        bb.notes["rec_dest"] = self._nearest_location()
        for key in ("rec_teleport_sent", "rec_teleport_from", "rec_recall_idx",
                    "rec_recall_sent", "rec_recall_best", "rec_recall_best_at",
                    "rec_respawn_at", "rec_recall_started", "rec_flyto_at",
                    "rec_move_key", "rec_move_at", "rec_move_started", "rec_off_at"):
            bb.notes.pop(key, None)
        if self.cfg.get("dismiss_death_window", True):
            inp.dismiss_death()
            bb.notes["rec_dismiss_at"] = time.time()

    def _finish(self, bb: Blackboard) -> None:
        bb.notes["rec_st"] = "normal"
        for key in ("rec_teleport_sent", "rec_teleport_from", "rec_recall_idx",
                    "rec_recall_sent", "rec_recall_best", "rec_recall_best_at",
                    "rec_respawn_at", "rec_recall_started", "rec_fly_at",
                    "rec_flyto_at", "rec_move_key", "rec_move_at", "rec_move_started",
                    "rec_off_at"):
            bb.notes.pop(key, None)
        self._recall = None
        if self.route is not None:
            self.route.index = 0
            self.route.forward = True
        # que la ruta vuelva a sacar su pokemon al reanudar
        bb.notes["route_recall_call"] = True

    # --- ciclo ---
    def act(self, state: GameState, bb: Blackboard, inp) -> None:
        now = time.time()
        if getattr(state, "connected", False) and state.player.name:
            bb.notes["last_character"] = state.player.name

        if not getattr(state, "connected", False):
            self._reconnect(state, bb, inp, now)
            return

        st = bb.notes.get("rec_st", "normal")
        if st == "dead":
            self._dead(state, bb, inp, now)
        elif st == "teleport":
            self._teleport(state, bb, inp, now)
        elif st == "recall":
            self._recall_step(state, bb, inp, now)
        elif st == "fly_up":
            self._fly_up(state, bb, inp, now)
        elif st == "fly_move":
            self._fly_move(state, bb, inp, now)
        elif st == "fly_down":
            self._fly_down(state, bb, inp, now)
        elif st == "fly_off":
            self._fly_off(state, bb, inp, now)
        elif self._is_dead(state):
            self._enter_dead(state, bb, inp)

    # --- reconexion (mismo personaje) ---
    def _reconnect(self, state: GameState, bb: Blackboard, inp, now: float) -> None:
        selector = getattr(state, "selector", False)
        online = getattr(state, "online", False)
        # cargando (online pero sin jugador): no spamear
        if online and not selector:
            return
        if now - bb.notes.get("rec_reconnect_at", 0.0) < float(self.cfg.get("retry_secs", 5.0)):
            return
        bb.notes["rec_reconnect_at"] = now
        name = str(self.cfg.get("character", "") or "") or str(bb.notes.get("last_character", "") or "")
        inp.open_game(name)
        # si estabamos recuperando, tras reconectar se reintenta el teleport
        if bb.notes.get("rec_st") in ("dead", "teleport", "recall"):
            bb.notes["rec_st"] = "teleport"
            bb.notes["rec_respawn_at"] = now
            bb.notes.pop("rec_teleport_sent", None)

    # --- muerte ---
    def _dead(self, state: GameState, bb: Blackboard, inp, now: float) -> None:
        if self.cfg.get("dismiss_death_window", True) and getattr(state, "death_window", False):
            if now - bb.notes.get("rec_dismiss_at", 0.0) > 1.0:
                inp.dismiss_death()
                bb.notes["rec_dismiss_at"] = now
        if self._is_dead(state):
            return
        # respawn: vivo de nuevo -> teleport
        bb.notes["rec_respawn_at"] = now
        bb.notes["rec_st"] = "teleport"
        bb.notes["rec_teleport_sent"] = 0.0

    # --- teleport ---
    def _teleport_slot(self):
        slot = (self.cfg.get("teleport", {}) or {}).get("pokemon_slot")
        try:
            return int(slot) if slot else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _active_slot(state: GameState):
        for p in state.party:
            if getattr(p, "active", False):
                return int(p.slot)
        return None

    @staticmethod
    def _slot_active(state: GameState, slot) -> bool:
        for p in state.party:
            if int(p.slot) == int(slot):
                return bool(p.active)
        return False

    def _restore_farm_pokemon(self, bb: Blackboard, inp) -> None:
        slot = bb.notes.get("rec_farm_slot")
        if slot:
            inp.call_slot(int(slot))

    def _teleport(self, state: GameState, bb: Blackboard, inp, now: float) -> None:
        tcfg = self.cfg.get("teleport", {}) or {}
        if not tcfg.get("enabled", True):
            self._after_teleport(state, bb, inp)
            return
        # 1) sacar al pokemon con la habilidad Teleport (si hay uno configurado)
        tp_slot = self._teleport_slot()
        if tp_slot and not self._slot_active(state, tp_slot):
            if now - bb.notes.get("rec_call_at", 0.0) > 1.0:
                inp.call_slot(tp_slot)
                bb.notes["rec_call_at"] = now
            return
        # 2) teleportar (por chat: h"<destino>): ciudad mas cercana (auto) o config
        sent = bb.notes.get("rec_teleport_sent", 0.0)
        if sent:
            fx, fy, fz = bb.notes.get("rec_teleport_from", (0, 0, 0))
            if state.player.pos.distance(Vec3(fx, fy, fz)) >= 5 or state.player.pos.z != fz:
                self._after_teleport(state, bb, inp)
                return
            if now - sent <= 15.0:
                return  # esperar a que se aplique el teleport
            bb.notes["rec_teleport_sent"] = 0.0  # no funciono: reintentar
        if getattr(state, "can_teleport", False):
            dest = ""
            rec_dest = bb.notes.get("rec_dest") or {}
            if str(self.cfg.get("travel_method", "auto")).lower() != "recall":
                dest = str(rec_dest.get("name", "") or "")
            if not dest:
                dest = str(tcfg.get("destination", "") or "")
            if dest:
                inp.teleport(dest)
                bb.notes["rec_teleport_sent"] = now
                bb.notes["rec_teleport_from"] = (state.player.pos.x, state.player.pos.y,
                                                 state.player.pos.z)
                return
        if now - bb.notes.get("rec_respawn_at", now) > float(self.cfg.get("teleport_timeout_secs", 240.0)):
            self._after_teleport(state, bb, inp)

    def _after_teleport(self, state: GameState, bb: Blackboard, inp) -> None:
        """Decide el metodo de vuelta tras teleportar: Outland (no se puede volar)
        -> ruta de recall; si no -> fly (subir, volar, bajar, desmontar)."""
        rec_dest = bb.notes.get("rec_dest") or {}
        region = str(rec_dest.get("region", "")).lower()
        method = str(self.cfg.get("travel_method", "auto")).lower()
        if method == "recall" or (method == "auto" and region == "outland"):
            self._restore_farm_pokemon(bb, inp)
            self._start_recall(state, bb)
        else:
            bb.notes["rec_st"] = "fly_up"
            bb.notes.pop("rec_fly_at", None)

    def _start_recall(self, state: GameState, bb: Blackboard) -> None:
        bb.notes["rec_st"] = "recall"
        bb.notes["rec_recall_idx"] = 0
        bb.notes["rec_recall_started"] = time.time()
        bb.notes.pop("rec_recall_sent", None)
        bb.notes.pop("rec_recall_best", None)
        name = str((self.cfg.get("teleport", {}) or {}).get("recall_route", "") or "")
        self._recall = self._load_recall(name)

    # --- recall (vuelta al inicio de la ruta, multi-piso) ---
    def _recall_step(self, state: GameState, bb: Blackboard, inp, now: float) -> None:
        route = self._recall
        if route is None or not route.waypoints:
            self._finish(bb)
            return
        idx = int(bb.notes.get("rec_recall_idx", 0))
        if idx >= len(route.waypoints):
            self._finish(bb)
            return
        wp = route.waypoints[idx]
        p = state.player.pos
        arrive = int(self.cfg.get("recall_arrive_distance", 1))
        if max(abs(p.x - wp.x), abs(p.y - wp.y)) <= arrive and p.z == wp.z:
            bb.notes["rec_recall_idx"] = idx + 1
            bb.notes.pop("rec_recall_sent", None)
            bb.notes.pop("rec_recall_best", None)
            return
        # si el waypoint esta en otro piso, caminar a su (x,y) en el piso actual:
        # al pisar la escalera el cliente cambia la z y se llega al waypoint.
        goal = Vec3(wp.x, wp.y, p.z)
        d = max(abs(p.x - goal.x), abs(p.y - goal.y))
        key = (goal.x, goal.y, goal.z)
        if bb.notes.get("rec_recall_sent") != key or (
            not state.is_walking and now - bb.notes.get("rec_recall_at", 0.0) > 0.6
        ):
            inp.walk_to(goal)
            bb.notes["rec_recall_sent"] = key
            bb.notes["rec_recall_at"] = now
        best = bb.notes.get("rec_recall_best")
        if best is None or d < best:
            bb.notes["rec_recall_best"] = d
            bb.notes["rec_recall_best_at"] = now
        elif now - bb.notes.get("rec_recall_best_at", now) > 2.5:
            # atascado y el waypoint es de otro piso -> intentar usar la escalera
            if wp.z != p.z:
                inp.left_click(goal)
            bb.notes["rec_recall_best_at"] = now
        if now - bb.notes.get("rec_recall_started", now) > float(self.cfg.get("recall_timeout_secs", 180.0)):
            self._finish(bb)


def make_walkable(state: GameState, static_map: Optional[list] = None):
    if static_map:
        rows = static_map

        def walk_static(x: int, y: int) -> bool:
            if 0 <= y < len(rows) and 0 <= x < len(rows[y]):
                return rows[y][x] not in "#X"
            return False

        return walk_static
    if state.tiles:
        z = state.player.pos.z

        def walk_tiles(x: int, y: int) -> bool:
            tile = state.tiles.get((x, y, z))
            return True if tile is None else tile.walkable

        return walk_tiles
    return lambda x, y: True


class RouteBehavior(Behavior):
    name = "route"
    priority = 10

    def __init__(self, cfg: dict, settings: dict, route: Optional[Route], world=None):
        super().__init__(cfg, settings)
        self.route = route
        self.world = world
        self._idle_until = 0.0
        self._idled_start = False
        self._looped = False
        self.pokemon = ""            # nombre del pokemon elegido para la ruta
        self.pokemon_slot = None     # slot de reserva si no hay nombre
        self._pending_call = False   # sacar el pokemon al empezar la ruta

    def set_pokemon(self, name, slot) -> None:
        """Selecciona el pokemon de la ruta; programa sacarlo al iniciarla."""
        name = str(name or "")
        if name == self.pokemon and slot == self.pokemon_slot:
            return
        self.pokemon = name
        self.pokemon_slot = slot
        self._pending_call = True
        self._idle_until = 0.0
        self._idled_start = False
        self._looped = False

    def _resolve_pokemon_slot(self, state: GameState):
        if self.pokemon:
            for p in state.party:
                if getattr(p, "name", "") == self.pokemon:
                    return p.slot
        if self.pokemon_slot is not None:
            return int(self.pokemon_slot)
        return None

    def _use_pokemon(self, state: GameState, bb: Blackboard, inp) -> None:
        """Al empezar la ruta, saca (callslot) el pokemon elegido, salvo que ya
        este out: en ese caso se deja como esta."""
        self._pending_call = False
        slot = self._resolve_pokemon_slot(state)
        if not slot:
            return
        # si el slot elegido YA esta out, dejarlo (no volver a clickearlo)
        for p in state.party:
            if p.slot == int(slot):
                if getattr(p, "active", False):
                    return
                break
        # fallback por nombre (por si el cliente solo reporta el activo por nombre)
        if self.pokemon and state.active_pokemon_name == self.pokemon:
            return
        inp.call_slot(int(slot))
        bb.notes["route_pokemon_called"] = int(slot)

    def evaluate(self, state: GameState, bb: Blackboard) -> bool:
        if bb.paused or not self.cfg.get("enabled", True) or self.route is None:
            return False
        # solo ceder mientras hay una secuencia de revive EN CURSO. El combo en
        # cooldown (`revive_pending`) NO debe parar la ruta: revive (prioridad 80
        # > ruta 10) ya preempta cuando le toca. Antes la ruta quedaba en IDLE
        # hasta que el revive cumplia su intervalo (varios segundos sin avanzar).
        if bb.notes.get("revive_rec"):
            return False
        return True

    def _detour_goal(self, bb: Blackboard, waypoint: Vec3, pos: Vec3) -> Vec3:
        # el recorrido dibujado se sigue tal cual (sin micro-desvios) salvo que
        # se active explicitamente route.detour
        if not self.cfg.get("detour", False):
            return waypoint
        hz = bb.humanizer
        if hz is None or not hz.should_detour():
            return waypoint
        dx = (waypoint.x > pos.x) - (waypoint.x < pos.x)
        dy = (waypoint.y > pos.y) - (waypoint.y < pos.y)
        if dx == 0 and dy == 0:
            return waypoint
        perps = [(dy, -dx), (-dy, dx)]
        hz.shuffle(perps)
        wm = self.world
        otmm = getattr(wm, "otmm", None) if wm else None
        for ox, oy in perps:
            gx, gy = waypoint.x + ox, waypoint.y + oy
            if otmm is not None and getattr(otmm, "ready", False):
                if not otmm.pathable(gx, gy, waypoint.z):
                    continue
            elif wm is not None:
                if not wm.walkable(gx, gy):
                    continue
            return Vec3(gx, gy, waypoint.z)
        return waypoint

    def _segment(self, pos: Vec3, final: Vec3) -> Vec3:
        """Punto de navegacion dentro del rango cargado hacia el waypoint final.
        Usa el camino REAL sobre otmm (astar), no la recta, para no cortar
        esquinas ni darse la vuelta al cruzar el eje del waypoint."""
        maxd = int(self.cfg.get("max_nav_dist", 8))
        if pos.distance(final) <= maxd:
            return final
        wm = self.world
        otmm = getattr(wm, "otmm", None) if wm is not None else None
        if wm is not None and otmm is not None and getattr(otmm, "ready", False):
            path = wm.plan_to(pos, final.x, final.y)
            if path:
                for p in path:
                    if pos.distance(p) >= maxd:
                        return p
                return path[-1]
        # fallback: recta + snap a caminable
        dx = (final.x > pos.x) - (final.x < pos.x)
        dy = (final.y > pos.y) - (final.y < pos.y)
        gx, gy = pos.x + dx * maxd, pos.y + dy * maxd
        if otmm is not None and getattr(otmm, "ready", False):
            for ox, oy in [(0, 0), (dx, 0), (0, dy), (dx, dy), (-dx, 0), (0, -dy)]:
                if otmm.pathable(gx + ox, gy + oy, final.z):
                    return Vec3(gx + ox, gy + oy, final.z)
        return Vec3(gx, gy, final.z)

    def _advance(self, bb: Blackboard, now: float, pos) -> None:
        self.route.advance()
        bb.notes.pop("route_key", None)
        bb.notes.pop("route_wp", None)
        bb.notes.pop("route_best", None)
        bb.notes.pop("route_seg", None)
        bb.notes.pop("route_tr_key", None)
        bb.notes.pop("route_tr_at", None)
        bb.notes.pop("route_tr_step_at", None)
        bb.notes.pop("route_tr_src_z", None)
        bb.notes["route_sent_at"] = 0.0
        bb.notes["route_pos"] = pos
        bb.notes["route_since"] = now

    def _do_transition(self, state: GameState, bb: Blackboard, inp, waypoint: Vec3,
                       now: float) -> None:
        """Waypoint marcado 'subir'/'bajar': la casilla es la escalera del piso
        actual. Navega al tile exacto y espera a que el cliente cambie la z al
        pisarlo; solo entonces avanza (no corta el cambio de piso)."""
        p = state.player.pos
        key = (waypoint.x, waypoint.y, waypoint.z)
        if bb.notes.get("route_tr_key") != key:
            bb.notes["route_tr_key"] = key
            bb.notes["route_tr_at"] = now
            bb.notes["route_tr_tries"] = 0
            bb.notes["route_tr_src_z"] = p.z
            bb.notes["route_key"] = None
            bb.notes.pop("route_tr_step_at", None)
        src_z = int(bb.notes.get("route_tr_src_z", waypoint.z))
        # la z cambio -> ya estamos en el otro piso
        if p.z != src_z:
            self._advance(bb, now, (p.x, p.y))
            return
        goal = Vec3(waypoint.x, waypoint.y, src_z)
        if p.x == waypoint.x and p.y == waypoint.y:
            # encima de la escalera: reenviar el paso por si el cliente no lo
            # disparo (pisarla es lo que cambia de piso)
            if now - bb.notes.get("route_tr_step_at", 0.0) > 0.6:
                inp.walk_to(goal)
                bb.notes["route_tr_step_at"] = now
        elif bb.notes.get("route_key") != key or (
            not state.is_walking
            and now - bb.notes.get("route_sent_at", 0.0) > float(self.cfg.get("resend_secs", 2.5))
        ):
            inp.walk_to(goal)
            bb.notes["route_key"] = key
            bb.notes["route_sent_at"] = now
        # no cambia la z: reintentar acercandose y, si se agota, saltar
        if now - bb.notes.get("route_tr_at", now) > float(self.cfg.get("transition_timeout_secs", 12.0)):
            bb.notes["route_tr_tries"] = int(bb.notes.get("route_tr_tries", 0)) + 1
            bb.notes["route_tr_at"] = now
            bb.notes["route_key"] = None
            if bb.notes["route_tr_tries"] >= 3:
                self._advance(bb, now, (p.x, p.y))

    def act(self, state: GameState, bb: Blackboard, inp) -> None:
        if bb.notes.pop("route_recall_call", False):
            self._pending_call = True
        if self._pending_call:
            self._use_pokemon(state, bb, inp)
            return
        now = time.time()
        # temporizador en idle al llegar al punto de inicio del recorrido
        if now < self._idle_until:
            return
        waypoint = self.route.current
        action = self.route.current_action
        pos = (state.player.pos.x, state.player.pos.y)
        pz = state.player.pos.z
        # transicion de piso explicita (subir/bajar) en este punto
        if action in ("up", "down"):
            self._do_transition(state, bb, inp, waypoint, now)
            return
        arrive = int(self.cfg.get("arrive_distance", 1))
        cross_z = waypoint.z != pz
        # "llegar" = estar en el piso del waypoint y a <= arrive en (x,y). Para un
        # waypoint de otro piso sin marcar, hay que cambiar de piso de verdad (la
        # z debe coincidir); no se avanza solo por cercania en (x,y).
        near_xy = max(abs(state.player.pos.x - waypoint.x),
                      abs(state.player.pos.y - waypoint.y)) <= arrive
        if pz == waypoint.z and near_xy:
            # al llegar al inicio del recorrido (waypoint 0) DESPUES del primer
            # loop, esperar 25-60 s (no en el arranque del bot)
            if (self.cfg.get("start_idle_enabled", True)
                    and self.route.index == 0 and not self._idled_start and self._looped):
                si = self.cfg.get("start_idle_seconds", [25, 60])
                if isinstance(si, (list, tuple)):
                    lo, hi = float(si[0]), float(si[-1])
                    if self.cfg.get("start_idle_random", False) and bb.humanizer is not None:
                        wait = bb.humanizer.uniform_range([lo, hi])
                    else:
                        wait = (lo + hi) / 2.0
                else:
                    wait = float(si)
                self._idle_until = now + wait
                self._idled_start = True
                return
            if self.route.index == 0:
                self._looped = True
            self._idled_start = False
            self._advance(bb, now, pos)
            return
        nav_wp = waypoint if not cross_z else Vec3(waypoint.x, waypoint.y, pz)
        dist = state.player.pos.distance(nav_wp)
        wp_key = (waypoint.x, waypoint.y, waypoint.z)
        if bb.notes.get("route_wp") != wp_key:
            bb.notes["route_wp"] = wp_key
            dg = self._detour_goal(bb, nav_wp, state.player.pos) if not cross_z else nav_wp
            bb.notes["route_final"] = (dg.x, dg.y, dg.z)
            bb.notes["route_best"] = dist
            bb.notes["route_best_at"] = now
            bb.notes["route_key"] = None
            bb.notes.pop("route_seg", None)
        elif dist < bb.notes.get("route_best", 9999):
            bb.notes["route_best"] = dist
            bb.notes["route_best_at"] = now
        # atasco por progreso: si no se acerca al waypoint en N s, saltarlo
        if now - bb.notes.get("route_best_at", now) > float(self.cfg.get("progress_stuck_secs", 6.0)):
            self._advance(bb, now, pos)
            return
        # atasco por posicion: no camina y la posicion no cambia
        if bb.notes.get("route_pos") != pos:
            bb.notes["route_pos"] = pos
            bb.notes["route_since"] = now
        if not state.is_walking and now - bb.notes.get("route_since", now) > float(self.cfg.get("stuck_secs", 2.0)):
            self._advance(bb, now, pos)
            return
        # objetivo segmentado: se fija un punto a <= max_nav_dist (rango que el
        # cliente sabe pathfindear) y NO se recalcula cada tile, para no
        # reiniciar la navegacion del agente (eso provocaba zigzag). Se recalcula
        # solo al acercarse al segmento actual.
        fx, fy, fz = bb.notes.get("route_final", (waypoint.x, waypoint.y, waypoint.z))
        final = Vec3(fx, fy, fz)
        seg = bb.notes.get("route_seg")
        if seg is None or state.player.pos.distance(Vec3(seg[0], seg[1], seg[2])) <= int(self.cfg.get("seg_recalc_dist", 2)):
            ng = self._segment(state.player.pos, final)
            seg = (ng.x, ng.y, ng.z)
            bb.notes["route_seg"] = seg
        goal = Vec3(seg[0], seg[1], seg[2])
        key = (goal.x, goal.y, goal.z)
        if bb.notes.get("route_key") != key or (
            not state.is_walking
            and now - bb.notes.get("route_sent_at", 0.0) > float(self.cfg.get("resend_secs", 2.5))
        ):
            inp.walk_to(goal)
            bb.notes["route_key"] = key
            bb.notes["route_sent_at"] = now
            bb.mark("route")


class ExploreBehavior(Behavior):
    name = "explore"
    priority = 5

    def __init__(self, cfg: dict, settings: dict, world=None):
        super().__init__(cfg, settings)
        self.world = world
        self._home_done = False
        self._target = None
        self._blacklist: dict = {}
        self._sent_at = 0.0
        self._last_pos = None
        self._pos_since = 0.0
        self._last_save = 0.0
        self._patrol_i = 0

    def evaluate(self, state: GameState, bb: Blackboard) -> bool:
        if bb.paused or not self.cfg.get("enabled", True) or self.world is None:
            return False
        return bool(getattr(state, "connected", False))

    def act(self, state: GameState, bb: Blackboard, inp) -> None:
        wm = self.world
        pos = state.player.pos
        if wm.z is not None and pos.z != wm.z:
            self._target = None
            return
        wm.visit(pos)

        chome = self.cfg.get("home")
        if chome and len(chome) == 3:
            wm.home = (int(chome[0]), int(chome[1]), int(chome[2]))
            self._home_done = True
        elif not self._home_done:
            wm.home = (pos.x, pos.y, pos.z)
            self._home_done = True
        wm.radius = int(self.cfg.get("radius", 60))
        wm.patrol_step = int(self.cfg.get("patrol_step", 4))
        wm.coverage_radius = int(self.cfg.get("coverage_radius", 2))

        now = time.time()
        for key in [k for k, exp in self._blacklist.items() if exp <= now]:
            self._blacklist.pop(key, None)
        if now - self._last_save > float(self.cfg.get("save_interval", 5.0)):
            wm.save()
            self._last_save = now
        if self._last_pos != (pos.x, pos.y):
            self._last_pos = (pos.x, pos.y)
            self._pos_since = now

        if self._target is not None:
            tx, ty = self._target
            if pos.x == tx and pos.y == ty:
                self._target = None
                return
            if not state.is_walking and now - self._pos_since > float(self.cfg.get("stuck_secs", 2.5)):
                self._blacklist[self._target] = now + float(self.cfg.get("blacklist_secs", 30))
                self._target = None
                return
            if not state.is_walking and now - self._sent_at > float(self.cfg.get("resend_secs", 0.6)):
                path = wm.plan_to(pos, tx, ty)
                if path:
                    self._send_path(inp, path, now)
            return

        plan = wm.plan_coverage(pos, set(self._blacklist.keys()))
        if plan is None:
            plan = wm.plan_to_frontier(pos, set(self._blacklist.keys()))
        if plan is None:
            self._patrol(state, inp, pos, now)
            return
        self._target = (plan[-1].x, plan[-1].y)
        self._send_path(inp, plan, now)

    def _send_path(self, inp, plan, now: float) -> None:
        look = max(1, int(self.cfg.get("lookahead", 40)))
        wps = plan[1:1 + look]
        if not wps:
            return
        inp.walk_path(wps, plan[0].z)
        self._sent_at = now

    def _patrol(self, state: GameState, inp, pos, now: float) -> None:
        wm = self.world
        if not self.cfg.get("patrol", True) or not wm.patrol:
            return
        wp = wm.patrol[self._patrol_i % len(wm.patrol)]
        tgt = Vec3(int(wp[0]), int(wp[1]), int(wp[2]))
        if pos.distance(tgt) <= 1:
            self._patrol_i += 1
            self._sent_at = 0.0
            return
        if not state.is_walking and now - self._sent_at > float(self.cfg.get("resend_secs", 0.6)):
            path = wm.plan_to(pos, tgt.x, tgt.y)
            if path:
                self._send_path(inp, path, now)
            else:
                inp.walk_to(tgt)
                self._sent_at = now
        if not state.is_walking and now - self._pos_since > float(self.cfg.get("stuck_secs", 2.5)) * 2:
            self._patrol_i += 1
            self._sent_at = 0.0


class SummonBehavior(Behavior):
    """Mantiene el ownsummon cerca del player, priorizando un tile cuyos 8
    vecinos esten libres (caminables). Ordena al pokemon con 'order'."""

    name = "summon"
    priority = 12

    def __init__(self, cfg: dict, settings: dict, world=None):
        super().__init__(cfg, settings)
        self.world = world

    def _otmm(self):
        wm = self.world
        mm = getattr(wm, "otmm", None) if wm is not None else None
        return mm if (mm is not None and getattr(mm, "ready", False)) else None

    def _neighbors_free(self, mm, x: int, y: int, z: int) -> bool:
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                if not mm.pathable(x + dx, y + dy, z):
                    return False
        return True

    def _best_tile(self, state: GameState, mm) -> Optional[Vec3]:
        return summon_near_tile(state, mm, int(self.cfg.get("radius", 4)))

    def evaluate(self, state: GameState, bb: Blackboard) -> bool:
        if bb.paused or not self.cfg.get("enabled", True):
            return False
        mm = self._otmm()
        if mm is None:
            return False
        pp = getattr(state, "pokemon_pos", None)
        if not pp:
            return False
        if not bb.ready("summon", float(self.cfg.get("interval_secs", 3.0))):
            return False
        pk = Vec3(int(pp[0]), int(pp[1]), int(pp[2]))
        far = pk.distance(state.player.pos) > int(self.cfg.get("max_dist", 2))
        blocked = not self._neighbors_free(mm, pk.x, pk.y, pk.z)
        return far or blocked

    def act(self, state: GameState, bb: Blackboard, inp) -> None:
        mm = self._otmm()
        t = self._best_tile(state, mm) if mm is not None else None
        if t is not None:
            inp.order(t)
        bb.mark("summon")


def build_behaviors(cfg: dict, route: Optional[Route], world=None,
                    llm=None) -> list[Behavior]:
    behaviors_cfg = cfg.get("behaviors", {})
    settings = cfg.get("settings", {})
    behaviors: list[Behavior] = [
        RecoveryBehavior(behaviors_cfg.get("recovery", {}), settings, route, world,
                         routes_file=cfg.get("lua", {}).get("routes_file", "")),
        CrisisBehavior(behaviors_cfg.get("crisis", {}), settings),
        HealingBehavior(behaviors_cfg.get("healing", {}), settings),
        CaptureBehavior(behaviors_cfg.get("capture", {}), settings),
        CombatBehavior(behaviors_cfg.get("combat", {}), settings, world),
        LootBehavior(behaviors_cfg.get("loot", {}), settings, world),
        ReviveBehavior(behaviors_cfg.get("revive", {}), settings,
                       behaviors_cfg.get("combat", {})),
        RouteBehavior(behaviors_cfg.get("route", {}), settings, route, world),
        SummonBehavior(behaviors_cfg.get("summon", {}), settings, world),
        ExploreBehavior(behaviors_cfg.get("explore", {}), settings, world),
    ]
    # Capa de decision con LLM (meta-policy): se construye solo si la seccion
    # `llm` esta habilitada y hay controlador; gobierna combat/route/explore.
    llm_cfg = cfg.get("llm", {}) or {}
    if llm is not None and llm_cfg.get("enabled", False):
        from .llm.behavior import LlmBehavior

        by_name = {b.name: b for b in behaviors}
        governed = {name: by_name[name]
                    for name in llm_cfg.get("governed", []) if name in by_name}
        behaviors.append(LlmBehavior(llm_cfg, settings, governed, llm))
    behaviors.sort(key=lambda b: b.priority, reverse=True)
    return behaviors
