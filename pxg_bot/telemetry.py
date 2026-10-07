"""Telemetria de muertes: graba la traza de juego y los episodios previos a morir.

Objetivo (Fase 1): generar los datos para aprender a no morir. No decide nada;
solo observa cada tick, mantiene un bufer circular de las ultimas ventana de
juego y, cuando detecta una muerte, vuelca el episodio a `pxg_deaths.jsonl`.

Senales de muerte (configurables):
- `exp_drop`: la experiencia BAJA  (señal fuerte: morir resta XP).
- `level_drop`: el nivel baja (bajar de nivel = muerte).
- `player_death`: la vida del jugador llega a 0.
- `alert_msg`: aparece en el chat del servidor un mensaje de muerte.
- `pokemon_faint`: un pokemon del equipo se debilita.
- `disconnect`: el cliente se desconecta.

Salidas (junto al state_file, o en `telemetry.dir`):
- `pxg_trace.jsonl`: una fila por muestra (negativos: casi-muertes que no murieron).
- `pxg_deaths.jsonl`: episodios de muerte con la ventana previa.

Sin dependencias; degradable (si algo falla, no bloquea el bot).
"""
from __future__ import annotations

import json
import os
import time
from collections import deque
from typing import Optional

DEFAULT_ALERT_KEYWORDS = [
    "has muerto", "moriste", "fuiste derrotado", "has sido derrotado",
    "derrotado", "perdiste", "experiencia perdida", "reaparecer", "respawn",
]

MAX_MSG = 8


class Telemetry:
    def __init__(self, cfg: dict):
        t = (cfg or {}).get("telemetry", {}) or {}
        self.enabled = bool(t.get("enabled", False))
        self.sample_secs = max(0.05, float(t.get("sample_secs", 0.25)))
        self.window_secs = max(1.0, float(t.get("window_secs", 30)))
        self.trace_on = bool(t.get("trace", True))
        self.trace_max_bytes = int(float(t.get("trace_max_mb", 50)) * 1024 * 1024)
        self.cooldown = float(t.get("death_cooldown_secs", 10))
        self.settle = float(t.get("settle_secs", 1.0))
        self.alert_keywords = [str(k).lower()
                               for k in (t.get("alert_keywords") or DEFAULT_ALERT_KEYWORDS)]
        self.detect = {
            "exp_drop": bool(t.get("exp_drop", True)),
            "level_drop": bool(t.get("level_drop", True)),
            "player_death": bool(t.get("player_death", True)),
            "alert_msg": bool(t.get("alert_msg", True)),
            "pokemon_faint": bool(t.get("pokemon_faint", True)),
            "disconnect": bool(t.get("disconnect", True)),
        }
        self.dir = self._resolve_dir(t, cfg)
        self.trace_path = os.path.join(self.dir, "pxg_trace.jsonl") if self.dir else ""
        self.deaths_path = os.path.join(self.dir, "pxg_deaths.jsonl") if self.dir else ""

        maxlen = max(2, int(self.window_secs / self.sample_secs) + 1)
        self._ring: deque = deque(maxlen=maxlen)
        self._last_sample = 0.0
        self._last_event = 0.0
        self._pending: Optional[dict] = None
        self._prev: dict = {}
        self.deaths = 0
        self.error = ""
        self._trace_bytes = 0
        if self.enabled and self.dir:
            try:
                os.makedirs(self.dir, exist_ok=True)
                self._trace_bytes = (os.path.getsize(self.trace_path)
                                     if os.path.exists(self.trace_path) else 0)
            except OSError as exc:
                self.error = str(exc)
                self.enabled = False

    # --- rutas ---
    @staticmethod
    def _resolve_dir(t: dict, cfg: dict) -> str:
        d = str(t.get("dir") or "").strip()
        if d:
            return d
        lua = (cfg or {}).get("lua", {}) or {}
        base = lua.get("status_file") or lua.get("state_file") or ""
        return os.path.dirname(base) if base else ""

    # --- features ---
    def _features(self, state, chosen, bb) -> dict:
        p = state.player
        pp = getattr(state, "pokemon_pos", None)
        ref = (int(pp[0]), int(pp[1]), int(pp[2])) if pp else (p.pos.x, p.pos.y, p.pos.z)
        enemies = [c for c in state.creatures if getattr(c, "attackable", False)]

        def dist(c):
            return max(abs(c.pos.x - ref[0]), abs(c.pos.y - ref[1]))

        vis = state.visible or {}
        hw = int(vis.get("w", 21)) // 2
        hh = int(vis.get("h", 11)) // 2
        on_screen = [c for c in enemies
                     if abs(c.pos.x - p.pos.x) <= hw and abs(c.pos.y - p.pos.y) <= hh]
        moves = state.moves or []
        ready = [m for m in moves if isinstance(m.get("pct"), (int, float)) and m["pct"] >= 100]
        notes = getattr(bb, "notes", {}) if bb is not None else {}

        party_hp = {str(q.slot): int(q.hp_pct) for q in (state.party or [])}
        return {
            "t": round(time.time(), 3),
            "x": p.pos.x, "y": p.pos.y, "z": p.pos.z,
            "player_hp_pct": int(p.hp_pct),
            "level": int(p.level),
            "exp": int(getattr(p, "exp", 0) or 0),
            "summon_hp": getattr(state, "pokemon_hp", None),
            "active_hp": next((int(q.hp_pct) for q in (state.party or []) if q.active), None),
            "party_hp": party_hp,
            "n_enemies_total": len(enemies),
            "n_enemies_onscreen": len(on_screen),
            "min_dist_enemy_summon": (min((dist(c) for c in enemies), default=-1)
                                      if enemies else -1),
            "n_moves_ready": len(ready),
            "n_aoe_ready": sum(1 for m in ready if m.get("aoe")),
            "revives": self._revives(state),
            "chosen": chosen or "idle",
            "lure_state": notes.get("lure_state"),
            "revive_pending": bool(notes.get("revive_pending")),
            "revive_urgent": bool(notes.get("revive_urgent")),
            "is_walking": bool(getattr(state, "is_walking", False)),
            "nav_result": str(getattr(state, "nav_result", "") or ""),
            "spawn_blocked": bool(getattr(state, "spawn_blocked", False)),
            "connected": bool(getattr(state, "connected", False)),
            "attacking": str(getattr(state, "attacking_name", "") or ""),
            "fight_mode": getattr(state, "fight_mode", None),
            "server_msgs": [str(m) for m in (getattr(state, "server_msgs", []) or [])][-MAX_MSG:],
        }

    @staticmethod
    def _revives(state):
        bc = getattr(state, "bag_counts", None) or {}
        for key in ("2269", 2269):
            if key in bc:
                try:
                    return int(bc[key])
                except (TypeError, ValueError):
                    return None
        return None

    # --- deteccion ---
    def _detect(self, feats: dict) -> list:
        prev = self._prev
        sig: list[str] = []

        def on(name: str) -> bool:
            return self.detect.get(name, False)

        lvl, exp, hp = feats.get("level"), feats.get("exp"), feats.get("player_hp_pct")
        if on("exp_drop") and prev.get("exp") and exp is not None and exp < prev["exp"]:
            sig.append("exp_drop")
        if on("level_drop") and prev.get("level") and lvl is not None and lvl < prev["level"]:
            sig.append("level_drop")
        if on("player_death") and prev.get("player_hp_pct", 0) > 0 and (hp or 0) <= 0:
            sig.append("player_death")
        if on("disconnect") and prev.get("connected") is True and feats.get("connected") is False:
            sig.append("disconnect")
        if on("pokemon_faint"):
            old = prev.get("party_hp") or {}
            for slot, chp in (feats.get("party_hp") or {}).items():
                ohp = old.get(slot)
                if ohp is not None and ohp > 0 and chp <= 0:
                    sig.append("pokemon_faint")
                    break
        if on("alert_msg") and self._alert_hit(feats.get("server_msgs")):
            sig.append("alert_msg")

        prev.update({
            "level": lvl, "exp": exp, "player_hp_pct": hp,
            "connected": feats.get("connected"),
            "party_hp": feats.get("party_hp") or {},
        })
        return sig

    def _alert_hit(self, msgs) -> bool:
        msgs = msgs or []
        seen = set(self._prev.get("server_msgs") or [])
        hit = False
        for m in msgs:
            if m in seen:
                continue
            low = str(m).lower()
            if any(k in low for k in self.alert_keywords):
                hit = True
        self._prev["server_msgs"] = list(msgs)
        return hit

    # --- ciclo ---
    def observe(self, state, chosen=None, bb=None) -> None:
        if not self.enabled or not self.dir:
            return
        now = time.time()
        if self._last_sample and (now - self._last_sample) < self.sample_secs:
            return
        self._last_sample = now
        try:
            feats = self._features(state, chosen, bb)
            self._ring.append(feats)
            if self.trace_on:
                self._write_trace(feats)
            signals = self._detect(feats)
            if signals:
                self._on_signal(now, signals, feats)
            self._maybe_flush(now)
        except Exception as exc:  # nunca tumbar el bot por telemetria
            self.error = f"{type(exc).__name__}: {exc}"

    def _on_signal(self, now: float, signals: list, feats: dict) -> None:
        if self._pending is not None:
            self._pending["signals"].update(signals)
            return
        if now - self._last_event < self.cooldown:
            return  # aun en el cooldown de una muerte reciente
        self._pending = {
            "signals": set(signals),
            "start": now,
            "ts": now,
            "feats": feats,
            "window": list(self._ring),
        }

    def _maybe_flush(self, now: float) -> None:
        pend = self._pending
        if pend is None:
            return
        if now - pend["start"] < self.settle:
            return
        self._pending = None
        self._last_event = now
        self.deaths += 1
        rec = {
            "ts": pend["ts"],
            "index": self.deaths,
            "signals": sorted(pend["signals"]),
            "level": pend["feats"].get("level"),
            "exp": pend["feats"].get("exp"),
            "player_hp_pct": pend["feats"].get("player_hp_pct"),
            "pos": [pend["feats"].get("x"), pend["feats"].get("y"), pend["feats"].get("z")],
            "chosen": pend["feats"].get("chosen"),
            "server_msgs": pend["feats"].get("server_msgs"),
            "window": pend["window"],
        }
        self._append(self.deaths_path, rec)

    # --- escritura ---
    def _write_trace(self, feats: dict) -> None:
        line = json.dumps(feats, ensure_ascii=False)
        try:
            self._trace_bytes += len(line.encode("utf-8")) + 1
            if self.trace_on and self.trace_max_bytes and self._trace_bytes > self.trace_max_bytes:
                self._rotate_trace()
            with open(self.trace_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as exc:
            self.error = str(exc)

    def _rotate_trace(self) -> None:
        try:
            if os.path.exists(self.trace_path):
                os.replace(self.trace_path, self.trace_path + f".{int(time.time())}")
            self._trace_bytes = 0
        except OSError:
            self._trace_bytes = 0

    def _append(self, path: str, rec: dict) -> None:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as exc:
            self.error = str(exc)

    # --- cierre / estado ---
    def close(self) -> None:
        # si quedo un episodio sin volcar, se escribe
        if self._pending is not None:
            self._maybe_flush(self._pending["start"] + self.settle + 1)

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "dir": self.dir,
            "deaths": self.deaths,
            "error": self.error,
        }
