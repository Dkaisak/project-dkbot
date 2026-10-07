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
ni los clics normales (todo va por Lua). **Ni `order` ni `revive` usan X11**:
`order` se resuelve por coordenadas (`tileToScreen` + `getMapThingByMousePosition`
+ `game_actionbar.queueAction`) y `revive` construye el `Thing` del slot
(`Pokeball` por `pokeId`), ambos sin mover el cursor. `pxg_bot/cursor.py`
(**XTest** en Linux / **`SetCursorPos`** en Windows) queda solo como utilidad
(ver §8 y §5.6).

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

1. `Bot.tick()` → `source.read_state()` construye un `GameState` (si falla, se
   trata como desconexión y no mata el bot).
2. `bb.update_corpses(state)`: cosecha los cuerpos derrotados (`defeated` →
   `corpses_pending`) **siempre**, sin depender de la prioridad de los behaviors.
3. `refresh_control()` aplica cambios del canal de control (pausa, toggles,
   ruta, explore, combate, `order`).
4. Se recorren los **behaviors ordenados por prioridad**; el primero cuyo
   `evaluate(state)` devuelve `True` ejecuta su `act(state)` y se detiene el
   tick.
5. El humano (humanizer) puede meter micro-pausas aleatorias entre acciones.
6. Se escribe `status_file` (estado del bot) y se duerme `tick_seconds`.

El tick por defecto es **0.05 s**. La lectura del `state_file` se **cachea por
`mtime`**: el agente lo reescribe cada ~200 ms, así que el bot no reparsea el
JSON en los ticks intermedios.

### Fuentes de estado (`settings.state_source`)

- `lua` (por defecto): lee el `state_file` que escribe el agente.
- `memory`: lee el proceso con `pxg_bot/memory.py` + offsets/signaturas
  (legado; no es el camino principal).
- `mock`: estado simulado para tests (`pxg_bot/mock.py`).

### Entrada (`Input`)

- `LuaInput`: traduce las acciones a comandos del agente (`nav`, `spell`,
  `loot`, `clickslot`, `reviveslot`, `pokestop`, …).
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
- `slot_pos`/`win_pos` (posiciones de la UI), `nav_result`, `pokemon_pos`,
  `pokemon_hp` (posición/HP del **pokémon propio / ownsummon**), `fight_mode`
  (1=Ofensivo, 2=Equilibrado, 3=Defensivo).

`Creature.attackable` decide si una criatura es atacable (no self/npc/summon/
ignorada, hp>0, monstruo o salvaje). `Vec3.distance` usa **Chebyshev** (max de
Δx,Δy).

---

## 5. Behaviors (`pxg_bot/behaviors.py`)

Se eligen por prioridad (mayor primero). Si uno actúa, el resto del tick se
salta.

| Prioridad | Behavior   | Qué hace |
|----------:|------------|----------|
| 100 | `crisis`    | Desconexión, jugador cerca, HP crítica, huida/logout. El toggle `enabled` desactiva **todo**. |
| 90  | `healing`   | Cura jugador/Pokémon. **Deshabilitado** por defecto (teclas de curación aún no implementadas). |
| 88  | `loot`      | Camina al **mejor tile** (o al cuerpo) y lootea con `collectLoot`. |
| 85  | `capture`   | Lanza balls a cuerpos (shiny / catch_all) por **Lua** (ítem, sin cursor). |
| 80  | `revive`    | Revive/resetea cooldowns y **bloquea la ruta** hasta lograrlo. |
| 70  | `combat`    | Lure+Pokestop+AoE, objetivo, spawn bloqueado, pánico, *fight-mode*. |
| 12  | `summon`    | Ordena el ownsummon cerca del player (con vecinos libres). |
| 10  | `route`     | Sigue la ruta dibujada (determinista, densificada sobre otmm). |
| 5   | `explore`   | Exploración/patrulla con el minimapa. |

(El orden real se fija en `build_behaviors`, que ordena por `priority`.)

### 5.1 Combate (`CombatBehavior`)

- **Lure con Pokestop + AoE** (`lure_aoe`): máquina `idle → hold → fight →
  resume`.
  - **idle**: camina la ruta hasta juntar **`lure_visible_min` (X) enemigos
    visibles** en pantalla, con **tope de tiempo `lure_gather_timeout`** (10 s):
    el contador arranca al aparecer el **primer enemigo** (`vis ≥ 1`) y se
    **resetea** al quedarte sin enemigos. Si vence el tope sin llegar a X, pasa a
    `hold` igualmente (con lo que haya); el gate estricto sigue decidiendo el
    casteo. `lure_gather_timeout=0` = sin tope. Configurable en la GUI ("Lure tope").
  - **hold**: para la navegación y ordena el ownsummon (`lure_summon`). **No
    ataca.** Pasa a `fight` cuando el ownsummon **llegó** al tile del `order`
    (a ≤ `summon_arrive_tolerance` tiles, o tras `summon_grace_secs` si no llega
    exacto) **y todos los de pantalla están a `attack_range` (R)**; entonces
    lanza **pokestop** (para fijarlos) y pelea. Si vence `hold_timeout` sin
    lograrlo → `resume` (abandona y sigue la ruta; **no ataca**).
  - **fight**: castea el combo con el gate estricto. Si el gate deja de
    cumplirse más de `fight_recover_secs` → vuelve a `hold` a reagrupar. Sin
    enemigos → `resume`.
  - **resume**: clic central, limpia estado, pausa `resume_pause_secs` → `idle`.
  - Única excepción al gate: **`panic_hp`** (vida baja del Pokémon).
- **Buff en el lure**: lanza **una vez** la skill de buff (efecto con componente
  `buff`, p. ej. `buff/nevermiss`), pero respetando el gate (no sale hasta que
  todos estén a rango, salvo `panic_hp`).
- **Summon en el lure** (`lure_summon`): en `hold` ordena el ownsummon a un tile
  con sus 8 vecinos libres (`lure_summon_radius`); el pokestop espera a que
  llegue. Ver §5.6.
- **Fight-mode** (`manage_fight_mode`): fuera del burst pone **Defensivo** (3) y
  al lanzar skills **Ofensivo** (1), con un pequeño delay (`fight_mode_delay`)
  antes del primer AoE para que el servidor aplique el modo.
- **Pokestop**: se dispara con `game_pokemon.pokeStop()` (config
  `pokestop_method`: `func` / `talk` / `hotkey`). Ver §9.
- **Ritmo de casteo** (`cooldown`, por defecto **0.3 s**): intervalo mínimo entre
  lanzamientos de skills. Antes estaba en `0.0` y el bot lanzaba una skill **cada
  tick** (0.05 s), más rápido de lo que el cliente procesa → se perdían skills.
  La GUI lo expone como **"Skill cada"**. `skill_repeat_guard` evita repetir la
  misma skill mientras el cliente no refleja su cooldown.
- **Orden de skills**: respeta el combo por Pokémon que editas en la GUI
  (`state.skill_order`), tanto en AoE como en skills de daño. Si no hay orden,
  usa shuffle humano / round-robin. Por defecto el lure pega **solo en área**
  (`lure_use_single=false`); actívalo si quieres incluir las de daño no-AoE del
  combo como relleno cuando las AoE están en cooldown.
- **Combo del lure** (`state.lure_order`, editable en la GUI): si marcas skills
  para un Pokémon, el lure usa **exactamente esas skills, en ese orden**, en cada
  fase de pelea (antes del revive) e ignora `lure_use_single`. Sin combo propio,
  aplica la lógica anterior (AoE por defecto).
- **Sin target**: **no se fija objetivo en ningún combate** (no se usa
  `attackat`); las skills se lanzan directas (`spell <key>`). Aplica al burst AoE
  y también al combate dirigido (Pokémon sin AoE). El único clic sobre criaturas
  que queda es el de loot/captura (derecho), que no es targeting de combate.
- **Gate de ataque**: **por defecto no lanza hasta que TODOS los enemigos en
  pantalla estén a ≤ `attack_range` del pokémon propio** (`require_all_close=true`;
  nunca se usa el player como referencia). La **única excepción es `panic_hp`**:
  con el Pokémon a ese % de vida o menos, lanza todas las skills y se salta el
  gate. El conjunto "en pantalla" es toda la cámara (`engage_radius=0`); con
  `engage_radius>0` se limita a ese radio del summon. Si pones
  `require_all_close=false`, usa el criterio relajado "al menos `cast_min_in_range`
  enemigos a rango". Si no se conoce el summon, no ataca. `ready_pct` es el % de
  cooldown para considerar una skill lista.
- **Spawn bloqueado**: si el servidor no deja atacar, retrocede, espera y
  vuelve; si persiste, blacklistea el objetivo.
- **Objetivo inatacable**: si no baja la vida del objetivo, lo añade a la lista
  de ignorados por id (ver §7).

### 5.2 Loot (`LootBehavior`)

- Los cuerpos se cosechan **siempre** (`update_corpses`), independientes de la
  prioridad: el agente reporta cada derrotado un solo tick.
- Elige el **tile óptimo** (cubre más cuerpos con Chebyshev 1; empate → el más
  cercano).
- **Navega con el astar del bot (otmm)** al mejor tile y lo manda con `navpath`
  (camino real); si no hay otmm, cae a `standnear`/`standnearmany` del agente.
- **Camina a cuerpos lejanos siempre**, haya o no enemigos.
- Lootea con `g_game.collectLoot()` (recoge todos los cuerpos adyacentes de una
  vez).
- **Timer de fase** (`phase_secs`, 10 s): desde que empieza a lootear; al
  expirar abandona lo pendiente y retoma la ruta.
- El **tile del pokémon propio cuenta como caminable** para el loot.

### 5.3 Revive (`ReviveBehavior`)

Condiciones para revivir (resetear cooldowns / revivir):
- Dispara el "hace falta" cuando el Pokémon del slot está **debilitado** o
  **alguna skill del combo está en cooldown** (`combo` = `lure_order` si hay, si
  no `skill_order`, si no las AoE).
- A partir de ahí, revive en estos casos (referencia: suma de `attack_range` del
  combate; `in_range` = enemigos atacables vivos a esa distancia del summon):

| Caso | Condición | Esperas | Modo |
|---|---|---|---|
| **Dead** | slot `hp ≤ 0` | ninguna | rápido |
| **A** | `in_range == 0` (rango de ataque despejado) | `after_aoe_wait` + `min_interval` | normal |
| **B** | `in_range > 0` pero **stun** activo (`stun_secs`) | `after_aoe_wait` + `stun_min_interval` | rápido |
| **C** | **combo agotado** (ninguna skill lista) | solo `fast_min_interval` | rápido |
| **D** | `in_range > 0`, sin stun, con skills listas | — | **no revive** (sigue casteando) |

Extras:
- **Sin revives → desconectar** (`revive.disconnect_when_out`): al llegar a **0
  revives** (ítem `revive.item`, por defecto 2269), el bot pulsa la tecla de
  logout (`revive.logout_key`, F12), pausa y **se detiene**. Cuenta los revives
  desde `bag_counts` del agente (requiere el backpack abierto); si no hay datos
  fiables, no actúa. Editable en la GUI (tarjeta *Revive*).
- **Sin doble revive (guarda de recuperación)**: tras completar una secuencia, no
  arranca otra hasta que el estado del juego confirme que surtió efecto
  (`revive_ready`: Pokémon **vivo** y **combo sin cooldowns**). Evita el doble
  revive por el retraso del snapshot del agente. Si nunca se recupera, reintenta
  tras `revive_retry_secs`.
- **Slot**: usa el slot del **Pokémon elegido para la ruta** (selector en la
  tarjeta *Equipo*); si no hay selección, cae a `revive.slot` de config. Así el
  revive siempre va al Pokémon que está trabajando en la ruta.
- **Rango de ataque, no pantalla**: el gate es por `attack_range` del summon; un
  enemigo lejano en cámara **ya no bloquea**.
- **Stun con ventana** (`stun_secs`, 2.5 s): se registra al castear una skill con
  `stun` en `effect` (`last_stun_at`); mientras dure, se considera al enemigo
  aturdido y se puede revivir con vivos en rango.
- **Combo agotado** (caso C): si no queda skill del combo para lanzar, revive
  igual aunque haya enemigos vivos, **lo más rápido posible** (salta
  `after_aoe_wait`, usa `fast_min_interval` y los tiempos `urgent_*`).
- **Rápido** = tiempos `urgent_click_delay`/`urgent_verify_delay`/
  `urgent_withdraw_wait`, sin `first_revive_wait_secs`. Loot y captura ceden
  (`revive_urgent` / `revive_fast`) para no robarle el turno.
- **Espera tras el último AoE** (`after_aoe_wait_secs`, 1 s): en los casos
  normales/stun no arranca hasta 1 s después del último AoE (no en Dead/C).
- **Intervalo mínimo** (`min_interval_secs`, 8 s) en A; `stun_min_interval_secs`
  y `fast_min_interval_secs` en B/C.
- Una secuencia **ya iniciada se completa siempre** (no se cancela por enemigos:
  cancelarla dejaba al Pokémon retirado y sin revivir).
- **Atómica**: loot y captura ceden mientras hay una secuencia en curso.

Flujo (`act`): **retirar (guardar) → revive (ítem 2269 al slot por `pokeId`,
sin cursor) → sacar (call slot)**. El **primer** revive de la sesión espera
`first_revive_wait_secs` **antes de guardar**. Ver §8.

### 5.4 Ruta (`RouteBehavior`)

- Sigue los waypoints dibujados de forma **determinista** (sin micro-desvíos;
  `route.detour=false`).
- **Idle de inicio** (`start_idle_enabled`, `start_idle_seconds`): al volver al
  **waypoint 0** (después del primer loop) espera `start_idle_seconds` (por
  defecto `[25,60]`; con `start_idle_random=false` usa la media). Se puede
  **activar/desactivar** y ajustar el tiempo desde la GUI (tarjeta *Exploración*,
  control "Idle inicio"); se aplica en caliente.
- **Pokémon de la ruta**: si en la GUI marcas un Pokémon para la ruta, al
  iniciarla el bot comprueba si **ese slot ya está out** (el agente lo marca con
  `isPokemonActive(pokeId)`); si ya está out **lo deja como está** (no hace
  click), y si no, lo saca con `callslot`. Desde ahí todo (lure, revive, combos)
  trabaja con él. Si no hay nombre, usa el slot guardado. La selección se guarda
  por ruta (control y rutas con nombre).
- **Densificada** (`route.densify`): al cargar la ruta, cada tramo se sustituye
  por el **camino real sobre el otmm** (astar), para que siga el corredor.
- **Segmentación por camino real**: el objetivo de navegación se calcula con
  **astar sobre otmm** (no en línea recta), así no se da la vuelta al cruzar el
  eje de un waypoint.
- Salta waypoint si no progresa (`progress_stuck_secs`) o está atascado
  (`stuck_secs`).

### 5.5 Exploración (`ExploreBehavior`)

- Usa el minimapa (`otmm`) para cubrir zonas, fronteras y patrullar; persiste
  el mapa explorado en `pxg_world.json`.

### 5.6 Summon / Order (`SummonBehavior`)

- Mantiene el **ownsummon** cerca del player: elige el tile más cercano al
  player cuyos **8 vecinos estén libres** (caminables en el otmm, sin criaturas)
  y lo ordena con **`order`**. El tile está **siempre a ≥ 2 tiles del personaje**
  (nunca encima ni a 1); `summon_near_tile` arranca el radio en 2.
- `evaluate` actúa si el pokémon está lejos del player (`max_dist`) o sus
  vecinos no están libres, con un `interval_secs` mínimo.
- El **lure** también lo usa antes de pelear (`combat.lure_summon`, ver §5.1).
- `order [x y z]` es un comando del agente y **no usa X11**: replica
  `orderPokemon()` pero tomando el `Thing` por coordenadas de pantalla
  (`tileToScreen` + `getMapThingByMousePosition`) y llamando
  `game_actionbar.queueAction(item Order, thing, nil, true)`. Sin coords cae al
  modo clásico (Thing bajo el cursor). `orderonself` usa `g_game.useWith(item,
  g_localPlayer)` (tampoco usa cursor). El control acepta `order: [x,y,z]`.

---

## 6. Navegación (agente Lua + bot)

- El bot envía `nav x y z` (o `navpath`). El agente mantiene una navegación
  **persistente** (`PXG_NAV`) sin que Python reenvíe cada paso.
- **Pathfinding** (`findPathDirs`): primero el `g_map.findPath` del cliente; si
  devuelve vacío (falla con obstáculos), usa un **BFS propio** sobre los tiles
  caminables (`bfsPathDirs`, radio 48 / 30000 nodos).
- **Bot con otmm**: para loot y ruta el bot calcula el **camino real con astar
  sobre el minimapa** (`WorldMap.plan_to`) y lo manda con `navpath`.
- **Modo `step`**: no avanza el índice si `g_game.walk` falla; reintenta y, si
  no hay ruta, corta la nav (antes saltaba pasos).
- **Modo `greedy`**: con contador de fallos; tras varios bloqueos corta la nav
  (antes oscilaba indefinidamente).
- **`nearestWalkable`**: si el destino pedido no es caminable, se ajusta al
  tile caminable más cercano.
- **Modo `auto`**: `g_game.autoWalk` del cliente conduce el path.
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

## 8. Revive (sin cursor)

El revive es un ítem (id **2269**) que se aplica al slot del Pokémon. El cliente
(`getThingByMousePosition`) obtiene el `Thing` del slot bajo el cursor como un
`Pokeball` con `setId(pokeId)`; el agente lo **construye directamente**
(`reviveslot <slot> <item>`): lee `pokeId` del widget
(`getPokemonWidgetByIndex`) y llama
`g_gameActions.useInventoryItemWith(2269, Pokeball)` — **sin mover el cursor
físico** (no necesita X11).

Pasos: retirar el Pokémon (debe estar guardado) → `reviveslot` → sacar el
Pokémon (`callslot`). Todo por Lua.

> `pxg_bot/cursor.py` (XTest / `SetCursorPos`) queda solo como utilidad; el flujo
> normal del bot ya no lo usa.

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
  - **No cuenta el summon propio** (`is_summon`) ni duplica por tick (dedupe por
    instancia), para no invertir la tabla.
- `capture` lanza la ball a los cuerpos shiny o a todos si `capture.catch_all`.
  `capture.range` (4): lanza desde **≤ 4 tiles**; si el cuerpo está más lejos, se
  acerca primero (antes estaba en 1, ahora puede lanzar desde 4).
- La ball se lanza **por Lua**: `ball <itemId> x y z` obtiene el `Thing` del
  tile con `getCreatureOrMapThingByMousePosition` (sin mover el cursor) y aplica
  `useInventoryItemWith(itemId, thing)`. El ítem (p. ej. **2652**) es
  `capture.ball_item`. `interval=0` → lanza cada tick.

---

## 12. Humanizer (anti-ban)

`pxg_bot/humanizer.py`: perfiles `light`/`normal`/`aggressive` con jitter en
tiempos y teclas, micro-pausas (solo en idle), micro-desvíos, "mirar
alrededor", shuffle de skills, varianza de objetivo y descansos de sesión. Todo
configurable por `settings.humanizer.overrides`.

---

## 13. Interfaz (`pxg_bot/webui.py` + `pxg_bot/web/` + `pxg_bot/app.py`)

Servidor `http.server` en `127.0.0.1:8765` (configurable). Arranca/para el bot,
muestra estado/log/mapa/ruta y edita config. La misma UI se puede abrir en el
navegador (`gui`) o en una **ventana nativa** de escritorio (`app`, pywebview +
bandeja del sistema); ver §18.

Endpoints:
- `GET /api/state`, `/api/log`, `/api/config`, `/api/world`, `/api/otmm`,
  `/api/otmm/info`, `/api/route`, `/api/routes`, `/api/pokemon_skills`,
  `/api/ignore`
- `POST /api/bot` (start/stop), `/api/command`, `/api/control`, `/api/config`,
  `/api/route`, `/api/routes`, `/api/routes/delete`,
  `/api/pokemon_skills/order`, `/api/pokemon_skills/lure_order`, `/api/ignore`

Tarjetas principales: estado, control (start/stop/pausa/panic), toggles de
behaviors (crisis/combat/loot/revive/explore; **sin curación**), ajustes de
combate (lure min, en rango, rango ataque, rango engage, HP pánico,
skill lista %, solo todos a rango, idle inicio), **Equipo**
(slots con HP y activo, y selector del Pokémon de la ruta, que se saca al
iniciarla), mapa real (base otmm, pan/zoom, transitabilidad), editor de ruta,
**Skills / combo del lure** (orden por Pokémon + skills marcadas para cada lure,
y **✕** para borrar del catálogo; el Pokémon activo se resalta y no se puede
borrar) e **Ignorar criaturas**. El panel lee la `party` del estado **en tiempo real** (slots con HP y
flag de activo) para el selector de Pokémon. El cliente no expone el nombre por
slot: se muestra "Slot N" y el panel **aprende** el nombre cuando ese Pokémon
está activo (se guarda en el navegador). La tarjeta *Estado* muestra además
**contadores** de la sesión (kills, loots, looted, balls, capturas, revives,
skills, pokestops, lures), tomados del `status_file`.

---

## 14. Configuración (`config.json` + `pxg_bot/config.py`)

- `process_name`, `settings` (`tick_seconds`, `state_source`, humanizer…),
  `lua` (rutas de los ficheros de estado/cmd/control/world/shiny/routes/
  pokemon_skills/ignore/minimap).
- `behaviors`: `crisis`, `healing`, `capture`, `combat`, `loot`, `revive`,
  `summon`, `route`, `explore` (ver §5 para el detalle de cada uno).

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
| `pxg_routes.json` | Rutas guardadas (waypoints + Pokémon de la ruta). |
| `pxg_pokemon_skills.json` | Skills por Pokémon + orden de combo. |
| `pxg_ignore.json` | Lista de ignorados (nombres + ids). |
| `pxg_walkmap.txt` | Cache de transitabilidad escaneada por el agente. |
| `minimap.otmm` | Minimapa del cliente (lo lee el bot). |

---

## 16. Herramientas (`tools/`)

- `install_agent.py`: **instala/recarga el agente Lua** en el cliente vía gdb
  (`luaL_loadfile`+`lua_pcall`). **Obligatorio tras cada reinicio del cliente.**
  (Linux.)
- `inject_windows.py`: inyector del agente en **Windows** (escribe la config de la
  DLL y hace `LoadLibraryW` remoto). Equivalente Windows de `install_agent.py`.
- `recon_windows.py`: reconocimiento del cliente Windows (arquitectura, módulos,
  exports, `mydata`; decide DLL vs firmas). Solo lectura.
- `derive_signatures.py` + `pe_xref.py`: derivan/localizan las funciones de LuaJIT
  dentro de `pxgme.exe` usando `pxgme-linux` (con símbolos) como oráculo.
- `agent_loader/`: la **DLL** (`pxg_agent_loader.c` + `signatures.h`), scripts de
  build y `NOTES.md` con el método y los hallazgos.
- `lua_probe.py`: ejecuta un chunk Lua dentro del cliente y lee `_G.PXG_DUMP`
  (diagnóstico; Linux/gdb).
- `pxg_agent.lua`: el agente (estado + comandos + navegación).
- `record_route.py`, `e2e_test.py`, `test_behaviors.py`, `fake_client.py`,
  `find_by_name.py`, `pxg_recon.py`: apoyo (grabar rutas, tests de behaviors,
  tests e2e, reconocimiento).

---

## 17. Comandos del agente (`tools/pxg_agent.lua`)

`walk`, `nav`, `navpath`, `standnear`, `standnearmany`, `stop`, `turn`,
`lclick`, `rclick`, `mclick`, `attackat`, `cancelattack`, `spell`, `hotkey`,
`key`, `loot`, `pokestop`, `revive`, `reviveslot`, `clickslot`, `callslot`,
`useinv`, `say`,
`scanmap`, `savemap`, `thingat`, `cover`, `ball`, `order [x y z]`, `orderonself`,
`setfightmode`, `introspect`, `dump`, `dumpn`, `luaver`, `proto`, `ga`, `debug*`.

---

## 18. Cómo ejecutar

```bash
# Instalar dependencias (pocas; ver requirements.txt)
pip install -r requirements.txt

# Instalar/recargar el agente en el cliente (tras cada reinicio del cliente)
python3 tools/install_agent.py          # Linux (gdb)
python  tools/inject_windows.py         # Windows (DLL) — ver abajo

# Arrancar el bot (AFK)
python3 main.py run --verbose

# Arrancar la GUI web
python3 main.py gui --no-open
# -> http://192.168.1.52:8765
```

En **Windows** (resumen; detalle en `tools/agent_loader/README.md`):

```bat
:: 1) compila la DLL (MinGW/Zig/MSVC)
tools\agent_loader\build_windows.bat

:: 2) con pxgme.exe abierto, inyecta el agente
python tools\inject_windows.py --dll tools\agent_loader\pxg_agent_loader.dll
:: o:  python tools\setup_client.py --install   (detecta mydata y el agente)

:: 3) arranca todo
run_windows.bat
```

### Aplicación de escritorio y ejecutable

```bat
python main.py app          # ventana nativa (pywebview/WebView2) + bandeja
python main.py gui          # modo web (navegador)

build_exe.bat
:: genera:
::   dist\dkbot.exe       -> consola (CLI: run/scan/... + app/GUI)
::   dist\dkbot-gui.exe   -> sin consola (doble clic -> app de escritorio)
```

La **app** (`pxg_bot/app.py`) reutiliza la UI web en una **ventana nativa**
(WebView2) con **bandeja del sistema** (pystray): mostrar/ocultar, *siempre
encima*, arrancar/parar bot y salir. **Cerrar la ventana la minimiza a bandeja**
(configurable). Si `pywebview`/WebView2 no están disponibles, cae al modo
navegador. El `.exe` sin subcomando abre la app.

Config en `ui`: `mode`, `debug`, `icon`, `window{width,height,title,on_top}`,
`tray{enabled,minimize_on_close}`.

El `.exe` incluye la interfaz web (`pxg_bot/web`), el agente `.lua`, la DLL, el
icono y `config.json`; al arrancar copia los ficheros escribibles (config,
agente, DLL) junto al exe. Detalle de rutas "frozen" en `pxg_bot/paths.py`.

Otros subcomandos: `scan`, `pointer`, `dump`, `procs`.

---

## 19. Notas y limitaciones

- **El agente se pierde al reiniciar el cliente**: hay que reinstalarlo
  (`tools/install_agent.py` en Linux / `tools/inject_windows.py` en Windows).
- **Todo es 100% Lua**: movimiento, clics, **`order`** y **`revive`** no usan
  X11. `pxg_bot/cursor.py` (XTest / `SetCursorPos`) queda como utilidad.
- El cliente usa **LuaJIT (Lua 5.1)**: evitar sintaxis de Lua 5.4 en el agente
  (p. ej. `//`).
- La detección de NPCs/pokémon de NPC no es directa; se resuelve con la lista de
  ignorados + auto-aprendizaje.
- El `minimap.otmm` puede reescribirse mientras el bot lo lee; la lectura de
  bloques tolera fallos de descompresión.
- **Curación** (`healing`) deshabilitada: sus teclas aún no están implementadas.
- Algunos umbrales (revive, loot, combate) son heurísticos y afinables por
  config.

---

## 20. Multiplataforma (Linux / Windows)

El bot es Python puro + stdlib, así que corre en ambas plataformas.

- **Memoria**: `pxg_bot/memory.py` incluye `WindowsProcess`
  (`OpenProcess`/`ReadProcessMemory`).
- **Input nativo**: `pxg_bot/input.py` incluye `WinInput` (`SendInput`/
  `PostMessage`); `create_input` lo usa en Windows.
- **Cursor** (utilidad, ya no en el flujo normal): `pxg_bot/cursor.py` (XTest en
  Linux / `SetCursorPos` en Windows).
- **Inyección del agente**:
  - Linux: `tools/install_agent.py` (gdb; `break lua_gettop`).
  - Windows: **`tools/agent_loader/pxg_agent_loader.dll`** inyectada con
    `tools/inject_windows.py` (`CreateRemoteThread`+`LoadLibraryW`). La DLL
    localiza `lua_gettop`/`luaL_loadbuffer`/`lua_pcall` en `pxgme.exe` por firma
    (el cliente va stripped y con LuaJIT estático), captura el `lua_State` con un
    hook inline y ejecuta el agente. Ver `tools/agent_loader/NOTES.md`.
- **Config/rutas**: `pxg_bot/config.py` deriva las rutas de `mydata` según
  plataforma y `process_name` es `pxgme.exe`/`pxgme-linux`. `tools/setup_client.py`
  (attach) y la GUI detectan el `mydata` real y reescriben `config.json`.
- **App de escritorio**: `pxg_bot/app.py` envuelve la UI en una ventana nativa
  (**pywebview/WebView2**) con **bandeja** (pystray) y cierre→minimizar. Sin esas
  dependencias cae al navegador (`gui`).
- **Empaquetado**: `build_exe.bat` (PyInstaller) genera `dist\dkbot.exe` (consola,
  CLI+app/GUI) y `dist\dkbot-gui.exe` (sin consola, app). La UI web, el agente
  `.lua`, la DLL, el icono y `config.json` van embebidos; `pxg_bot/paths.py`
  resuelve las rutas en modo "frozen" (datos en el bundle, ficheros escribibles
  junto al exe) y la app lanza el bot como subproceso del propio exe.
  `run_windows.bat` arranca la versión "de repo" (inyecta + GUI).

Nota sobre el cliente Windows: carga los scripts como `.klua` **cifrado**
(`assets/init.klua`, módulos `.klmod`), y solo cae a `assets/init.lua` si
`init.klua` falla; por eso la inyección va por DLL, no por fichero. Detalle y
método de derivación de firmas en `tools/agent_loader/NOTES.md`.
