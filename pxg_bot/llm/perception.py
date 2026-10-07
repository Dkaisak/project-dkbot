"""Percepcion: resume el GameState a un contexto compacto para el LLM.

El objetivo es dar al modelo solo lo relevante para decidir el behavior
(combat/route/explore), sin volcar cientos de criaturas ni la UI entera.
"""
from __future__ import annotations

import json
from typing import Optional

MAX_MSG = 6


def _enemies(state) -> list:
    return [c for c in state.creatures if getattr(c, "attackable", False) and not c.is_player]


def _ref_pos(state) -> tuple[int, int, int]:
    pp = getattr(state, "pokemon_pos", None)
    if pp:
        return int(pp[0]), int(pp[1]), int(pp[2])
    p = state.player.pos
    return p.x, p.y, p.z


def _dist(a: tuple[int, int, int], c) -> int:
    return max(abs(c.pos.x - a[0]), abs(c.pos.y - a[1]))


def summarize(state, cfg: dict, extra: Optional[dict] = None) -> dict:
    """GameState -> dict compacto (serializable) para el prompt."""
    p = state.player
    ref = _ref_pos(state)
    enemies = sorted(_enemies(state), key=lambda c: _dist(ref, c))
    maxe = int(cfg.get("max_enemies", 12) or 12)

    enemy_list = [
        {
            "name": c.name,
            "dist_summon": _dist(ref, c),
            "hp": int(getattr(c, "hp_pct", 0) or 0),
            "shiny": bool(getattr(c, "shiny", False)),
        }
        for c in enemies[:maxe]
    ]
    in_screen = _screen_enemies(state)
    party = [
        {"slot": q.slot, "name": q.name, "hp": int(q.hp_pct), "active": bool(q.active)}
        for q in (state.party or [])
    ]
    moves = [
        {
            "key": str(m.get("key", "")),
            "name": str(m.get("name", "")),
            "pct": m.get("pct"),
            "aoe": bool(m.get("aoe")),
            "effect": str(m.get("effect", "")),
        }
        for m in (state.moves or [])
    ]
    summon_hp = getattr(state, "pokemon_hp", None)
    revives = _revives(state)

    ctx = {
        "player": {
            "hp_pct": int(p.hp_pct),
            "pos": [p.pos.x, p.pos.y, p.pos.z],
        },
        "summon": {
            "pos": list(ref),
            "hp_pct": summon_hp,
            "known": getattr(state, "pokemon_pos", None) is not None,
        },
        "enemies_total": len(enemies),
        "enemies_on_screen": in_screen,
        "enemies_nearest": enemy_list,
        "active_pokemon": getattr(state, "active_pokemon_name", "") or "",
        "party": party,
        "moves": moves,
        "skill_order": list(getattr(state, "skill_order", []) or []),
        "lure_order": list(getattr(state, "lure_order", []) or []),
        "corpses_pending": len(_pending_corpses(state)),
        "revives": revives,
        "fight_mode": getattr(state, "fight_mode", None),
        "nav_result": str(getattr(state, "nav_result", "") or ""),
        "attacking": str(getattr(state, "attacking_name", "") or ""),
        "connected": bool(getattr(state, "connected", False)),
        "server_msgs": [str(m) for m in (getattr(state, "server_msgs", []) or [])][-MAX_MSG:],
    }
    if extra:
        ctx.update(extra)
    return ctx


def _screen_enemies(state) -> int:
    vis = state.visible or {}
    hw = int(vis.get("w", 21)) // 2
    hh = int(vis.get("h", 11)) // 2
    px, py = state.player.pos.x, state.player.pos.y
    return sum(
        1 for c in _enemies(state)
        if abs(c.pos.x - px) <= hw and abs(c.pos.y - py) <= hh
    )


def _pending_corpses(state) -> list:
    # `corpses_pending` vive en el Blackboard, no en el GameState; aqui solo
    # contamos los `defeated` del tick actual.
    return list(getattr(state, "defeated", []) or [])


def _revives(state) -> Optional[int]:
    bc = getattr(state, "bag_counts", None) or {}
    for key in ("2269", 2269):
        if key in bc:
            try:
                return int(bc[key])
            except (TypeError, ValueError):
                return None
    return None


def render(ctx: dict) -> str:
    """Contexto -> texto compacto para el prompt."""
    try:
        return json.dumps(ctx, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return json.dumps(ctx, ensure_ascii=True, default=str)
