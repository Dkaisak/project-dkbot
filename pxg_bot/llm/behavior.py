"""Behavior que aplica la meta-policy del LLM.

No ejecuta primitivas: delega en el behavior gobernado que el LLM haya elegido
(combat/route/explore), solo si ese behavior es viable en el estado actual. Si
no hay decision fresca, devuelve False y el bot sigue con la prioridad clasica.

Se situa en una prioridad por encima de los behaviors gobernados (71 > combat 70)
y por debajo de los deterministicos (revive 80, loot 88, capture 85, crisis 100),
que conservan su turno intacto.
"""
from __future__ import annotations

from typing import Optional

from ..behaviors import Behavior


class LlmBehavior(Behavior):
    name = "llm"

    def __init__(self, cfg: dict, settings: dict, governed: dict, controller):
        super().__init__(cfg, settings)
        self.priority = int(cfg.get("priority", 71))
        self.governed = governed or {}
        self.controller = controller
        self._choice: Optional[str] = None

    def evaluate(self, state, bb) -> bool:
        self._choice = None
        if bb.paused or not self.cfg.get("enabled", True):
            return False
        if self.controller is None:
            return False
        decision = self.controller.latest()
        if not decision:
            return False
        name = str(decision.get("behavior", ""))
        behavior = self.governed.get(name)
        if behavior is None or not behavior.cfg.get("enabled", True):
            return False
        # Solo se elige entre opciones viables: si el behavior elegido no aplica
        # en este estado, se cae a la politica clasica.
        if not behavior.evaluate(state, bb):
            return False
        self._choice = name
        bb.notes["llm_choice"] = name
        bb.notes["llm_rationale"] = str(decision.get("rationale", "") or "")
        return True

    def act(self, state, bb, inp) -> None:
        name = self._choice or bb.notes.get("llm_choice")
        behavior = self.governed.get(str(name)) if name else None
        if behavior is None:
            return
        # El behavior gobernado ya paso su evaluate() en LlmBehavior.evaluate;
        # aqui solo se ejecuta su accion con el estado del blackboard intacto.
        behavior.act(state, bb, inp)
