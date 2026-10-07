"""Capa de decision con LLM (meta-policy) para el bot.

El LLM NO ejecuta primitivas ni entra en el bucle rapido (0.05 s): corre en un
hilo aparte a su propia cadencia y solo elige **cual** de los behaviors
gobernados (combat/route/explore) se ejecuta. El resto de behaviors
(crisis/revive/loot/capture/summon) siguen siendo 100% deterministas.

Si el LLM esta deshabilitado, sin API key, tarda o falla, el bot vuelve solo a
la politica de prioridades clasica (degradacion segura).
"""
from __future__ import annotations

from .config import DEFAULT_LLM, normalize
from .controller import LlmController

__all__ = ["DEFAULT_LLM", "normalize", "LlmController", "build_controller"]


def build_controller(llm_cfg: dict, cfg: dict) -> LlmController:
    """Crea el controlador del LLM a partir de `cfg["llm"]`."""
    governed = normalize(llm_cfg).get("governed", [])
    return LlmController(llm_cfg, governed=governed)
