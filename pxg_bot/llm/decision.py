"""Esquema de la decision que devuelve el LLM (salida estructurada)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Behaviors gobernables. El Literal fija el contrato del structured output.
GovernedBehavior = Literal["combat", "route", "explore"]


class Decision(BaseModel):
    """Eleccion de alto nivel del LLM para el siguiente tramo de juego."""

    behavior: GovernedBehavior = Field(
        description=(
            "Behavior a ejecutar: 'combat' (lurrear/pelear), 'route' (seguir la "
            "ruta dibujada) o 'explore' (explorar/patrullar)."
        )
    )
    rationale: str = Field(
        default="",
        description="Justificacion breve en espanol (una frase).",
    )
