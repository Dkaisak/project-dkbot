"""Defaults y normalizacion de la seccion `llm` de la config."""
from __future__ import annotations

import os
from typing import Optional

# Gateway OpenAI-compatible de OpenCode Zen / OpenCode Go.
DEFAULT_LLM = {
    "enabled": False,
    "base_url": "https://opencode.ai/zen/v1",
    "model": "deepseek-v4.1-flash",
    "api_key_env": "OPENCODE_API_KEY",
    "api_key": "",
    "temperature": 0.0,
    "structured_method": "function_calling",
    "timeout": 20.0,
    "max_retries": 1,
    "decide_interval": 2.5,
    "stale_secs": 12.0,
    "governed": ["combat", "route", "explore"],
    "priority": 71,
    "max_enemies": 12,
    "log_rationale": True,
}

# Behaviors que el LLM puede gobernar (los unicos validos en `governed`).
KNOWN_GOVERNED = ("combat", "route", "explore")


def normalize(cfg: Optional[dict]) -> dict:
    """Devuelve la config `llm` completa (defaults + overrides), saneada."""
    out = dict(DEFAULT_LLM)
    out.update(cfg or {})
    try:
        out["temperature"] = float(out.get("temperature", 0.0))
    except (TypeError, ValueError):
        out["temperature"] = 0.0
    for key, default in (("timeout", 20.0), ("decide_interval", 2.5), ("stale_secs", 12.0)):
        try:
            out[key] = float(out.get(key, default))
        except (TypeError, ValueError):
            out[key] = default
    for key, default in (("max_retries", 1), ("priority", 71), ("max_enemies", 12)):
        try:
            out[key] = int(out.get(key, default))
        except (TypeError, ValueError):
            out[key] = default
    governed = out.get("governed")
    if isinstance(governed, str):
        governed = [governed]
    if not isinstance(governed, (list, tuple)):
        governed = list(DEFAULT_LLM["governed"])
    clean = [str(g) for g in governed if str(g) in KNOWN_GOVERNED]
    out["governed"] = clean or list(DEFAULT_LLM["governed"])
    return out


def resolve_api_key(cfg: dict) -> str:
    """API key explicita en config, o desde `api_key_env` (por defecto
    OPENCODE_API_KEY). Vacio = sin credenciales -> LLM inactivo."""
    key = str(cfg.get("api_key") or "").strip()
    if key:
        return key
    env = str(cfg.get("api_key_env") or "OPENCODE_API_KEY").strip()
    return (os.environ.get(env) or "").strip() if env else ""
