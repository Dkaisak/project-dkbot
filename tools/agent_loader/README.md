# agent_loader — inyección del agente Lua en `pxgme.exe`

Carga `tools/pxg_agent.lua` dentro del cliente Windows **sin usar la ventana**,
igual que hace `tools/install_agent.py` (gdb) en Linux.

## Componentes

| Fichero | Qué es |
|---|---|
| `pxg_agent_loader.c` | La DLL. Captura el `lua_State` y ejecuta el agente. |
| `signatures.h` | Firmas/RVAs de `lua_gettop`/`luaL_loadbuffer`/`lua_pcall`. |
| `NOTES.md` | Reconocimiento del cliente y método de derivación de firmas. |
| `build_windows.bat` / `Makefile` | Build (MinGW / MSVC / Zig). |
| `../inject_windows.py` | Inyector (ctypes) + config de la DLL. |

## Build

```
# Zig (sin admin; pip install ziglang)
python -m ziglang cc -target x86_64-windows-gnu -shared -O2 -o pxg_agent_loader.dll pxg_agent_loader.c

# MinGW-w64
x86_64-w64-mingw32-gcc -shared -O2 -o pxg_agent_loader.dll pxg_agent_loader.c

# MSVC
cl /nologo /O2 /LD /Fe:pxg_agent_loader.dll pxg_agent_loader.c kernel32.lib
```

El cliente es **x86_64**: la DLL debe ser x86_64.

## Uso

```
# 1) Con el cliente pxgme.exe abierto:
python tools/inject_windows.py --dll tools/agent_loader/pxg_agent_loader.dll

# o automático (detecta mydata y el agente):
python tools/setup_client.py --install
```

`inject_windows.py` escribe `pxg_agent_dll.json` junto a la DLL:

```json
{ "agent": "C:\\...\\tools\\pxg_agent.lua",
  "dir":   "D:\\...\\PokeXGames\\mydata",
  "log":   "D:\\...\\PokeXGames\\mydata\\pxg_bot.log",
  "suspend": false }
```

y luego inyecta la DLL (`LoadLibraryW` remoto). Comprueba que
`pxg_bot_state.json` se actualiza.

> **Nota**: la inyección necesita poder **escribir** en el proceso del cliente
> (`VirtualAllocEx`/`WriteProcessMemory`). En entornos con esa operación
> restringida (sandbox, o si el anti-cheat lo bloquea) fallará; ejecútalo desde
> una consola normal del usuario.

## Cómo funciona

1. `DllMain` → hilo worker.
2. Lee la config JSON junto a la DLL.
3. Localiza en el módulo principal:
   - `lua_gettop` por firma exacta (única en `.text`),
   - `luaL_loadbuffer` y `lua_pcall` por firma (fallback a RVA).
4. Hook inline de `lua_gettop` → guarda el primer argumento (`rcx` = `lua_State*`).
   El stub se reserva **dentro de ±2 GB** del exe (`alloc_near`) porque los `jmp`
   del parche son `rel32`; `g_L` se direcciona con `movabs` (absoluto).
5. Prepend `PXG_DIR = "<dir>"` al fuente del agente y ejecuta
   `luaL_loadbuffer` + `lua_pcall` con los **demás hilos suspendidos**
   (`suspend`, por defecto ON) para no competir con el hilo del juego por el
   `lua_State`.

Reinstalable: el agente cancela su tick anterior (`PXG_GEN`), así que inyectar
varias veces es seguro.

## Actualizaciones del cliente

Si `pxgme.exe` cambia, las firmas pueden dejar de coincidir (el loader lo
detecta y **aborta sin tocar nada**; escribe el motivo en el log). Re-derivar con
`tools/derive_signatures.py` (usa `pxgme-linux` como oráculo) y actualizar
`signatures.h`. Ver `NOTES.md` §3.
