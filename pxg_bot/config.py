from __future__ import annotations

import copy
import json
from typing import Optional

DEFAULT_HUMANIZER = {
    "enabled": True,
    "tick_jitter_pct": 30,
    "cooldown_jitter_pct": 25,
    "threshold_jitter_pct": 10,
    "key_duration_jitter_pct": 40,
    "micro_idle_chance": 0.04,
    "pause_chance": 0.004,
    "pause_seconds": [3, 12],
    "session_enabled": False,
    "play_minutes": [45, 120],
    "break_minutes": [5, 25],
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
    "direction_map": {"0": 0, "1": 4, "2": 1, "3": 5, "4": 2, "5": 6, "6": 3, "7": 7},
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
        "hp_threshold": 30,
        "range": 6,
        "ball_key": "F4",
        "cooldown": 1.5,
    },
    "combat": {
        "enabled": True,
        "detect_range": 7,
        "attack_range": 1,
        "moves": ["F5", "F6", "F7", "F8"],
        "cooldown": 0.7,
        "approach_cooldown": 0.15,
    },
    "loot": {
        "enabled": True,
        "range": 2,
        "loot_key": "F9",
        "cooldown": 1.0,
        "periodic_quickloot": False,
    },
    "route": {
        "enabled": True,
        "arrive_distance": 1,
        "step_delay": 0.25,
        "static_map": None,
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
    raw.setdefault("offsets", {})
    return raw


def save_config(cfg: dict, path: str) -> None:
    data = copy.deepcopy(cfg)
    data.pop("_doc", None)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
