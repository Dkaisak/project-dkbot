from __future__ import annotations

import random
from typing import Optional, Sequence


class Humanizer:
    """Introduce variabilidad pseudo-humana en tiempos y decisiones."""

    def __init__(self, cfg: Optional[dict] = None):
        self.cfg = cfg or {}
        self.enabled = bool(self.cfg.get("enabled", True))
        seed = self.cfg.get("random_seed")
        self.rng = random.Random(seed)

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

    def uniform_range(self, pair) -> float:
        low, high = pair
        return self.rng.uniform(float(low), float(high))

    def randint_range(self, pair) -> int:
        low, high = pair
        return self.rng.randint(int(low), int(high))
