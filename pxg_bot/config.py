from __future__ import annotations

import copy
import json
from typing import Optional

DEFAULT_HUMANIZER = {
    "enabled": True,
    "profile": "normal",
    "overrides": {},
    "idle_only_pause": True,
    "random_seed": None,
}

DEFAULT_SETTINGS = {
    "tick_seconds": 0.1,
    "state_source": "memory",
    "background_input": False,
    "input_backend": "auto",
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

DEFAULT_LUA = {
    "state_file": "/home/dkaisak/Descargas/pxg-linux/mydata/pxg_bot_state.json",
    "cmd_file": "/home/dkaisak/Descargas/pxg-linux/mydata/pxg_bot_cmd.txt",
    "control_file": "/home/dkaisak/Descargas/pxg-linux/mydata/pxg_control.json",
    "status_file": "/home/dkaisak/Descargas/pxg-linux/mydata/pxg_bot_status.json",
    "minimap_file": "/home/dkaisak/Descargas/pxg-linux/mydata/minimap.otmm",
    "shiny_file": "/home/dkaisak/Descargas/pxg-linux/mydata/pxg_shiny.json",
    "pokemon_skills_file": "/home/dkaisak/Descargas/pxg-linux/mydata/pxg_pokemon_skills.json",
    "ignore_file": "/home/dkaisak/Descargas/pxg-linux/mydata/pxg_ignore.json",
    "routes_file": "/home/dkaisak/Descargas/pxg-linux/mydata/pxg_routes.json",
    "direction_map": {"0": 0, "1": 4, "2": 1, "3": 5, "4": 2, "5": 6, "6": 3, "7": 7},
}

DEFAULT_UI = {
    "port": 8765,
    "host": "127.0.0.1",
    "auto_open": True,
    "log_file": "/home/dkaisak/Descargas/pxg-linux/mydata/pxg_bot.log",
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
        "range": 1,
        "ball_key": "F4",
        "interval": 1.0,
        "max_throws": 10,
        "cooldown": 1.5,
    },
    "combat": {
        "enabled": True,
        "detect_range": 7,
        "attack_range": 2,
        "moves": ["F5", "F6", "F7", "F8"],
        "cooldown": 0.1,
        "approach_cooldown": 0.15,
        "target_switch_secs": 4.0,
        "target_drop_secs": 3.0,
        "target_blacklist_secs": 10.0,
        "lure_aoe": True,
        "lure_visible_min": 5,
        "lure_attack_range": 2,
        "lure_attack_min": 2,
        "lure_wait_timeout": 20.0,
        "panic_hp": 20,
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
    },
    "loot": {
        "enabled": True,
        "range": 2,
        "loot_key": "F9",
        "cooldown": 1.0,
        "periodic_quickloot": False,
    },
    "revive": {
        "enabled": True,
        "slot": 3,
        "item": 2269,
        "min_interval_secs": 8.0,
        "on_stun": True,
        "click_delay": 0.3,
        "verify_delay": 1.2,
        "max_attempts": 3,
    },
    "route": {
        "enabled": True,
        "detour": False,
        "start_idle_random": False,
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
