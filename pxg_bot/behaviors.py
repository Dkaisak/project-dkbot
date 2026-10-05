from __future__ import annotations

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
    humanizer: object = None
    ignore: object = None

    def ready(self, name: str, cooldown: float) -> bool:
        if self.humanizer is not None:
            cooldown = self.humanizer.jitter_up(cooldown, "cooldown_jitter_pct")
        return time.time() - self.action_times.get(name, 0.0) >= cooldown

    def mark(self, name: str) -> None:
        self.action_times[name] = time.time()

    def update_corpses(self, state: GameState) -> None:
        # deteccion de cuerpos desacoplada de la prioridad de los behaviors: el
        # agente reporta cada cuerpo derrotado durante un solo tick, asi que hay
        # que cosecharlo siempre, aunque en ese tick actue revive/loot/etc.
        seen = self.notes.setdefault("defeated_seen", set())
        looted = self.notes.setdefault("looted_ids", set())
        pending = self.notes.setdefault("corpses_pending", [])
        shiny_pos = self.notes.setdefault("shiny_pos", {})
        for c in state.creatures:
            if getattr(c, "shiny", False) and c.kind == CreatureKind.MONSTER:
                shiny_pos[(c.name, c.pos.x, c.pos.y)] = c.pos.z
        for d in state.defeated:
            did = str(d.get("id", ""))
            if not did or did in seen:
                continue
            seen.add(did)
            if did in looted:
                continue
            dname = d.get("name", "?")
            dx, dy = int(d.get("x", 0)), int(d.get("y", 0))
            was_shiny = any(
                name == dname and abs(x - dx) <= 1 and abs(y - dy) <= 1
                for (name, x, y) in shiny_pos
            )
            pending.append({
                "id": did, "name": dname, "x": dx, "y": dy, "z": int(d.get("z", 0)),
            })
            # meta de cada cuerpo: se captura (ball) tras lotear si es shiny o catch_all
            self.notes.setdefault("corpse_meta", {})[did] = {
                "name": dname, "x": dx, "y": dy, "z": int(d.get("z", 0)),
                "shiny": was_shiny,
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

    def _promote(self, bb: Blackboard) -> None:
        # los cuerpos se lottean primero; al quedar looteados pasan a captura si
        # son shiny (obligatorio) o si catch_all esta activo.
        meta = bb.notes.get("corpse_meta")
        if not meta:
            return
        looted = bb.notes.get("looted_ids", set())
        pending = self._pending(bb)
        catch_all = bool(self.cfg.get("catch_all", False))
        for cid in list(meta.keys()):
            if cid in looted:
                m = meta.pop(cid)
                if m.get("shiny") or catch_all:
                    pending.append({"id": cid, "n": 0, **m})

    def _valid(self, t: dict) -> bool:
        if t.get("shiny"):
            return True
        return bool(self.cfg.get("enabled", True)) and bool(self.cfg.get("catch_all", False))

    def evaluate(self, state: GameState, bb: Blackboard) -> bool:
        if bb.paused:
            return False
        self._promote(bb)
        pending = self._pending(bb)
        pending[:] = [t for t in pending if self._valid(t)]
        if not pending:
            return False
        # no capturar si hay un enemigo vivo pegado (pelear primero)
        for c in state.creatures:
            if is_enemy(c) and state.player.pos.distance(c.pos) <= 3:
                return False
        return True

    def act(self, state: GameState, bb: Blackboard, inp) -> None:
        pending = self._pending(bb)
        if not pending:
            return
        if getattr(state, "captured", False):
            pending.pop(0)
            return
        t = pending[0]
        pos = Vec3(int(t["x"]), int(t["y"]), int(t["z"]))
        if state.player.pos.distance(pos) > int(self.cfg.get("range", 1)):
            if bb.ready("cap_goto", 0.35):
                inp.walk_to(pos)
                bb.mark("cap_goto")
            return
        if bb.ready("ball", float(self.cfg.get("interval", 1.0))):
            inp.hotkey(self.cfg.get("ball_key", "F4"))
            bb.mark("ball")
            t["n"] = int(t.get("n", 0)) + 1
            if t["n"] >= int(self.cfg.get("max_throws", 10)):
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
        if self._spawn_blocked(state, bb):
            tgt = bb.notes.get("last_target")
            if tgt:
                self._begin_spawn_backoff(bb, tgt)
                return bool(bb.notes.get("spawn_backoff"))
        # lure con pokestop: caminar la ruta hasta juntar N visibles; parar (R),
        # esperar a que se acerquen y lanzar AoE; luego reanudar con clic central.
        if self.cfg.get("lure_aoe", False) and self._has_aoe(state):
            lstate = bb.notes.get("lure_state")
            vis = self._visible_enemies(state)
            vmin = int(self.cfg.get("lure_visible_min", 5))
            arange = int(self.cfg.get("lure_attack_range", 2))
            amin = int(self.cfg.get("lure_attack_min", 2))
            if lstate == "fight":
                if not any(is_enemy(c) for c in state.creatures):
                    bb.notes["lure_state"] = "resume"
                return True
            if lstate == "wait":
                # panico: vida baja -> atacar ya, sin esperar al minimo
                if self._panic(state) and len(vis) >= 1:
                    bb.notes["lure_state"] = "fight"
                    bb.notes["lure_panic"] = True
                    return True
                if len(vis) < vmin:
                    bb.notes["lure_state"] = "resume"
                    return True
                lnow = time.time()
                ref = self._ref_pos(state)
                close = [c for c in vis if ref.distance(c.pos) <= arange]
                all_close = len(vis) > 0 and len(close) == len(vis)
                if all_close or (lnow - bb.notes.get("lure_start", lnow)) > float(self.cfg.get("lure_wait_timeout", 20)):
                    bb.notes["lure_state"] = "fight"
                return True
            if lstate == "resume":
                return True
            # panico: vida baja -> lanzar skills ya, aunque no haya el minimo
            # de pokemones para iniciar el combate
            if self._panic(state) and len(vis) >= 1:
                bb.notes["lure_state"] = "fight"
                bb.notes["lure_panic"] = True
                return True
            # fase A: si hay >= vmin visibles -> pokestop y esperar
            if len(vis) >= vmin:
                bb.notes["lure_state"] = "wait"
                bb.notes["lure_start"] = time.time()
                bb.notes["lure_pokestop"] = True
                return True
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
            bb.notes.pop("lure_start", None)
            bb.notes.pop("lure_fighting", None)
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
            engage = int(self.cfg.get("attack_range", 1)) + 1
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

    def _ref_pos(self, state: GameState) -> Vec3:
        # referencia para medir distancias: el pokemon propio si se conoce,
        # si no el jugador.
        pp = getattr(state, "pokemon_pos", None)
        if pp:
            return Vec3(int(pp[0]), int(pp[1]), int(pp[2]))
        return state.player.pos

    def _all_enemies_close(self, state: GameState, rng: int) -> bool:
        """True solo si TODOS los enemigos visibles estan a <= rng del pokemon."""
        vis = self._visible_enemies(state)
        if not vis:
            return False
        ref = self._ref_pos(state)
        return all(ref.distance(c.pos) <= rng for c in vis)

    def _panic(self, state: GameState) -> bool:
        # vida baja de NUESTRO pokemon (ownsummon): lanzar todo sin esperar
        hp = getattr(state, "pokemon_hp", None)
        return hp is not None and hp <= int(self.cfg.get("panic_hp", 20))

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

    def _aoe_fight(self, state: GameState, bb: Blackboard, inp, enemies) -> None:
        """Fase de pelea tras el lure: lanza AoE (fallback a skills de dano). Sin moverse."""
        s = self.cfg
        now = time.time()
        if not enemies:
            return
        threshold = float(s.get("ready_pct", 100))

        def is_ready(m):
            return isinstance(m.get("pct"), (int, float)) and m["pct"] >= threshold

        aoe_moves = [m for m in state.moves if m.get("aoe") and is_ready(m)]
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

        aoe_moves.sort(key=rank)
        aoe_ready = [m["key"] for m in aoe_moves]
        dmg_ready = [m["key"] for m in state.moves
                     if not m.get("aoe") and is_ready(m) and "damage" in str(m.get("effect", ""))]
        panic = self._panic(state)
        if panic:
            # vida baja de nuestro pokemon: lanzar TODAS las skills listas
            pool = aoe_ready + dmg_ready
        else:
            pool = aoe_ready or dmg_ready
        if not pool:
            return
        # solo lanzar ataques cuando TODOS los enemigos visibles esten a
        # <= attack_range (2) del pokemon; en panico se ataca igual.
        arange = int(s.get("attack_range", 2))
        if not panic and not self._all_enemies_close(state, arange):
            return
        close = self._visible_enemies(state)
        # fijar objetivo (sin acercarse)
        if state.attacking_name == "":
            ref = self._ref_pos(state)
            target = min(close, key=lambda c: ref.distance(c.pos))
            if now - bb.notes.get("tlast", 0.0) > float(s.get("retarget_interval", 0.25)):
                inp.click_tile(target.pos, {"player": state.player})
                bb.notes["tlast"] = now
            return
        if bb.ready("attack", float(s.get("cooldown", 0.3))):
            # en orden: primero la AoE prioritaria (Air Vortex), luego el resto
            inp.press(pool[0])
            bb.notes["skill_sent"] = pool[0]
            bb.mark("attack")

    def act(self, state: GameState, bb: Blackboard, inp) -> None:
        if bb.notes.get("spawn_backoff"):
            self._run_spawn_backoff(state, bb, inp)
            return
        lstate = bb.notes.get("lure_state")
        if lstate == "wait":
            if bb.notes.pop("lure_pokestop", False):
                method = str(self.cfg.get("pokestop_method", "func"))
                if method == "hotkey":
                    inp.hotkey(str(self.cfg.get("pokestop_key", "R")))
                elif method == "talk":
                    inp.pokestop("talk", str(self.cfg.get("pokestop_talk", "!pokestop")))
                else:
                    inp.pokestop("func")
                bb.notes["pokestop_n"] = int(bb.notes.get("pokestop_n", 0)) + 1
                inp.stop()        # cancela la navegacion de la ruta
            return  # esperar sin atacar
        if lstate == "fight":
            enemies = [c for c in state.creatures if is_enemy(c)]
            self._aoe_fight(state, bb, inp, enemies)
            return
        if lstate == "resume":
            self._middle_click_empty(state, bb, inp)
            bb.notes.pop("lure_state", None)
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
        now = time.time()
        pos = (state.player.pos.x, state.player.pos.y)
        if bb.notes.get("engage_pos") != pos:
            bb.notes["engage_pos"] = pos
            bb.notes["engage_since"] = now
        # soltar objetivo que no se puede atacar (evita quedar parado sin fin)
        if state.attacking_name != name and now - bb.notes.get("engage_since", now) > float(s.get("target_drop_secs", 3.0)):
            bb.notes.pop("tname", None)
            bb.notes.setdefault("target_blacklist", {})[
                (name, target.pos.x, target.pos.y)
            ] = now + float(s.get("target_blacklist_secs", 10.0))
            return
        if state.player.pos.distance(target.pos) > int(s.get("attack_range", 1)):
            direction = state.player.pos.direction_to(target.pos)
            if direction >= 0 and not state.is_walking and bb.ready("approach", float(s.get("approach_cooldown", 0.15))):
                inp.move(direction)
                bb.mark("approach")
            return

        now = time.time()
        # 1) fijar objetivo con clic si hace falta
        if state.attacking_name != name:
            if bb.notes.get("tname_prev") != name:
                bb.notes["tname_prev"] = name
                bb.notes["tlast"] = 0.0
            if now - bb.notes.get("tlast", 0.0) > float(s.get("retarget_interval", 0.25)):
                inp.click_tile(target.pos, {"player": state.player})
                bb.notes["tlast"] = now
                bb.notes["attack_attempt"] = now
                bb.notes["dmg_key"] = None
            return

        # 2) objetivo fijado: si no pierde vida atacando, es un spawn bloqueado
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
        # Solo cuando TODOS los enemigos visibles esten a <= attack_range.
        if not self._all_enemies_close(state, int(s.get("attack_range", 2))):
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
                bb.notes["skill_sent"] = key
                bb.notes["attack_attempt"] = now
                bb.mark("attack")


class LootBehavior(Behavior):
    name = "loot"
    priority = 88

    def _pending(self, bb: Blackboard) -> list:
        return bb.notes.setdefault("corpses_pending", [])

    def _mark_looted(self, bb: Blackboard, cid: str) -> None:
        bb.notes.setdefault("looted_ids", set()).add(cid)

    def _coverage(self, tx: int, ty: int, pending: list) -> int:
        return sum(1 for c in pending if max(abs(tx - c["x"]), abs(ty - c["y"])) <= 1)

    def _best_coverage(self, pending: list) -> int:
        if not pending:
            return 0
        xs = [c["x"] for c in pending]
        ys = [c["y"] for c in pending]
        best = 0
        for x in range(min(xs) - 1, max(xs) + 2):
            for y in range(min(ys) - 1, max(ys) + 2):
                cov = self._coverage(x, y, pending)
                if cov > best:
                    best = cov
        return best

    def _best_tile(self, pending: list, pos: Vec3):
        # tile que cubre mas cuerpos (Chebyshev 1); a igual cobertura, el mas
        # cercano. Es el mismo criterio que bestCoverTile del agente.
        if not pending:
            return None
        xs = [c["x"] for c in pending]
        ys = [c["y"] for c in pending]
        best = None
        best_cov = 0
        best_d = 1 << 30
        for x in range(min(xs) - 1, max(xs) + 2):
            for y in range(min(ys) - 1, max(ys) + 2):
                cov = self._coverage(x, y, pending)
                if cov <= 0:
                    continue
                d = max(abs(pos.x - x), abs(pos.y - y))
                if cov > best_cov or (cov == best_cov and d < best_d):
                    best_cov = cov
                    best_d = d
                    best = (x, y)
        return best

    def _finalize(self, bb: Blackboard) -> None:
        # un unico collectLoot recoge TODOS los cuerpos adyacentes; tras el
        # tiempo de gracia marcamos como loteados los que estaban adyacentes.
        rec = bb.notes.get("collect_rec")
        if not rec:
            return
        if time.time() - rec["t"] < float(self.cfg.get("collect_grace", 0.8)):
            return
        looted = bb.notes.setdefault("looted_ids", set())
        looted.update(rec["ids"])
        pending = self._pending(bb)
        pending[:] = [c for c in pending if c["id"] not in looted]
        bb.notes.pop("collect_rec", None)
        if not pending:
            bb.notes["loot_phase_start"] = 0.0
            bb.notes["loot_phase_elapsed"] = 0.0

    def _enemy_near(self, state: GameState) -> bool:
        # no caminar hacia cuerpos si hay un enemigo pegado (seguridad). Los
        # enemigos a media distancia no impiden lootear: si no, en un spawn
        # nunca se lotea nada.
        rng = int(self.cfg.get("enemy_range", 3))
        return nearest(state, is_enemy, max_range=rng) is not None

    def evaluate(self, state: GameState, bb: Blackboard) -> bool:
        if bb.paused or not self.cfg.get("enabled", True):
            return False
        self._finalize(bb)
        pending = self._pending(bb)
        if not pending:
            bb.notes["loot_phase_start"] = 0.0
            bb.notes["loot_phase_elapsed"] = 0.0
            bb.notes.pop("loot_nav_fp", None)
            return False
        # timer de la fase de loot: cuenta desde que empieza a lootear de verdad.
        # Al expirar, se abandona lo que quede y el bot retoma la ruta.
        phase = bb.notes.get("loot_phase_start", 0.0)
        if phase != 0.0:
            elapsed = time.time() - phase
            bb.notes["loot_phase_elapsed"] = elapsed
            if elapsed > float(self.cfg.get("phase_secs", 5.0)):
                for c in pending:
                    self._mark_looted(bb, c["id"])
                pending.clear()
                bb.notes["loot_phase_start"] = 0.0
                bb.notes["loot_phase_elapsed"] = 0.0
                bb.notes.pop("loot_nav_fp", None)
                return False
        reach = int(self.cfg.get("reach", 20))
        px, py, pz = state.player.pos.x, state.player.pos.y, state.player.pos.z
        pending[:] = [c for c in pending
                      if c["z"] == pz and max(abs(px - c["x"]), abs(py - c["y"])) <= reach]
        if not pending:
            bb.notes["loot_phase_start"] = 0.0
            bb.notes["loot_phase_elapsed"] = 0.0
            return False
        # un cuerpo adyacente se lootea ya (pulsacion instantanea), aunque haya
        # enemigos: no conviene perder el botin que tenemos al lado.
        adjacent = self._coverage(px, py, pending) >= 1
        # para caminar hacia cuerpos lejanos, no adelantar al combate
        if not adjacent and self._enemy_near(state):
            return False
        # arranca el timer al empezar a lootear (adyacente o caminando)
        if bb.notes.get("loot_phase_start", 0.0) == 0.0:
            bb.notes["loot_phase_start"] = time.time()
            bb.notes["loot_phase_elapsed"] = 0.0
        return True

    def act(self, state: GameState, bb: Blackboard, inp) -> None:
        pending = self._pending(bb)
        if not pending:
            return
        px, py = state.player.pos.x, state.player.pos.y
        pz = pending[0]["z"]
        cur = self._coverage(px, py, pending)
        best = self._best_coverage(pending)
        enemy_near = self._enemy_near(state)
        bb.notes["loot_cur"] = cur
        bb.notes["loot_best"] = best
        bb.notes["loot_bopt"] = self._best_tile(pending, state.player.pos)

        # temporizador de atasco: se reinicia al movernos o cambiar el grupo
        sig = (best, tuple(sorted(c["id"] for c in pending)))
        if bb.notes.get("loot_sig") != sig or bb.notes.get("loot_pos") != (px, py):
            bb.notes["loot_sig"] = sig
            bb.notes["loot_pos"] = (px, py)
            bb.notes["loot_since"] = time.time()
        stuck = time.time() - bb.notes.get("loot_since", time.time())

        # lootear si hay algo adyacente (aunque haya enemigos) o en cobertura maxima
        if cur >= 1 and (enemy_near or cur >= best or stuck > float(self.cfg.get("stuck_secs", 2.5))):
            if bb.ready("collect", float(self.cfg.get("collect_interval", 0.8))):
                inp.loot()
                bb.mark("collect")
                ids = [c["id"] for c in pending
                       if max(abs(px - c["x"]), abs(py - c["y"])) <= 1]
                bb.notes["collect_rec"] = {"t": time.time(), "ids": ids}
            return

        # con enemigos cerca no caminamos hacia cuerpos lejanos
        if enemy_near:
            return

        bodies = [[c["x"], c["y"]] for c in pending]
        # enviar el destino una sola vez (el agente lo recorre solo); reenviar
        # solo si cambia el conjunto de cuerpos o como red de seguridad.
        fp = tuple(sorted((c["x"], c["y"]) for c in pending))
        since = time.time() - bb.notes.get("loot_nav_t", 0.0)
        if fp != bb.notes.get("loot_nav_fp") or since > float(self.cfg.get("resend_secs", 2.5)):
            if len(bodies) == 1:
                inp.stand_near(Vec3(bodies[0][0], bodies[0][1], pz))
            else:
                inp.stand_near_many(bodies, pz)
            bb.notes["loot_nav_fp"] = fp
            bb.notes["loot_nav_t"] = time.time()


class ReviveBehavior(Behavior):
    name = "revive"
    priority = 80

    def _slot(self) -> int:
        return int(self.cfg.get("slot", 3))

    def _slot_hp(self, state: GameState):
        for p in state.party:
            if p.slot == self._slot():
                return p.hp_pct
        return None

    def _enemies_on_screen(self, state: GameState) -> bool:
        vis = state.visible or {}
        hw = int(vis.get("w", 21)) // 2
        hh = int(vis.get("h", 11)) // 2
        px, py = state.player.pos.x, state.player.pos.y
        return any(is_enemy(c) and abs(c.pos.x - px) <= hw and abs(c.pos.y - py) <= hh
                   for c in state.creatures)

    def _skills_used(self, state: GameState) -> bool:
        # el bot ya uso al menos una skill AoE (esta en cooldown)
        return any(isinstance(m.get("pct"), (int, float)) and m["pct"] < 100
                   for m in (state.moves or []) if m.get("aoe"))

    def _stun_used(self, state: GameState) -> bool:
        # se uso una skill de stun (esta en cooldown). Con esto se puede revivir
        # aunque haya pokemones en pantalla.
        if not self.cfg.get("on_stun", True):
            return False
        for m in (state.moves or []):
            if "stun" in str(m.get("effect", "")).lower():
                pct = m.get("pct")
                if isinstance(pct, (int, float)) and pct < 100:
                    return True
        return False

    def _need(self, state: GameState) -> bool:
        # 2) ya se uso alguna skill AoE, o
        # 3) nuestro propio pokemon debilitado
        hp = self._slot_hp(state)
        if hp is not None and hp <= 0:
            return True
        return self._skills_used(state)

    def evaluate(self, state: GameState, bb: Blackboard) -> bool:
        if bb.paused or not self.cfg.get("enabled", True):
            return False
        need = self._need(state)
        # si se uso un stun, se puede revivir aunque haya pokemones en pantalla
        stun = self._stun_used(state)
        # 1) revivir es obligatorio antes de retomar la ruta: se marca pendiente
        # mientras haga falta, y la ruta no continua hasta revivir.
        if need or stun:
            bb.notes["revive_pending"] = True
        else:
            bb.notes.pop("revive_pending", None)
        rec = bb.notes.get("revive_rec")
        if rec:
            if self._enemies_on_screen(state) and not stun:
                bb.notes.pop("revive_rec", None)
                return False
            return True
        if not need and not stun:
            return False
        if self._enemies_on_screen(state) and not stun:
            return False
        # intervalo minimo entre revives (evita revivir en bucle)
        if time.time() - bb.notes.get("revive_done", 0.0) < float(self.cfg.get("min_interval_secs", 8.0)):
            return False
        return True

    def act(self, state: GameState, bb: Blackboard, inp) -> None:
        now = time.time()
        slot = self._slot()
        cd = float(self.cfg.get("click_delay", 0.3))
        vd = float(self.cfg.get("verify_delay", 1.5))
        rec = bb.notes.get("revive_rec")
        if rec is None:
            bb.notes["revive_rec"] = {"slot": slot, "step": "recall", "t": now}
            return
        step = rec["step"]
        if step == "recall":
            # el pokemon debe estar guardado: si hay activo, retirarlo
            if state.active_pokemon_name:
                if now - rec["t"] >= cd:
                    inp.click_slot(slot)
                    rec["step"] = "recall_wait"
                    rec["t"] = now
            else:
                rec["step"] = "revive"
                rec["t"] = now
            return
        if step == "recall_wait":
            if not state.active_pokemon_name or now - rec["t"] > 2.0:
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
            bb.notes["revive_done"] = now
            bb.notes.pop("revive_rec", None)


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

    def evaluate(self, state: GameState, bb: Blackboard) -> bool:
        if bb.paused or not self.cfg.get("enabled", True) or self.route is None:
            return False
        # revivir es obligatorio antes de retomar la ruta
        if bb.notes.get("revive_pending"):
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
        """Punto de navegacion dentro del rango cargado hacia el waypoint final."""
        maxd = int(self.cfg.get("max_nav_dist", 8))
        if pos.distance(final) <= maxd:
            return final
        dx = (final.x > pos.x) - (final.x < pos.x)
        dy = (final.y > pos.y) - (final.y < pos.y)
        gx, gy = pos.x + dx * maxd, pos.y + dy * maxd
        otmm = getattr(self.world, "otmm", None) if self.world else None
        if otmm is not None and getattr(otmm, "ready", False):
            for ox, oy in [(0, 0), (dx, 0), (0, dy), (dx, dy), (-dx, 0), (0, -dy)]:
                if otmm.pathable(gx + ox, gy + oy, final.z):
                    return Vec3(gx + ox, gy + oy, final.z)
        return Vec3(gx, gy, final.z)

    def act(self, state: GameState, bb: Blackboard, inp) -> None:
        now = time.time()
        # temporizador en idle al llegar al punto de inicio del recorrido
        if now < self._idle_until:
            return
        waypoint = self.route.current
        arrive = int(self.cfg.get("arrive_distance", 1))
        pos = (state.player.pos.x, state.player.pos.y)
        if state.player.pos.distance(waypoint) <= arrive:
            # al llegar al inicio del recorrido (waypoint 0) DESPUES del primer
            # loop, esperar 25-60 s (no en el arranque del bot)
            if self.route.index == 0 and not self._idled_start and self._looped:
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
            self.route.advance()
            bb.notes.pop("route_key", None)
            bb.notes.pop("route_wp", None)
            bb.notes.pop("route_best", None)
            bb.notes["route_sent_at"] = 0.0
            bb.notes["route_pos"] = pos
            bb.notes["route_since"] = now
            return
        dist = state.player.pos.distance(waypoint)
        wp_key = (waypoint.x, waypoint.y, waypoint.z)
        if bb.notes.get("route_wp") != wp_key:
            bb.notes["route_wp"] = wp_key
            dg = self._detour_goal(bb, waypoint, state.player.pos)
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
            self.route.advance()
            bb.notes.pop("route_key", None)
            bb.notes.pop("route_wp", None)
            bb.notes.pop("route_best", None)
            bb.notes["route_sent_at"] = 0.0
            bb.notes["route_since"] = now
            return
        # atasco por posicion: no camina y la posicion no cambia
        if bb.notes.get("route_pos") != pos:
            bb.notes["route_pos"] = pos
            bb.notes["route_since"] = now
        if not state.is_walking and now - bb.notes.get("route_since", now) > float(self.cfg.get("stuck_secs", 2.0)):
            self.route.advance()
            bb.notes.pop("route_key", None)
            bb.notes.pop("route_wp", None)
            bb.notes.pop("route_best", None)
            bb.notes["route_sent_at"] = 0.0
            bb.notes["route_since"] = now
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


def build_behaviors(cfg: dict, route: Optional[Route], world=None) -> list[Behavior]:
    behaviors_cfg = cfg.get("behaviors", {})
    settings = cfg.get("settings", {})
    behaviors: list[Behavior] = [
        CrisisBehavior(behaviors_cfg.get("crisis", {}), settings),
        HealingBehavior(behaviors_cfg.get("healing", {}), settings),
        CaptureBehavior(behaviors_cfg.get("capture", {}), settings),
        CombatBehavior(behaviors_cfg.get("combat", {}), settings, world),
        LootBehavior(behaviors_cfg.get("loot", {}), settings),
        ReviveBehavior(behaviors_cfg.get("revive", {}), settings),
        RouteBehavior(behaviors_cfg.get("route", {}), settings, route, world),
        ExploreBehavior(behaviors_cfg.get("explore", {}), settings, world),
    ]
    behaviors.sort(key=lambda b: b.priority, reverse=True)
    return behaviors
