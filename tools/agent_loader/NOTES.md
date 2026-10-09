# Notas — Port a Windows (Fase 0: reconocimiento y estrategia)

Documento de resultados del reconocimiento del cliente Windows y de las
decisiones tomadas para el loader. Complementa `PLAN-WINDOWS.md`.

## 1. Cliente objetivo (medido en vivo)

| Dato | Valor |
|---|---|
| Ejecutable | `D:\Users\Dkaisak\AppData\Local\Programs\PokeXGames\pxgme.exe` |
| Arquitectura | **x86_64** (PE32+), ASLR (`base` 0x140000000) |
| Compilador | **mingw-14-posix x86_64** (gcc, no MSVC) — ver `clientlog` |
| Build | "Built for Windows on Tue Oct  6 07:31:06 2026", rev 0 (production) |
| Motor Lua | **LuaJIT 2.1** estático (`LuaJIT 2.1.1748459687`, `Lua 5.1`) |
| `mydata` | `D:\Users\Dkaisak\AppData\Local\Programs\PokeXGames\mydata` |
| CWD del cliente | el directorio de instalación (resuelve `assets/` relativo a él) |
| `pxgme-linux` | presente en la misma carpeta (22.5 MB, **con `.dynsym`**) |

Conclusión Fase 0: **Lua va estático, sin DLL y sin exports** (el exe solo exporta
2 símbolos; `.edata` = 0x7F). → estrategia **2B (firmas)**, no 2A.

## 2. Hallazgos relevantes

- **Lua cifrado**: los scripts se cargan como `.klua` (cifrado, 256 valores
  distintos; no es zlib/lzma). 348 `.klua` en `assets/`; el único `.lua` plano es
  `assets/init.lua`. Los módulos usan `.klmod` + `.klua`.
- **Fallback de arranque**: la función de arranque `0x140E8FFB0` intenta cargar
  `assets/init.klua`; si falla, carga `assets/init.lua` (plano). Luego llama a
  `globalInit` / `globalTerminate`. Por eso el `assets/init.lua` con el marcador
  del operador **nunca se ejecutó**: `init.klua` siempre carga bien.
  - Reemplazar `init.klua` **rompería el bootstrap** (los módulos los carga
    `globalInit`), así que no es una vía de inyección limpia.
- **`pxgme-linux` conserva 15519 símbolos dinámicos**, incluidos **148 de la API
  Lua** (`lua_gettop`, `lua_pcall`, `luaL_loadfilex`, ...). Sirve de **oráculo**
  para localizar funciones en el PE stripped.

## 3. Cómo se localizaron las funciones Lua en `pxgme.exe`

El PE está stripped (sin `.symtab`/COFF/debug). Dos binarios compilan el mismo
LuaJIT con gcc; la diferencia es la ABI (SysV vs Microsoft), así que el cuerpo de
funciones *hoja* coincide salvo los registros de argumento (`rdi`→`rcx`, ...).

1. **`lua_gettop`** — firma exacta del oráculo traducida a MS ABI:
   `48 8B 41 28 48 2B 41 20 48 C1 F8 03 C3`
   (`mov rax,[rcx+0x28]; sub rax,[rcx+0x20]; sar rax,3; ret`). **Única** en
   `.text` → `VA 0x140A29C30` (RVA `0xA29C30`).
2. **`lua_pcall`** — el wrapper C++ del cliente (`0x1404570E0`) llama a
   `0x140A32380` con `(L, nargs, -1=LUA_MULTRET, errfunc)` y comprueba el error
   → `VA 0x140A32380`.
3. **`luaL_loadbuffer`** — `0x140451590` (loader de scripts del cliente) llama a
   `0x140A33A40` con `(L, buff, size, name)`; la función construye el reader y
   llama a `lua_load` = `0x140A33230`. La siguiente, `0x140A33A70`, es
   `luaL_loadstring`.
4. **`lua_gc`** = `0x140A32870` (llamada con opción 3/4 = `LUA_GCCOUNT*`).

Set mínimo para el loader: `lua_gettop` (captura de `lua_State`), `luaL_loadbuffer`
y `lua_pcall`. **No hacen falta** `lua_pushstring`/`lua_setfield`: la DLL fija
`PXG_DIR` *preponiendo* `PXG_DIR = "<dir>"` al fuente del agente.

Las tres firmas usadas son **únicas** en `.text` y RIP-relativas (independientes
de la base de carga), por lo que valen en runtime.

## 4. Diseño del loader (Fase 2)

`tools/agent_loader/pxg_agent_loader.c` → `pxg_agent_loader.dll`:

1. `DllMain` lanza un hilo (nunca trabajo pesado dentro de `DllMain`).
2. Lee `pxg_agent_dll.json` junto a la DLL (`agent`, `dir`, `log`, `suspend`).
3. Localiza `lua_gettop`/`luaL_loadbuffer`/`lua_pcall` por **firma** (fallback a
   RVA). Si no encuentra la firma de `lua_gettop`, **aborta** (no toca nada).
4. **Hook inline** de `lua_gettop`: sobreescribe los 8 primeros bytes del prólogo
   (`mov rax,[rcx+0x28]; sub rax,[rcx+0x20]`) con `jmp stub` + 3 `nop`. El stub
   guarda `rcx` (=L) con `movabs rax,&g_L; mov [rax],rcx` (direccionamiento
   **absoluto**), ejecuta el prólogo original y vuelve con `jmp gettop+8`.
   - **Importante**: el stub se reserva con `alloc_near()` **dentro de ±2 GB** de
     `lua_gettop`, porque los `jmp rel32` (parche→stub y stub→gettop) usan
     desplazamiento de 32 bits. `g_L` se direcciona con `movabs` (absoluto), así
     que la DLL puede estar lejos. (Un primer intento con `mov [rip+disp], rcx` y
     el stub lejos del exe saltaba/escribía a una dirección inválida y **crasheaba
     el cliente**; ver §7.)
   - La instalación se hace con los demás hilos suspendidos (evita ejecutar bytes
     a medio escribir).
5. Con L: lee el agente, lo precede con `PXG_DIR = "<dir>"` (con `/`), y ejecuta
   `luaL_loadbuffer(L, src, len, "@pxg_agent.lua")` + `lua_pcall(L, 0, 0, 0)`.
6. **`suspend` (por defecto ON)**: suspende los demás hilos durante la llamada a
   Lua. El cliente llama a `lua_gettop` en cada frame (Lua activo), así que sin
   suspender hay carrera sobre el `lua_State`; suspender equivale al
   `break`/stop-the-world de gdb. `--no-suspend` para desactivarlo.

`tools/inject_windows.py` escribe la config y hace `LoadLibraryW` remoto
(`VirtualAllocEx` + `WriteProcessMemory` + `CreateRemoteThread`) y verifica que
`pxg_bot_state.json` se actualiza.

## 5. Build

Sin MSVC instalado. Se compila con **Zig** (`pip install ziglang`):

```
python -m ziglang cc -target x86_64-windows-gnu -shared -O2 -o pxg_agent_loader.dll pxg_agent_loader.c
```

También vale MinGW-w64 (`x86_64-w64-mingw32-gcc -shared`) o MSVC (`cl /LD`).
Script: `tools/agent_loader/build_windows.bat`.

## 6. Resultado de la inyección (probada)

**Funciona contra el cliente en vivo.** Sesión de referencia (`pxg_bot.log`):

```
[loader] gettop=00007FF67A7C9C30 loadbuffer=00007FF67A7D3A40 pcall=00007FF67A7D2380
[loader] hook de lua_gettop instalado (stub=00007FF67B1D0000) parche=E9 CB 63 A0 00 90 90 90
[loader] lua_State capturado: 0000016198400380
[loader] suspendidos 17 hilos
[loader] loadbuffer=0 pcall=0 (60387 bytes)
```

Tras inyectar, `pxg_bot_state.json` se actualiza cada ~200 ms
(`{"connected":false}` en la pantalla de login; se llena al entrar al juego) y el
cliente sigue estable.

> Nota de entorno: el sandbox de la sesión bloqueaba `VirtualAllocEx`/
> `WriteProcessMemory` **hacia procesos hijos** (`ERROR_ACCESS_DENIED`), pero la
> inyección contra `pxgme.exe` (proceso ajeno) sí funcionó.

## 7. Historial de bugs del hook (importante para updates)

1. **`mov [rip+disp], rcx` con la DLL lejos del exe** (>2 GB): el `disp32` se
   desborda → escribe a una dirección inválida al llamarse `lua_gettop` →
   **crash del cliente**. Corregido con `movabs` (absoluto).
2. **Stub reservado lejos del exe**: el `jmp rel32` del parche apuntaba a la
   dirección equivocada (se suma al `gettop+5` truncado a 32 bits) → salto a
   código basura → **crash**. Corregido con `alloc_near()` (±2 GB).
3. Validación local del hook en `tools/agent_loader/hook_selftest.py` (crea una
   función falsa con el mismo prólogo y comprueba retorno y captura) antes de
   tocar el cliente.

## 8. Pendiente / siguiente paso

1. Ya inyectado; al reiniciar el cliente hay que **reinyectar**
   (`run_windows.bat` o `python tools/inject_windows.py`).
2. Al entrar al juego, verificar que el `state` se llena (player, party, moves) y
   arrancar la GUI/bot (`python main.py gui` / `run_windows.bat`).
3. Re-derivar firmas tras cada update del cliente con
   `tools/derive_signatures.py` (oráculo Linux) + los call sites descritos.

## 9. Fix de reinyeccion en la misma sesion (RVA del fallback)

El hook inline de `lua_gettop` **no se retira** tras capturar el `lua_State`, asi
que en una segunda inyeccion la firma ya no coincide y el loader usa el fallback
`base + PXG_RVA_LUA_GETTOP`. Ese RVA estaba **0x1000 por debajo** del real (build
actual): los cuatro RVAs se corrigieron en `signatures.h`:

| | antes | correcto |
|---|---|---|
| `lua_gettop` | `0x0A29C30` | `0x0A2AC30` |
| `lua_pcall` | `0x0A32380` | `0x0A33380` |
| `luaL_loadbuffer` | `0x0A33A40` | `0x0A34A40` |

Con eso, `hook previo detectado` localiza el `lua_State` en el stub existente y
recarga el agente **sin reiniciar el cliente**. Si el cliente se actualiza,
re-derivar y ajustar de nuevo (los RVAs cambian; la firma sigue siendo primaria).

> La canonica `tools/agent_loader/pxg_agent_loader.dll` ya esta recompilada con el
> fix (recompilada con el cliente cerrado). Si el cliente se actualiza, re-derivar
> y recompilar de nuevo.
