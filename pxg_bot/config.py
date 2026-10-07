from __future__ import annotations

import copy
import json
import os
import sys
from typing import Optional

IS_WINDOWS = sys.platform == "win32"

# Nombre del proceso del cliente según plataforma. En Windows el ejecutable es
# pxgme.exe; en Linux el binario nativo pxgme-linux.
DEFAULT_PROCESS = "pxgme.exe" if IS_WINDOWS else "pxgme-linux"


def _default_mydata() -> str:
    """Directorio `mydata` del cliente. Portable, sin rutas absolutas fijas.

    Orden: variable de entorno PXG_MYDATA > heurística por plataforma. El botón
    de la GUI / `tools/setup_client.py` (attach) lo detectan igualmente y
    reescriben `config.json`.
    """
    env = os.environ.get("PXG_MYDATA")
    if env:
        return os.path.abspath(env)
    if IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(local, "Programs", "PokeXGames", "mydata")
    return os.path.join(os.path.expanduser("~"), "pxg-linux", "mydata")


DEFAULT_MYDATA = _default_mydata()


def _lua_paths(base: str) -> dict:
    return {
        "state_file": os.path.join(base, "pxg_bot_state.json"),
        "cmd_file": os.path.join(base, "pxg_bot_cmd.txt"),
        "control_file": os.path.join(base, "pxg_control.json"),
        "status_file": os.path.join(base, "pxg_bot_status.json"),
        "minimap_file": os.path.join(base, "minimap.otmm"),
        "shiny_file": os.path.join(base, "pxg_shiny.json"),
        "pokemon_skills_file": os.path.join(base, "pxg_pokemon_skills.json"),
        "ignore_file": os.path.join(base, "pxg_ignore.json"),
        "routes_file": os.path.join(base, "pxg_routes.json"),
        "direction_map": {"0": 0, "1": 4, "2": 1, "3": 5, "4": 2, "5": 6, "6": 3, "7": 7},
    }


DEFAULT_HUMANIZER = {
    "enabled": True,
    "profile": "normal",
    "overrides": {},
    "idle_only_pause": True,
    "random_seed": None,
}

DEFAULT_SETTINGS = {
    "tick_seconds": 0.1,
    "state_source": "lua",
    "background_input": False,
    "input_backend": "lua",
    "window_name": None,
    "pointer_size": 8,
    "tile_size": 32,
    "player_screen_x": 480,
    "player_screen_y": 360,
    "move_key_duration": 0.02,
    "keymap": {
        "move": {
            "0": "NUM8", "1": "NUM9", "2": "NUM6", "3": "NUM3",
            "4": "NUM2", "5": "NUM1", "6": "NUM4", "7": "NUM7",
        },
        "say": "ENTER",
    },
    "humanizer": DEFAULT_HUMANIZER,
}

DEFAULT_LUA = _lua_paths(DEFAULT_MYDATA)

DEFAULT_UI = {
    "port": 8765,
    "host": "127.0.0.1",
    "auto_open": True,
    "log_file": os.path.join(DEFAULT_MYDATA, "pxg_bot.log"),
    "mode": "app",
    "debug": False,
    "icon": "tools/icon.ico",
    "window": {"width": 1280, "height": 860, "title": "DKBot", "on_top": False},
    "tray": {"enabled": True, "minimize_on_close": True},
}

DEFAULT_BEHAVIORS = {
    "crisis": {
        "enabled": True,
        "pause_on_player": True,
        "player_range": 7,
        "logout_on_player": False,
        "logout_key": "F12",
        "reconnect_key": "F1",
        "reconnect_cooldown": 5,
        "panic_hp_pct": 10,
        "flee_detect_range": 5,
    },
    "healing": {
        "enabled": True,
        "player_hp_pct": 60,
        "player_heal_key": "F2",
        "pokemon_hp_pct": 40,
        "pokemon_heal_key": "F3",
        "cooldown": 1.0,
    },
    "capture": {
        "enabled": True,
        "targets": [],
        "catch_all": False,
        "shiny_only": True,
        "hp_threshold": 30,
        "range": 4,
        "ball_key": "F4",
        "ball_item": 2652,
        "interval": 0.0,
        "max_throws": 5,
        "cooldown": 1.5,
    },
    "combat": {
        "enabled": True,
        "detect_range": 7,
        "attack_range": 3,
        "engage_radius": 0,
        "cast_min_in_range": 2,
        "require_all_close": True,
        "moves": ["F5", "F6", "F7", "F8"],
        "cooldown": 0.3,
        "skill_repeat_guard": 0.3,
        "approach_cooldown": 0.15,
        "target_switch_secs": 4.0,
        "target_drop_secs": 3.0,
        "target_blacklist_secs": 10.0,
        "lure_aoe": True,
        "lure_visible_min": 5,
        "lure_gather_timeout": 10.0,
        "lure_use_single": False,
        "hold_timeout": 15.0,
        "fight_recover_secs": 1.5,
        "resume_pause_secs": 1.0,
        "lure_summon": True,
        "lure_summon_radius": 3,
        "summon_arrive_tolerance": 1,
        "summon_grace_secs": 3.0,
        "buff_on_screen": True,
        "buff_visible_min": 3,
        "panic_hp": 25,
        "panic_hp_source": "summon",
        "panic_skills": "all",
        "pokestop_method": "func",
        "pokestop_talk": "!pokestop",
        "pokestop_key": "R",
        "aoe_priority": ["Air Vortex"],
        "no_damage_secs": 3.0,
        "spawn_clear_dist": 12,
        "spawn_wait_secs": 1.0,
        "spawn_away_timeout": 30.0,
        "spawn_return_timeout": 45.0,
        "spawn_retries": 2,
        "spawn_blacklist_secs": 20.0,
        "spawn_cooldown_secs": 8.0,
        "resend_secs": 0.5,
        "manage_fight_mode": True,
        "cast_fight_mode": 1,
        "idle_fight_mode": 3,
        "fight_mode_delay": 0.3,
    },
    "loot": {
        "enabled": True,
        "reach": 20,
        "enemy_range": 3,
        "collect_interval": 1.0,
        "collect_grace": 0.7,
        "stuck_secs": 2.5,
        "resend_secs": 4.0,
        "phase_secs": 10.0,
    },
    "revive": {
        "enabled": True,
        "slot": 3,
        "item": 2269,
        "min_interval_secs": 8.0,
        "on_stun": True,
        "click_delay": 0.15,
        "withdraw_wait_secs": 0.5,
        "first_revive_wait_secs": 2.0,
        "after_aoe_wait_secs": 1.0,
        "verify_delay": 0.5,
        "urgent_click_delay": 0.05,
        "urgent_verify_delay": 0.25,
        "urgent_withdraw_wait": 0.25,
        "revive_retry_secs": 3.0,
        "stun_secs": 2.5,
        "fast_min_interval_secs": 0.6,
        "stun_min_interval_secs": 0.6,
        "use_attack_range": True,
        "attack_range": 3,
        "post_mode": 3,
        "max_attempts": 3,
    },
    "summon": {
        "enabled": True,
        "interval_secs": 3.0,
        "radius": 4,
        "max_dist": 2,
    },
    "route": {
        "enabled": True,
        "detour": False,
        "densify": True,
        "start_idle_random": False,
        "start_idle_enabled": True,
        "arrive_distance": 1,
        "step_delay": 0.25,
        "resend_secs": 0.5,
        "stuck_secs": 2.0,
        "progress_stuck_secs": 6.0,
        "max_nav_dist": 8,
        "start_idle_seconds": [25, 60],
        "static_map": None,
    },
    "explore": {
        "enabled": False,
        "home": None,
        "radius": 60,
        "patrol": True,
        "patrol_step": 4,
        "coverage_radius": 2,
        "lookahead": 40,
        "stuck_secs": 2.5,
        "blacklist_secs": 30,
        "resend_secs": 0.6,
        "save_interval": 5.0,
    },
}


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    raw["settings"] = deep_merge(DEFAULT_SETTINGS, raw.get("settings", {}))
    raw["behaviors"] = deep_merge(DEFAULT_BEHAVIORS, raw.get("behaviors", {}))
    raw["lua"] = deep_merge(DEFAULT_LUA, raw.get("lua", {}))
    raw["ui"] = deep_merge(DEFAULT_UI, raw.get("ui", {}))
    raw.setdefault("offsets", {})
    return raw


def save_config(cfg: dict, path: str) -> None:
    data = copy.deepcopy(cfg)
    data.pop("_doc", None)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
