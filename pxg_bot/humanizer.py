from __future__ import annotations

import random
from typing import Optional, Sequence

# Perfiles de variabilidad humana. El perfil define los parametros; los
# "overrides" del config permiten afinar sin tocar el perfil.
HUMANIZER_PROFILES = {
    "light": {
        "tick_jitter_pct": 25, "cooldown_jitter_pct": 20, "threshold_jitter_pct": 8,
        "key_duration_jitter_pct": 35, "micro_idle_chance": 0.02,
        "pause_chance": 0.0003, "pause_seconds": [1, 3],
        "reaction_min": 0.10, "reaction_max": 0.35,
        "detour_chance": 0.04, "look_around_chance": 0.03,
        "skill_shuffle": True, "target_variance": True,
        "session_enabled": True, "play_minutes": [45, 110], "break_minutes": [1, 4],
    },
    "normal": {
        "tick_jitter_pct": 30, "cooldown_jitter_pct": 25, "threshold_jitter_pct": 10,
        "key_duration_jitter_pct": 40, "micro_idle_chance": 0.04,
        "pause_chance": 0.0005, "pause_seconds": [1, 4],
        "reaction_min": 0.12, "reaction_max": 0.45,
        "detour_chance": 0.07, "look_around_chance": 0.05,
        "skill_shuffle": True, "target_variance": True,
        "session_enabled": True, "play_minutes": [30, 90], "break_minutes": [1, 5],
    },
    "aggressive": {
        "tick_jitter_pct": 35, "cooldown_jitter_pct": 30, "threshold_jitter_pct": 12,
        "key_duration_jitter_pct": 45, "micro_idle_chance": 0.06,
        "pause_chance": 0.0012, "pause_seconds": [2, 6],
        "reaction_min": 0.15, "reaction_max": 0.60,
        "detour_chance": 0.12, "look_around_chance": 0.08,
        "skill_shuffle": True, "target_variance": True,
        "session_enabled": True, "play_minutes": [20, 60], "break_minutes": [2, 8],
    },
}

DEFAULT_PROFILE = "normal"


class Humanizer:
    """Variabilidad pseudo-humana: tiempos, micro-desvios, miradas y descansos.

    La idea es variar el comportamiento (reacciones, orden de acciones, caminos)
    en vez de quedarse quieto, que es justo lo que delata a un bot.
    """

    def __init__(self, cfg: Optional[dict] = None):
        cfg = dict(cfg or {})
        profile = cfg.get("profile", DEFAULT_PROFILE)
        self.profile = profile if profile in HUMANIZER_PROFILES else DEFAULT_PROFILE
        merged = dict(HUMANIZER_PROFILES[self.profile])
        overrides = cfg.get("overrides")
        if isinstance(overrides, dict):
            merged.update(overrides)
        # claves explicitas que siempre respetan el config
        for key in ("enabled", "random_seed", "idle_only_pause"):
            if key in cfg:
                merged[key] = cfg[key]
        merged.setdefault("enabled", True)
        merged.setdefault("idle_only_pause", True)
        self.cfg = merged
        self.enabled = bool(merged.get("enabled", True))
        seed = merged.get("random_seed")
        self.rng = random.Random(seed)

    # --- utilidades basicas ---
    def _frac(self, pct) -> float:
        return float(pct) / 100.0

    def jitter(self, value: float, key: str) -> float:
        if not self.enabled:
            return value
        p = self._frac(self.cfg.get(key, 0))
        if p <= 0:
            return value
        return value * self.rng.uniform(1.0 - p, 1.0 + p)

    def jitter_up(self, value: float, key: str) -> float:
        if not self.enabled:
            return value
        p = self._frac(self.cfg.get(key, 0))
        if p <= 0:
            return value
        return value * self.rng.uniform(1.0, 1.0 + p)

    def chance(self, probability: float) -> bool:
        return self.rng.random() < float(probability)

    def choice(self, sequence: Sequence):
        return self.rng.choice(sequence)

    def shuffle(self, items: list) -> list:
        out = list(items)
        self.rng.shuffle(out)
        return out

    def uniform_range(self, pair) -> float:
        low, high = pair
        return self.rng.uniform(float(low), float(high))

    def randint_range(self, pair) -> int:
        low, high = pair
        return self.rng.randint(int(low), int(high))

    # --- variacion conductual ---
    def reaction(self) -> float:
        """Latencia de reaccion humana (segundos)."""
        lo = float(self.cfg.get("reaction_min", 0.12))
        hi = float(self.cfg.get("reaction_max", 0.45))
        if hi < lo:
            lo, hi = hi, lo
        return self.rng.uniform(lo, hi)

    def should_detour(self) -> bool:
        return self.enabled and self.chance(self.cfg.get("detour_chance", 0))

    def should_look_around(self) -> bool:
        return self.enabled and self.chance(self.cfg.get("look_around_chance", 0))

    def pick_weighted(self, items: list, weight: callable, top: int = 3):
        """Elige un item dando mas peso a los primeros (weight menor = mejor)."""
        if not items:
            return None
        ranked = sorted(items, key=weight)
        pool = ranked[: max(1, top)]
        weights = [1.0 / (i + 1) for i in range(len(pool))]
        total = sum(weights)
        r = self.rng.random() * total
        acc = 0.0
        for item, w in zip(pool, weights):
            acc += w
            if r <= acc:
                return item
        return pool[-1]
