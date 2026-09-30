#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys

from pxg_bot.bot import Bot
from pxg_bot.config import load_config
from pxg_bot.input import create_input, MockInput


def _parse_value(text: str, vtype: str):
    if vtype in ("f32", "f64"):
        return float(text)
    if text.lower().startswith("0x"):
        return int(text, 16)
    return int(text)


def _open(args, cfg):
    from pxg_bot.memory import open_process

    return open_process(
        name=args.process or cfg.get("process_name"),
        pid=getattr(args, "pid", None),
        pointer_size=int(cfg.get("settings", {}).get("pointer_size", 8)),
    )


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.mock:
        from pxg_bot.mock import MockStateSource

        source = MockStateSource(cfg, args.verbose)
        inp = MockInput(args.verbose)
    elif cfg.get("settings", {}).get("state_source") == "lua":
        from pxg_bot.lua_bridge import HybridInput, LuaBridge, LuaInput, LuaStateSource

        bridge = LuaBridge(cfg.get("lua", {}))
        if not bridge.fresh(5.0):
            print("aviso: el estado Lua no se actualiza; instala el agente (tools/install_agent.py)", file=sys.stderr)
        source = LuaStateSource(bridge)
        backend = cfg.get("settings", {}).get("input_backend", "lua")
        if backend == "hybrid":
            from pxg_bot.input import LinuxInput
            from pxg_bot.memory import find_pid

            pid = find_pid(cfg.get("process_name", "pxgme-linux"))
            key_settings = dict(cfg.get("settings", {}))
            key_settings["input_backend"] = "xdotool"
            key_input = LinuxInput(pid, key_settings)
            inp = HybridInput(LuaInput(bridge), key_input)
        elif backend == "lua":
            inp = LuaInput(bridge)
        else:
            inp = create_input(0, cfg.get("settings", {}))
    else:
        from pxg_bot.reader import MemoryStateSource

        process = _open(args, cfg)
        source = MemoryStateSource(process, cfg)
        inp = create_input(process.pid, cfg.get("settings", {}))

    bot = Bot(source, inp, cfg, verbose=args.verbose)
    try:
        bot.run(max_ticks=args.ticks)
    except KeyboardInterrupt:
        print("\ndetenido por el usuario")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    from pxg_bot import scanner

    process = _open(args, cfg)
    addresses = scanner.scan_value(process, _parse_value(args.value, args.type), args.type, args.limit)
    print(f"{len(addresses)} coincidencia(s) para {args.value} ({args.type})")
    for address in addresses[: args.limit]:
        print(f"  0x{address:X}")
    if args.refine is not None and addresses:
        kept = scanner.refine(process, addresses, _parse_value(args.refine, args.type), args.type)
        print(f"tras refinar con {args.refine}: {len(kept)}")
        for address in kept:
            print(f"  0x{address:X}")
    return 0


def cmd_pointer(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    from pxg_bot import scanner

    process = _open(args, cfg)
    chains = scanner.pointer_scan(process, int(args.address, 16), args.levels)
    print(f"{len(chains)} cadena(s) de punteros hacia {args.address}")
    for chain in chains[: args.limit]:
        print("  " + " -> ".join(f"0x{value:X}" for value in chain))
    return 0


def cmd_dump(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    from pxg_bot import scanner

    process = _open(args, cfg)
    print(scanner.dump(process, int(args.address, 16), args.size))
    return 0


def cmd_procs(args: argparse.Namespace) -> int:
    from pxg_bot.memory import list_processes

    for pid, name in list_processes(args.name):
        print(f"{pid:>8}  {name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pxg-bot", description="Bot AFK para clientes tipo Tibia (PokeXGames)")
    parser.add_argument("--config", default="config.json")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_target(sp):
        sp.add_argument("--process", default=None, help="nombre del ejecutable")
        sp.add_argument("--pid", type=int, default=None, help="pid directo")

    run = sub.add_parser("run", help="ejecuta el bot")
    run.add_argument("--mock", action="store_true", help="usa estado simulado (sin el cliente)")
    run.add_argument("--ticks", type=int, default=None, help="numero de ticks antes de salir")
    run.add_argument("--verbose", action="store_true")
    add_target(run)
    run.set_defaults(func=cmd_run)

    scan = sub.add_parser("scan", help="busca un valor en memoria")
    scan.add_argument("value")
    scan.add_argument("--type", default="i32", choices=["i8", "i16", "i32", "u16", "u32", "f32", "f64", "u8"])
    scan.add_argument("--refine", default=None, help="segundo valor para filtrar coincidencias")
    scan.add_argument("--limit", type=int, default=50)
    add_target(scan)
    scan.set_defaults(func=cmd_scan)

    pointer = sub.add_parser("pointer", help="busca cadenas de punteros hacia una direccion")
    pointer.add_argument("address", help="direccion hex, ej 0x1A2B3C")
    pointer.add_argument("--levels", type=int, default=3)
    pointer.add_argument("--limit", type=int, default=20)
    add_target(pointer)
    pointer.set_defaults(func=cmd_pointer)

    dump = sub.add_parser("dump", help="vuelca memoria en hex")
    dump.add_argument("address")
    dump.add_argument("--size", type=int, default=128)
    add_target(dump)
    dump.set_defaults(func=cmd_dump)

    procs = sub.add_parser("procs", help="lista procesos")
    procs.add_argument("name", nargs="?", default=None)
    procs.set_defaults(func=cmd_procs)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ndetenido por el usuario")
        return 130
    except Exception as exc:
        name = type(exc).__name__
        print(f"error: {name}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())