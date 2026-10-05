# dkbot — Bot AFK para PokeXGames

Documento de referencia del proyecto: qué es, cómo está construido, qué tiene
implementado y cómo se opera.

---

## 1. Qué es

`dkbot` es un **bot 100% AFK** para **PokeXGames** (un MMO de Pokémon basado en
OTClient). Controla el cliente nativo de Linux (`pxgme-linux`) sin usar la
ventana: el estado del juego se lee y las acciones se ejecutan **dentro del
propio proceso del cliente** mediante un agente Lua inyectado.

Objetivo: farmear (pelear, lotear, capturar shinies, revivir, resetear
cooldowns), siguiendo una **ruta dibujada por el usuario** o explorando, con un
toque "humano" para no parecer un bot.

**Restricción clave del proyecto:** no se usa `xdotool`/X11 para el movimiento
ni los clics normales (todo va por Lua). La única excepción es mover el cursor
físico con XTest para el revive (ver §8), porque el ítem se aplica sobre el
"Thing" que hay bajo el cursor y eso no se puede fijar desde Lua.

---

## 2. Arquitectura

```
┌─────────────────────────────────────────────────────────────┐
│  main.py  (CLI: run / gui / scan / pointer / dump / procs)   │
└───────────────┬───────────────────────────┬─────────────────┘
                │                           │
        ┌───────▼────────┐          ┌───────▼─────────┐
        │  Bot (Python)  │          │  GUI web (Python)│
        │  bucle/tick    │          │  http.server     │
        │  + behaviors   │          │  :8765           │
        └───┬────────┬───┘          └──────────────────┘
            │        │
   ┌────────▼──┐  ┌──▼──────────┐
   │ StateSource│  │  Input      │
   │ (Lua/Mock/ │  │ (Lua/Mock)  │
   │  Memory)   │  └──┬──────────┘
   └────────┬───┘     │
            │         │  ficheros de estado/comandos
            │         │  (mydata/pxg_bot_*.json|txt)
        ┌───▼─────────▼─────────────────────────────┐
        │  Cliente pxgme-linux                        │
        │   └── agente Lua (tools/pxg_agent.lua)      │
        │        · snapshot del estado (state_file)   │
        │        · ejecuta comandos (cmd_file)        │
        └─────────────────────────────────────────────┘
```

- **Bot Python**: bucle de decisión. Cada *tick* lee el estado, elige **un**
  behavior (por prioridad) y ejecuta su acción.
- **Agente Lua**: vive dentro del cliente. Publica el estado en un JSON
  (`state_file`) y consume comandos de un fichero (`cmd_file`). También hace de
  *pathfinder* y ejecuta clics/hotkeys/skills por Lua.
- **GUI web**: dashboard para arrancar/parar el bot, ver estado/mapa/ruta,
  editar config y listas (skills, ignorados).

Comunicación Python ↔ cliente: **ficheros** (JSON/TXT), no red. El canal de
comandos se drena de forma **atómica** (`rename` + lectura) para no perder
órdenes.

---

## 3. Cómo funciona (flujo por tick)

1. `Bot.tick()` → `source.read_state()` construye un `GameState`.
2. `refresh_control()` aplica cambios del canal de control (pausa, toggles,
   ruta, explore, combate).
3. Se recorren los **behaviors ordenados por prioridad**; el primero cuyo
   `evaluate(state)` devuelve `True` ejecuta su `act(state)` y se detiene el
   tick.
4. El humano (humanizer) puede meter micro-pausas aleatorias entre acciones.
5. Se escribe `status_file` (estado del bot) y se duerme `tick_seconds`.

El tick por defecto es **0.05 s**.

### Fuentes de estado (`settings.state_source`)

- `lua` (por defecto): lee el `state_file` que escribe el agente.
- `memory`: lee el proceso con `pxg_bot/memory.py` + offsets/signaturas
  (legado; no es el camino principal).
- `mock`: estado simulado para tests (`pxg_bot/mock.py`).

### Entrada (`Input`)

- `LuaInput`: traduce las acciones a comandos del agente (`nav`, `spell`,
  `loot`, `clickslot`, `revive`, `pokestop`, …).
- `MockInput`: no-op para tests.

---

## 4. Modelo de estado (`pxg_bot/models.py`)

`GameState` es la foto de un tick. Lo más relevante:

- `player` (pos, hp, dir…), `creatures[]`, `party[]` (slots + hp), `moves[]`
  (skills del Pokémon activo con `key`, `pct` de cooldown, `aoe`, `effect`).
- `active_pokemon_name`, `skill_order` (combo por Pokémon), `attacking_name`.
- `spawn_blocked`, `captured`, `defeated[]` (cuerpos detectados),
  `server_msgs[]`.
- `visible` (rectángulo visible), `camera`, `tile_size`, `map_rect`.
- `slot_pos`/`win_pos` (para el revive), `nav_result`, `pokemon_pos`,
  `pokemon_hp` (posición/HP del **pokémon propio / ownsummon**).

`Creature.attackable` decide si una criatura es atacable (no self/npc/summon/
ignorada, hp>0, monstruo o salvaje). `Vec3.distance` usa **Chebyshev** (max de
Δx,Δy).

---

## 5. Behaviors (`pxg_bot/behaviors.py`)

Se eligen por prioridad (mayor primero). Si uno actúa, el resto del tick se
salta.

| Prioridad | Behavior   | Qué hace |
|----------:|------------|----------|
| 100 | `crisis`    | Desconexión, jugador cerca, HP crítica, huida/logout. |
| —   | `healing`   | Cura jugador/Pokémon con sus teclas. |
| —   | `capture`   | Lanza balls a cuerpos (shiny / catch_all). |
| 88  | `loot`      | Camina al **mejor tile** y lootea (`e`). |
| 80  | `revive`    | Revive/resetea cooldowns y **bloquea la ruta** hasta lograrlo. |
| 70  | `combat`    | Lure+Pokestop+AoE, objetivo, spawn bloqueado, pánico. |
| 10  | `route`     | Sigue la ruta dibujada (determinista). |
| 5   | `explore`   | Exploración/patrulla con el minimapa. |

(El orden real se fija en `build_behaviors`, que ordena por `priority`.)

### 5.1 Combate (`CombatBehavior`)

- **Lure con Pokestop + AoE** (`lure_aoe`): camina la ruta hasta juntar
  `lure_visible_min` enemigos visibles → **pokestop** (los deja quietos) →
  espera a que se acerquen → lanza **AoE** en el orden configurado → reanuda.
- **Pokestop**: se dispara con `game_pokemon.pokeStop()` (config
  `pokestop_method`: `func` / `talk` / `hotkey`). Ver §9.
- **Orden de skills**: respeta el combo por Pokémon que editas en la GUI
  (`state.skill_order`). Si no hay orden, usa shuffle humano / round-robin.
- **Gate de ataque**: solo ataca cuando **todos** los enemigos visibles están a
  ≤ `attack_range` (2) **del pokémon propio** (ownsummon). Con `panic_hp` (20%)
  de vida del pokémon, lanza **todas** las skills y se salta el gate y el
  mínimo del lure.
- **Spawn bloqueado**: si el servidor no deja atacar, retrocede, espera y
  vuelve; si persiste, blacklistea el objetivo.
- **Objetivo inatacable**: si no baja la vida del objetivo, lo añade a la lista
  de ignorados por id (ver §7).

### 5.2 Loot (`LootBehavior`)

- Elige el **tile óptimo** (cubre más cuerpos con Chebyshev 1; empate → el más
  cercano; se filtra por alcanzable). El agente tiene su propio `bestCoverTile`
  (con la caminabilidad real del cliente) y el bot calcula `bopt` como
  verificación.
- Lootea con la tecla `e` (`g_game.collectLoot()`), que recoge todos los
  cuerpos adyacentes de una vez.
- **Timer de fase** (`phase_secs`, 5 s): desde que empieza a lootear; al
  expirar abandona lo pendiente y retoma la ruta.
- El **tile del pokémon propio cuenta como caminable** para el loot.

### 5.3 Revive (`ReviveBehavior`)

Condiciones (en `_need`):
1. **Obligatorio antes de retomar la ruta**: `revive_pending` bloquea
   `RouteBehavior` (orden: loot → revive → route).
2. **Ya usó alguna skill** (alguna AoE en cooldown), o
3. **El pokémon propio está debilitado** (`hp ≤ 0`).

Extras:
- **Bypass por STUN**: si se usó una skill de `stun` (en cooldown), se puede
  revivir **aunque haya pokemones en pantalla**.
- **Intervalo mínimo** (`min_interval_secs`, 8 s) para no revivir en bucle.
- Solo se ejecuta con la pantalla sin enemigos (salvo el bypass por stun).

Flujo (`act`): **retirar (guardar) → revive (XTest al slot + ítem 2269) →
sacar (call slot)**. Ver §8.

### 5.4 Ruta (`RouteBehavior`)

- Sigue los waypoints dibujados de forma **determinista** (sin micro-desvíos;
  `route.detour=false`). Idle de arranque determinista.
- **Segmentación fija**: fija un objetivo a ≤ `max_nav_dist` y lo recalcula
  solo al acercarse (`seg_recalc_dist`), para no reiniciar el nav del agente.
- Salta waypoint si no progresa (`progress_stuck_secs`) o está atascado
  (`stuck_secs`).

### 5.5 Exploración (`ExploreBehavior`)

- Usa el minimapa (`otmm`) para cubrir zonas, fronteras y patrullar; persiste
  el mapa explorado en `pxg_world.json`.

---

## 6. Navegación (agente Lua + bot)

- El bot envía `nav x y z` (o `navpath`). El agente mantiene una navegación
  **persistente** (`PXG_NAV`) sin que Python reenvíe cada paso.
- **Pathfinding** (`findPathDirs`): primero el `g_map.findPath` del cliente; si
  devuelve vacío (falla con obstáculos), usa un **BFS propio** sobre los tiles
  caminables (`bfsPathDirs`). Así se evita el *greedy* que oscilaba.
- **`nearestWalkable`**: si el destino pedido no es caminable, se ajusta al
  tile caminable más cercano.
- **Modo `auto`**: `g_game.autoWalk` del cliente conduce el path; no se recalcula
  cada tick.
- El **tile del pokémon propio** se considera caminable (`PXG_POKE_POS`).

---

## 7. Ignorados (criaturas inatacables)

El cliente no distingue NPCs/pokémon de NPC (comparten `isMonster` y a veces
outfit). Se usa una lista persistente (`pxg_ignore.json`):
- **Nombres** (p. ej. un NPC como "Andrea").
- **Ids de criatura** (instancias concretas), **auto-aprendidos** cuando el bot
  ataca y no hace daño tras unos segundos (no afecta a salvajes de la misma
  especie, que sí reciben daño).

`Creature.attackable` excluye las ignoradas. Editable desde la GUI y con
recarga en caliente (mtime).

---

## 8. Revive: por qué usa XTest

El revive es un ítem (id **2269**) que se aplica con
`g_gameActions.useInventoryItemWith(item, ThingBajoElCursor)`. El `Thing` se
obtiene de `g_gameActions.getThingByMousePosition()`, es decir, **el cursor
físico tiene que estar sobre el slot del Pokémon**. Como desde Lua no se puede
fijar la posición del cursor, se mueve el puntero con **XTest** (`libXtst`) solo
para esto (`pxg_bot/x11mouse.py`), y el agente aplica el ítem.

Pasos: retirar el Pokémon (debe estar guardado) → mover cursor al slot →
`useInventoryItemWith(2269, thing)` → sacar el Pokémon.

---

## 9. Pokestop

El "pokestop" detiene a las criaturas. El botón del action bar **no** tiene una
tecla "R"; el mecanismo real es **`game_pokemon.pokeStop()`** (envía
`!pokestop`). El agente expone el comando `pokestop` con métodos
configurables (`combat.pokestop_method`): `func` (pokeStop), `talk`
(`!pokestop`), `hotkey` (una tecla).

---

## 10. Detección de cuerpos (defeated)

El agente rastrea criaturas (`PXG_TRACK`). Una criatura cuenta como **derrotada**
si desaparece y además:
- estaba en la **lista de batalla del cliente** (la estábamos peleando), o
  estaba a ≤2 tiles, y
- desapareció dentro de ~2.5 s y su última posición era ≤4 tiles.

Así se evitan "cuerpos falsos" (criaturas que se alejan de la vista).

---

## 11. Shiny / captura

- `pxg_bot/shiny.py`: tabla persistente `nombre → outfit normal` (el más
  observado). Una criatura con outfit distinto se marca `shiny`.
- `capture` lanza la ball (`F4`) a los cuerpos shiny o a todos si
  `capture.catch_all`.

---

## 12. Humanizer (anti-ban)

`pxg_bot/humanizer.py`: perfiles `light`/`normal`/`aggressive` con jitter en
tiempos y teclas, micro-pausas (solo en idle), micro-desvíos, "mirar
alrededor", shuffle de skills, varianza de objetivo y descansos de sesión. Todo
configurable por `settings.humanizer.overrides`.

---

## 13. GUI web (`pxg_bot/webui.py` + `pxg_bot/web/`)

Servidor `http.server` en `192.168.1.52:8765` (configurable). Arranca/para el
bot, muestra estado/log/mapa/ruta y edita config.

Endpoints:
- `GET /api/state`, `/api/log`, `/api/config`, `/api/world`, `/api/otmm`,
  `/api/otmm/info`, `/api/route`, `/api/routes`, `/api/pokemon_skills`,
  `/api/ignore`
- `POST /api/bot` (start/stop), `/api/command`, `/api/control`, `/api/config`,
  `/api/route`, `/api/routes`, `/api/routes/delete`,
  `/api/pokemon_skills/order`, `/api/ignore`

Tarjetas principales: estado, control (start/stop/pausa/panic), toggles de
behaviors, mapa real (base otmm, pan/zoom, transitabilidad), editor de ruta,
**Skills** (combo por Pokémon, reordenable) e **Ignorar criaturas**.

---

## 14. Configuración (`config.json` + `pxg_bot/config.py`)

- `process_name`, `settings` (`tick_seconds`, `state_source`, humanizer…),
  `lua` (rutas de los ficheros de estado/cmd/control/world/shiny/routes/
  pokemon_skills/ignore/minimap).
- `behaviors`: `crisis`, `healing`, `capture`, `combat`, `loot`, `route`,
  `explore`, `revive` (ver §5 para el detalle de cada uno).

Los valores por defecto están en `pxg_bot/config.py`; `config.json` los
sobreescribe. La GUI edita `config.json` y el canal de control
(`pxg_control.json`) para cambios en caliente.

---

## 15. Ficheros de persistencia (`.../pxg-linux/mydata/`)

| Fichero | Contenido |
|---|---|
| `pxg_bot_state.json` | Snapshot del juego (lo escribe el agente). |
| `pxg_bot_cmd.txt` | Comandos hacia el agente (drenado atómico). |
| `pxg_control.json` | Control en caliente (pausa, toggles, ruta, combate…). |
| `pxg_bot_status.json` | Estado del bot (behavior elegido, uptime…). |
| `pxg_bot.pid` | PID del bot. |
| `pxg_world.json` | Mapa explorado / cobertura. |
| `pxg_shiny.json` | Tabla nombre→outfit normal. |
| `pxg_routes.json` | Rutas guardadas. |
| `pxg_pokemon_skills.json` | Skills por Pokémon + orden de combo. |
| `pxg_ignore.json` | Lista de ignorados (nombres + ids). |
| `minimap.otmm` | Minimapa del cliente (lo lee el bot). |

---

## 16. Herramientas (`tools/`)

- `install_agent.py`: **instala/recarga el agente Lua** en el cliente vía gdb
  (`luaL_loadfile`+`lua_pcall`). **Obligatorio tras cada reinicio del cliente.**
- `lua_probe.py`: ejecuta un chunk Lua dentro del cliente y lee `_G.PXG_DUMP`
  (diagnóstico).
- `pxg_agent.lua`: el agente (estado + comandos + navegación).
- `record_route.py`, `e2e_test.py`, `fake_client.py`, `find_by_name.py`,
  `pxg_recon.py`: apoyo (grabar rutas, tests, reconocimiento).

---

## 17. Comandos del agente (`tools/pxg_agent.lua`)

`walk`, `nav`, `navpath`, `standnear`, `standnearmany`, `stop`, `turn`,
`lclick`, `rclick`, `mclick`, `attackat`, `cancelattack`, `spell`, `hotkey`,
`key`, `loot`, `pokestop`, `revive`, `clickslot`, `callslot`, `useinv`, `say`,
`scanmap`, `savemap`, `thingat`, `cover`, `debug*`.

---

## 18. Cómo ejecutar

```bash
# Instalar dependencias (pocas; ver requirements.txt)
pip install -r requirements.txt

# Instalar/recargar el agente en el cliente (tras cada reinicio del cliente)
python3 tools/install_agent.py

# Arrancar el bot (AFK)
python3 main.py run --verbose

# Arrancar la GUI web
python3 main.py gui --no-open
# -> http://192.168.1.52:8765
```

Otros subcomandos: `scan`, `pointer`, `dump`, `procs`.

---

## 19. Notas y limitaciones

- **El agente se pierde al reiniciar el cliente**: hay que reinstalarlo
  (`tools/install_agent.py`).
- **X11/XTest** solo para el cursor del revive; el movimiento y los clics
  normales son 100% Lua.
- El cliente usa **LuaJIT (Lua 5.1)**: evitar sintaxis de Lua 5.4 en el agente
  (p. ej. `//`).
- La detección de NPCs/pokémon de NPC no es directa; se resuelve con la lista de
  ignorados + auto-aprendizaje.
- El `minimap.otmm` puede reescribirse mientras el bot lo lee; la lectura de
  bloques tolera fallos de descompresión.
- Algunos umbrales (revive, loot, combate) son heurísticos y afinables por
  config.
