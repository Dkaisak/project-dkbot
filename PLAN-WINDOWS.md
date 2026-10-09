# Plan — Port de `ShinyBot` a Windows (inyección por DLL)

> Este documento es un plan de trabajo autocontenido para una sesión de OpenCode
> en **Windows**. El objetivo es que el bot `ShinyBot` (bot AFK para PokeXGames)
> funcione en Windows igual que en Linux, cargando el agente Lua dentro de
> `pxgme.exe` mediante una **DLL** inyectada (en Linux se usa `gdb`).

---

## 0. Contexto del proyecto (resumen)

`ShinyBot` controla el cliente nativo de PokeXGames (`pxgme.exe`) **sin usar la
ventana**. La arquitectura es:

- **Bot Python** (`main.py`, `pxg_bot/`): bucle de decisión por *tick*. Lee el
  estado y elige **un** behavior por prioridad (`crisis`, `heal`, `loot`,
  `capture`, `revive`, `combat`, `summon`, `route`, `explore`).
- **Agente Lua** (`tools/pxg_agent.lua`): vive **dentro** del cliente. Publica el
  estado en un JSON (`state_file`) y consume comandos de un fichero
  (`cmd_file`). También hace de pathfinder y ejecuta clics/hotkeys/skills.
- **GUI web** (`pxg_bot/webui.py` + `pxg_bot/web/`): `http.server`, arranca/para
  el bot, muestra estado/mapa/ruta y edita config.

**Comunicación Python ↔ cliente: ficheros** (JSON/TXT) en el directorio
`mydata` del cliente. **No hay red ni memoria en el camino principal**
(`settings.state_source = "lua"`, `settings.input_backend = "lua"`).

Conclusión clave: **casi todo es cross-platform**; lo único específico de
plataforma en el camino principal es **cómo se inyecta el agente Lua**.

---

## 1. Estado actual del soporte Windows

Ya existe (pero **no se usa** en el camino principal, que es Lua):

- `pxg_bot/memory.py`: `WindowsProcess` (OpenProcess/ReadProcessMemory),
  `list_processes`/`find_pid` por Toolhelp32.
- `pxg_bot/input.py`: `WinInput` (SendInput/PostMessage).
- `pxg_bot/cursor.py`: `SetCursorPos` (utilidad; no está en el flujo normal).

Falta (esto es el port):

1. **Inyector del agente**: `tools/install_agent.py` usa `gdb` (Linux-only).
2. **`pxg_bot/setup.py` (`attach`)**: usa `pgrep`, `/proc/<pid>/exe`,
   `/proc/<pid>/cwd` y gdb → Linux-only.
3. **Rutas hardcodeadas de Linux** en `pxg_bot/config.py`, `config.json` y el
   agente (`local DIR = "/home/dkaisak/Descargas/pxg-linux/mydata"`).
4. **`process_name`** por defecto `pxgme-linux` → debe ser `pxgme.exe`.
5. **Operaciones de fichero del agente**: `os.rename(CMD, CMD.tmp)` falla en
   Windows si `CMD.tmp` ya existe; separadores y codificación.

`cursor.py`/`x11mouse.py` **no se usan** en el camino principal (el revive y el
`order` van por Lua). No hacen falta para el port.

---

## 2. Fase 0 — Reconocimiento del cliente Windows (PRIMERO)

Decide la estrategia de la DLL: **¿Lua es una DLL o va estático en el exe?**

### Tarea 0.1 — Escribir `tools/recon_windows.py` (Python stdlib, solo lectura)

Debe:

1. Encontrar `pxgme.exe` (usar `pxg_bot.memory.find_pid`/`list_processes`).
2. Abrir el proceso con `WindowsProcess` (`pxg_bot.memory.open_process`) y
   **listar sus módulos** (`proc._modules`), buscando `lua*.dll`
   (`lua51.dll`, `lua5.1.dll`, `luajit.dll`, `lua.dll`).
3. Volcar la **tabla de exports** del exe principal (parsear el PE: Data
   Directory[0] = Export Table) y buscar nombres que empiecen por `lua`
   (`luaL_loadfile`, `lua_pcall`, `lua_gettop`, `luaL_newstate`, …). Hacer lo
   mismo con los módulos `lua*.dll`.
4. Reportar arquitectura (x64/x86, vía `IsWow64Process` / `machine` del PE).
5. Reportar el directorio del exe (ruta) y si existe `mydata` cerca
   (`<dir exe>/mydata` y `<cwd>/mydata`).

Salida esperada: un JSON con `{pid, exe, arch, modules[], lua_exports[], mydata}`.

**Criterio de aceptación:** sabemos si hay una DLL de Lua exportando las
funciones, o si hay que ir por **firmas**.

> Si `lua*.dll` existe con exports → Fase 2A (GetProcAddress, fácil).
> Si Lua va estático y **exporta** `lua*` en `pxgme.exe` → también fácil.
> Si Lua va estático y **no exporta** → Fase 2B (firmas; hay que encontrarlas).

### Tarea 0.2 — Decidir la estrategia
Escribir la decisión y las direcciones/patrones encontrados en
`tools/agent_loader/NOTES.md`.

---

## 3. Fase 1 — Rutas y configuración (cross-platform, sin Windows)

Estas tareas se pueden hacer ya y son de bajo riesgo.

### Tarea 1.1 — `pxg_bot/config.py`
- `DEFAULT_SETTINGS["state_source"]` y `input_backend` ya son `lua`; mantener.
- Hacer el `process_name` por defecto dependiente de la plataforma:
  `"pxgme.exe"` en Windows, `"pxgme-linux"` en Linux (p. ej.
  `import sys; DEFAULT_PROCESS = "pxgme.exe" if sys.platform == "win32" else "pxgme-linux"`).
- `DEFAULT_LUA`: dejar las rutas como placeholders **derivadas del `mydata`**
  (ver 1.3). Evitar rutas absolutas de Linux hardcodeadas.

### Tarea 1.2 — `config.json`
- Igual: no hardcodear `/home/dkaisak/...`. Se rellenan solas con el `attach`.

### Tarea 1.3 — `pxg_bot/setup.py` (portar `attach` a Windows)
- `find_pid`: ya vale (`memory.find_pid`).
- `proc_exe`/`proc_cwd`: en Windows, obtener la ruta del exe con
  `QueryFullProcessImageNameW` (kernel32) o desde `WindowsProcess._modules`
  (módulo principal). El `cwd` no es fiable en Windows → usar el dir del exe.
- `detect_mydata`: buscar `<dir exe>/mydata` (y `<dir exe>/../mydata`).
- `install_agent`: en Windows, llamar al **inyector** (Fase 2b) en vez de gdb.

### Tarea 1.4 — `tools/pxg_agent.lua`
- `local DIR = _G.PXG_DIR or "<default>"` (la DLL inyecta `_G.PXG_DIR`; el
  `attach` de Linux sigue reescribiendo el default como hoy).
- `readCmd`: borrar `CMD.tmp` antes de `os.rename(CMD, tmp)` si existe.

---

## 4. Fase 2 — DLL + inyector (el núcleo)

### 2A. Localización de funciones Lua
- **Si hay DLL de Lua** (o el exe exporta): `GetModuleHandleW` + `GetProcAddress`
  para `luaL_loadfile`, `lua_pcall`, `lua_gettop`.
- **Si es estático sin exports**: escanear el `.text` del módulo principal con
  firmas (patrones de LuaJIT). Determinar los patrones en la Fase 0 (se pueden
  sacar comparando con una build conocida de LuaJIT o con el cliente Linux).
  Guardarlos en `tools/agent_loader/signatures.h`.

### 2B. `tools/agent_loader/` (DLL en C/C++)
Objetivo: al cargarse en `pxgme.exe`, ejecutar el agente Lua.

Diseño:
1. **`DllMain(DLL_PROCESS_ATTACH)`**: lanzar un hilo (`CreateThread`); **nunca**
   hacer trabajo pesado dentro de `DllMain`.
2. El hilo:
   a. Lee config `pxg_agent_dll.json` (junto a la DLL):
      `{"agent": "<abs pxg_agent.lua>", "dir": "<abs mydata>", "log": "<abs log>"}`.
   b. Resuelve `luaL_loadfile`/`lua_pcall`/`lua_gettop` (2A o 2B).
   c. **Captura el `lua_State`**: instalar un **hook inline** en `lua_gettop`
      (se llama constantemente por el cliente) que guarde el primer argumento
      (`rcx` en x64) y luego llame al original. Una vez capturado, quitar el hook.
      *(Equivale al `break lua_gettop; $L = $rdi` de gdb en Linux.)*
   d. Con `L`: `lua_pushstring(L, dir)` + `lua_setglobal(L, "PXG_DIR")` (si el
      global existe en esa build), y luego `luaL_loadfile(L, agent)` +
      `lua_pcall(L, 0, 0, 0)`.
   e. Escribe el resultado en el log.
3. **Reinstalable**: el agente cancela su tick anterior al reejecutarse
   (`PXG_GEN`), así que inyectar de nuevo es seguro.

Notas:
- Hooking inline en x64: `VirtualProtect` la página a RWX, escribir un `jmp`
  al trampolín (que guarda `rcx`, restaura los bytes originales y salta de
  vuelta). Alternativa: hookear `lua_pushstring`/`lua_gettop` con una técnica de
  trampolín mínima.
- Si la captura por hook es frágil, alternativa: buscar el global `lua_State`
  de OTClient (suele haber un `g_lua` global) por patrón.

### 2C. `tools/inject_windows.py` (inyector, Python stdlib)
```python
# esquema
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
# OpenProcess(PROCESS_ALL_ACCESS)
# VirtualAllocEx(h, 0, len(path), MEM_COMMIT|MEM_RESERVE, PAGE_READWRITE)
# WriteProcessMemory(h, addr, path_utf16, len, None)
# load = GetProcAddress(GetModuleHandleW("kernel32.dll"), b"LoadLibraryW")
# CreateRemoteThread(h, 0, 0, load, addr, 0, 0); WaitForSingleObject(...)
```
- Argumentos: `--process pxgme.exe` / `--pid`, `--dll <ruta>`, `--agent <ruta>`,
  `--dir <mydata>`. Escribe `pxg_agent_dll.json` y luego inyecta.
- Verificación: comprobar que el `state_file` se actualiza (mtime) tras inyectar.

### 2D. Build de la DLL
- **Cross-compile desde Linux**: `x86_64-w64-mingw32-gcc -shared -o pxg_agent_loader.dll ...`
  (o `i686-...` si el cliente es x86). Ajustar al arch de la Fase 0.
- **En Windows**: MSVC (`cl /LD`) o MinGW.
- Documentar el comando en `tools/agent_loader/README.md`.

---

## 5. Fase 3 — Robustez del agente en Windows

- `readCmd`: `os.remove(CMD.tmp)` antes del `rename` (Windows no sobrescribe).
- `saveMap`/`snapTick`: usar `DIR .. "/"` (forward slashes funcionan en
  `io.open` de Windows) o `DIR .. "\\"`; verificar con el cliente real.
- Codificación: el agente escribe JSON con `cjson` (UTF-8); Python lee UTF-8.
  Verificar que el `DIR` sin caracteres raros no rompa nada.

---

## 6. Fase 4 — Empaquetado

- PyInstaller para `main.py` (bot) y `webui.py` (GUI). El agente `.lua` y la DLL
  van como datos.
- Un lanzador `.bat`/acceso directo que: 1) inyecta la DLL, 2) arranca la GUI.

---

## 7. Fase 5 — Pruebas

- **Tests cross-platform** (ya existen en `tools/test_behaviors.py`, `--mock`):
  deben pasar en Windows sin cambios.
- **E2E**: con el cliente abierto en Windows: inyectar la DLL, verificar que
  `pxg_bot_state.json` se actualiza y que el bot lee el estado; probar `order`,
  `nav`, `loot`, `ball`.
- **Regresión**: el flujo Linux no debe romperse (mantener el default del agente
  y el `attach` Linux).

---

## 8. Riesgos

- **Anti-cheat**: si `pxgme.exe` bloquea `LoadLibrary`/`CreateRemoteThread`, la
  DLL no cargará. Alternativas: *manual mapping*, o volver a un inyector por
  **debugger** (ctypes: `DebugActiveProcess`/`WaitForDebugEvent`/
  `GetThreadContext`) que replique el flujo de gdb.
- **Lua estático sin exports**: hay que sacar firmas; más trabajo y frágil ante
  updates del cliente.
- **x86 vs x64**: la DLL debe coincidir con la arquitectura del cliente.

---

## 9. Orden de ejecución recomendado

1. **Fase 0**: `tools/recon_windows.py` → decidir DLL vs estático y arquitectura.
2. **Fase 1**: rutas/config/`setup` (se puede hacer en paralelo).
3. **Fase 2**: DLL + inyector (según Fase 0).
4. **Fase 3**: robustez del agente.
5. **Fase 4-5**: empaquetado y pruebas.

---

## 10. Preguntas a resolver en la máquina Windows

1. ¿Existe `lua*.dll` en el directorio de `pxgme.exe`? (lo dice el recon)
2. ¿`pxgme.exe` exporta funciones `lua*`? (lo dice el recon)
3. Arquitectura del cliente: x64 o x86.
4. ¿Hay anti-cheat que bloquee `LoadLibrary`? (probar la inyección y ver si
   falla).
5. Directorio `mydata` real del cliente Windows.

---

## 11. Referencias de código (Linux) que se deben espejar

- `tools/install_agent.py` — inyección por gdb (break `lua_gettop`,
  `luaL_loadfile`, `lua_pcall`). **Espejo de lo que hace la DLL.**
- `pxg_bot/setup.py` — `attach`/`detect_mydata`/`install_agent` (a portar).
- `tools/lua_probe.py` — ejemplo de leer memoria del cliente (útil para el recon).
- `pxg_bot/memory.py` — `WindowsProcess` (base para el recon y el inyector).
- `pxg_bot/signatures.py` — escaneo de patrones (para Lua estático).
