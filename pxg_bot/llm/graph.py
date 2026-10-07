"""Grafo LangGraph de la capa de decision.

Flujo: perceive -> decide -> validate.

El grafo se compila una vez (por hilo) y se invoca con el contexto ya
construido. La salida es una `Decision` validada contra los behaviors
gobernados; si algo falla, deja `decision=None` (el bot cae a la politica
clasica). No usa checkpointer: cada invocacion es autocontenida (el contexto
incluye todo el estado relevante).
"""
from __future__ import annotations

from typing import Optional, TypedDict

from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

from .decision import Decision

SYSTEM_PROMPT = """\
Eres la capa de decision de un bot AFK de un MMO (PokeXGames, tipo Tibia/OTClient).
Cada pocos segundos eliges UNA sola accion de alto nivel para el bot. NO ejecutas
nada: solo eliges cual de los behaviors gobernados debe tomar el control.

Behaviors disponibles:
- combat: lurrear y pelear. El bot junta enemigos en pantalla, para la navegacion,
  ordena su pokemon, lanza pokestop y castea skills AoE. Elige esto cuando convenga
  pelear (hay enemigos a la vista y el pokemon propio esta sano).
- route: seguir la ruta dibujada por el usuario (waypoints). Elige esto para avanzar
  por el recorrido previsto.
- explore: explorar/patrullar zonas sin ruta fija. Elige esto si no hay una ruta util
  o quieres cubrir terreno nuevo.

Guia:
- Si el pokemon propio (summon) esta con vida baja y hay muchos enemigos, evita
  'combat': elige 'route' o 'explore' para retirarte/avanzar.
- Si el summon esta sano y hay enemigos cerca en pantalla, 'combat' suele ser lo mejor.
- Si no hay enemigos y hay ruta, 'route'; si no hay ruta, 'explore'.
- Responde SIEMPRE en el formato pedido, con un `rationale` corto en espanol.

Contexto (JSON) del estado actual del juego viene en el mensaje de usuario.
"""


class GraphState(TypedDict, total=False):
    context: str
    decision: Optional[dict]
    error: str


def build_model(cfg: dict, api_key: str) -> ChatOpenAI:
    return ChatOpenAI(
        base_url=str(cfg.get("base_url")),
        api_key=api_key,
        model=str(cfg.get("model")),
        temperature=float(cfg.get("temperature", 0.0)),
        timeout=float(cfg.get("timeout", 20.0)),
        max_retries=int(cfg.get("max_retries", 1)),
    )


def build_graph(model: ChatOpenAI, governed: list, method: str = "function_calling"):
    """Compila el StateGraph con salida estructurada.

    Por defecto usa *function calling* (tools), que es lo mas portable en
    gateways OpenAI-compatibles; `json_schema`/`json_mode` quedan como opcion.
    """
    structured = model.with_structured_output(Decision, method=method)
    allowed = set(governed)

    def perceive(state: GraphState) -> GraphState:
        # El contexto ya viene construido; el nodo existe para dejar el flujo
        # explicito y permitir ampliarlo (p. ej. recuperar memoria) mas adelante.
        return {}

    def decide(state: GraphState) -> GraphState:
        messages = [("system", SYSTEM_PROMPT), ("human", state.get("context", "{}"))]
        try:
            result = structured.invoke(messages)
        except Exception as exc:  # red/timeout/schema -> sin decision (fallback)
            return {"decision": None, "error": f"{type(exc).__name__}: {exc}"}
        if isinstance(result, Decision):
            return {"decision": result.model_dump()}
        if isinstance(result, dict):
            return {"decision": result}
        return {"decision": None, "error": "salida no reconocida"}

    def validate(state: GraphState) -> GraphState:
        decision = state.get("decision")
        if not decision:
            return {}
        behavior = str(decision.get("behavior", ""))
        if behavior not in allowed:
            return {"decision": None, "error": f"behavior no gobernado: {behavior}"}
        return {"decision": {"behavior": behavior,
                             "rationale": str(decision.get("rationale", ""))}}

    graph = StateGraph(GraphState)
    graph.add_node("perceive", perceive)
    graph.add_node("decide", decide)
    graph.add_node("validate", validate)
    graph.add_edge(START, "perceive")
    graph.add_edge("perceive", "decide")
    graph.add_edge("decide", "validate")
    graph.add_edge("validate", END)
    return graph.compile()
