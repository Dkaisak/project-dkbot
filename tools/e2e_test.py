"""Test end-to-end: arranca el cliente falso, lee su memoria real, resuelve
offsets, escanea y ejecuta el bot completo. No requiere X11 ni el juego.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pxg_bot.bot import Bot
from pxg_bot.input import MockInput
from pxg_bot.memory import open_process
from pxg_bot.reader import MemoryStateSource
from pxg_bot import scanner


def build_config(info: dict) -> dict:
    pf = info["player_fields"]
    cf = info["creature_fields"]
    kf = info["pokemon_fields"]
    return {
        "process_name": "fake_client",
        "offsets": {
            "player_base": {"mode": "static", "address": hex(info["player_base"]), "deref": 1},
            "player": {"fields": {
                "hp": {"offset": hex(pf["hp"]), "type": "i32"},
                "max_hp": {"offset": hex(pf["max_hp"]), "type": "i32"},
                "mp": {"offset": hex(pf["mp"]), "type": "i32"},
                "max_mp": {"offset": hex(pf["max_mp"]), "type": "i32"},
                "x": {"offset": hex(pf["x"]), "type": "i32"},
                "y": {"offset": hex(pf["y"]), "type": "i32"},
                "z": {"offset": hex(pf["z"]), "type": "i32"},
                "level": {"offset": hex(pf["level"]), "type": "i32"},
                "exp": {"offset": hex(pf["exp"]), "type": "i32"},
                "direction": {"offset": hex(pf["direction"]), "type": "u8"},
                "alive": {"offset": hex(pf["alive"]), "type": "bool"},
                "name": {"offset": hex(pf["name"]), "type": "str", "len": 32},
            }},
            "battle_array": {"mode": "static", "address": hex(info["battle_array"]), "deref": 1},
            "battle_count": {"mode": "static", "address": hex(info["battle_count"]), "deref": 0},
            "battle_list": {
                "from": "battle_array", "offset": "0x0", "deref": False,
                "pointer_array": True, "stride": hex(info["pointer_size"]),
                "count_base": "battle_count", "count": {"offset": "0x0", "type": "i32"},
                "max_entries": 8,
                "fields": {
                    "id": {"offset": hex(cf["cid"]), "type": "i32"},
                    "name": {"offset": hex(cf["name"]), "type": "str", "len": 32},
                    "x": {"offset": hex(cf["x"]), "type": "i32"},
                    "y": {"offset": hex(cf["y"]), "type": "i32"},
                    "z": {"offset": hex(cf["z"]), "type": "i32"},
                    "hp_pct": {"offset": hex(cf["hp_pct"]), "type": "u8"},
                    "kind": {"offset": hex(cf["kind"]), "type": "u8"},
                    "is_player": {"offset": hex(cf["is_player"]), "type": "bool"},
                    "is_wild": {"offset": hex(cf["is_wild"]), "type": "bool"},
                },
            },
            "party_base": {"mode": "static", "address": hex(info["party_base"]), "deref": 0},
            "party": {
                "from": "party_base", "offset": "0x0", "deref": False,
                "pointer_array": False, "stride": hex(info["pokemon_stride"]),
                "count_fixed": 6, "max_entries": 6,
                "fields": {
                    "slot": {"offset": hex(kf["slot"]), "type": "i32"},
                    "name": {"offset": hex(kf["name"]), "type": "str", "len": 32},
                    "hp_pct": {"offset": hex(kf["hp_pct"]), "type": "u8"},
                    "level": {"offset": hex(kf["level"]), "type": "i32"},
                    "active": {"offset": hex(kf["active"]), "type": "bool"},
                    "alive": {"offset": hex(kf["alive"]), "type": "bool"},
                    "in_field": {"offset": hex(kf["in_field"]), "type": "bool"},
                },
            },
        },
        "settings": {"tick_seconds": 0.05, "humanizer": {"enabled": False}},
        "behaviors": {},
        "route": {"loop": True, "waypoints": [[100, 100, 7], [106, 100, 7]]},
    }


def main() -> int:
    child = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "tools", "fake_client.py")],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        info = json.loads(child.stdout.readline())
        print(f"cliente falso pid={info['pid']} ptr={hex(info['player_base'])}")

        proc = open_process(pid=info["pid"])
        cfg = build_config(info)
        source = MemoryStateSource(proc, cfg)

        state = source.read_state()
        print(f"lectura real -> nombre={state.player.name!r} pos=({state.player.pos.x},{state.player.pos.y}) "
              f"hp={state.player.hp}/{state.player.max_hp} pokemon={state.party[0].name} "
              f"({state.party[0].hp_pct}%) criaturas={len(state.creatures)}")
        assert state.player.name.strip("\x00") == "Hero", "no se leyo el nombre del jugador"
        assert state.player.pos.x == 100 and state.player.pos.y == 100
        assert state.party and state.party[0].name.strip("\x00") == "Pikachu"

        player_addr = proc.read_pointer(info["player_base"])
        exp_addr = player_addr + info["player_fields"]["exp"]
        hits = scanner.scan_value(proc, 0x0BADF00D, "i32", max_hits=500)
        assert exp_addr in hits, "el escaner no encontro el valor centinela"

        print(f"escaneo -> {len(hits)} coincidencias; exp centinela en 0x{exp_addr:X} localizado")

        bot = Bot(source, MockInput(), cfg, verbose=False)
        seen = set()
        for _ in range(60):
            _state, chosen = bot.tick()
            if chosen:
                seen.add(chosen)
            time.sleep(0.05)

        print("comportamientos observados:", ", ".join(sorted(seen)))
        expected = {"route", "combat", "capture", "heal", "loot", "crisis"}
        missing = expected - seen
        assert not missing, f"faltaron comportamientos: {missing}"

        proc.close()
        print("\nE2E OK: memoria real, offsets, escaneo y bot funcionando")
        return 0
    finally:
        child.terminate()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()


if __name__ == "__main__":
    sys.exit(main())
