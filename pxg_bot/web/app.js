"use strict";

/* ---------------- estado general ---------------- */
const BEHAVIORS = ["crisis", "combat", "loot", "revive", "explore"];
let togglesBuilt = false;
let logAutoScroll = true;
let lastState = {};
let worldData = {};

const view = {
  cx: 0, cy: 0, z: 0, scale: 6, follow: true, mode: "color",
  edit: false, initialized: false,
};
let routeWps = [];
let baseCanvas = null, baseKey = "";
let baseLoading = false;
let dragging = null;

async function api(path, method = "GET", body = null) {
  const opts = { method, headers: {} };
  if (body !== null) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  try { return await res.json(); } catch (e) { return {}; }
}

/* ---------------- estado / toggles ---------------- */
function fmtUptime(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const m = Math.floor(sec / 60), s = sec % 60, h = Math.floor(m / 60);
  if (h) return `${h}h ${m % 60}m`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}
function setBar(id, txtId, pct) {
  pct = Math.max(0, Math.min(100, pct || 0));
  document.getElementById(id).style.width = pct + "%";
  document.getElementById(txtId).textContent = Math.round(pct) + "%";
}
function buildToggles() {
  const box = document.getElementById("behaviors");
  box.innerHTML = "";
  for (const name of BEHAVIORS) {
    const el = document.createElement("label");
    el.className = "toggle";
    el.innerHTML = `<input type="checkbox" data-beh="${name}"> <span>${name}</span>`;
    box.appendChild(el);
  }
  box.addEventListener("change", (ev) => {
    const name = ev.target.getAttribute("data-beh");
    if (name) api("/api/control", "POST", { behaviors: { [name]: { enabled: ev.target.checked } } });
  });
  togglesBuilt = true;
}
function buildSkills(keys) {
  const box = document.getElementById("skills");
  box.innerHTML = "";
  keys.forEach((k) => {
    const b = document.createElement("button");
    b.innerHTML = `<strong>${k}</strong><span class="cd" data-cd="${k}">—</span>`;
    b.onclick = () => api("/api/command", "POST", { cmd: `spell ${k}` });
    box.appendChild(b);
  });
}
let routePokemon = { name: "", slot: null };

let partyNames = {};
try { partyNames = JSON.parse(localStorage.getItem("pxgPartyNames") || "{}") || {}; } catch (e) { partyNames = {}; }
function savePartyNames() { try { localStorage.setItem("pxgPartyNames", JSON.stringify(partyNames)); } catch (e) {} }

function updatePartySelect(party, active) {
  // aprender el nombre de los slots que esten activos (el cliente no lo expone por slot)
  let learned = false;
  for (const p of party) {
    if (p.active && active && p.pokeId && partyNames[p.pokeId] !== active) {
      partyNames[p.pokeId] = active;
      learned = true;
    }
  }
  if (learned) savePartyNames();
  const nameOf = (p) => p.name || partyNames[p.pokeId] || "";

  // selector de Pokemon de la ruta
  const sel = document.getElementById("route-pokemon");
  if (sel) {
    const struct = party.map((p) => String(p.slot)).join("|");
    if (sel.dataset.struct !== struct) {
      sel.innerHTML = '<option value="">(ninguno)</option>';
      for (const p of party) {
        const opt = document.createElement("option");
        opt.value = String(p.slot);
        sel.appendChild(opt);
      }
      sel.dataset.struct = struct;
    }
    for (const opt of sel.options) {
      if (!opt.value) continue;
      const slot = parseInt(opt.value, 10);
      const p = party.find((x) => x.slot === slot);
      if (!p) continue;
      const nm = nameOf(p);
      opt.dataset.name = nm;
      opt.textContent = `Slot ${slot}${nm ? " — " + nm : ""}`;
    }
    let want = routePokemon.slot != null ? String(routePokemon.slot) : "";
    if (routePokemon.name) {
      const byName = [...sel.options].find((o) => o.dataset.name === routePokemon.name);
      if (byName) want = byName.value;
    }
    sel.value = [...sel.options].some((o) => o.value === want) ? want : "";
  }
  const info = document.getElementById("route-pokemon-info");
  if (info) info.textContent = routePokemon.name
    ? `se saca al iniciar: ${routePokemon.name} (slot ${routePokemon.slot})`
    : (routePokemon.slot != null ? `se saca al iniciar: slot ${routePokemon.slot}` : "se saca al iniciar la ruta");

  // tarjeta de equipo: una fila por slot, clic = elegir para la ruta
  const box = document.getElementById("party");
  if (!box) return;
  box.innerHTML = "";
  if (!party.length) {
    box.innerHTML = '<div class="muted small">sin datos (agente no activo / fuera del juego)</div>';
    return;
  }
  for (const p of party) {
    const row = document.createElement("div");
    row.className = "party-row";
    if (routePokemon.slot === p.slot) row.classList.add("sel");
    if (p.active) row.classList.add("on");
    const nm = nameOf(p);
    const hp = p.hp != null ? p.hp : 0;
    row.innerHTML =
      `<span class="party-slot">${p.active ? "●" : "○"} Slot ${p.slot}</span>` +
      `<span class="party-name">${nm || "—"}</span>` +
      `<div class="bar"><i style="width:${hp}%"></i><span>${p.hp != null ? p.hp + "%" : ""}</span></div>`;
    row.title = "Elegir este Pokémon (slot " + p.slot + ") para la ruta";
    row.onclick = () => {
      routePokemon = { name: nm, slot: p.slot };
      updatePartySelect(party, active);
    };
    box.appendChild(row);
  }
}

function setRoutePokemon(name, slot) {
  routePokemon = {
    name: name || "",
    slot: (slot === null || slot === undefined || slot === "") ? null : parseInt(slot, 10),
  };
  updatePartySelect(lastState ? (lastState.party || []) : [],
                   lastState ? (lastState.active_pokemon || "") : "");
}

function renderState(s) {
  lastState = s;
  document.getElementById("conn").classList.toggle("on", !!s.connected);
  document.getElementById("who").textContent = s.name ? `${s.name} · L${s.level ?? "?"}` : "—";
  document.getElementById("behavior").textContent = s.behavior || "idle";
  document.getElementById("humanizer").textContent = s.humanizer ? `hum: ${s.humanizer}` : "—";
  document.getElementById("uptime").textContent = fmtUptime(s.uptime);
  document.getElementById("pid").textContent = s.running ? `pid ${s.pid}` : "detenido";
  setBar("hp-bar", "hp-txt", s.hppct);
  setBar("mp-bar", "mp-txt", s.maxmp ? (100 * s.mp / s.maxmp) : 0);
  document.getElementById("pos").textContent = (s.x !== undefined && s.x !== null) ? `${s.x},${s.y},${s.z}` : "—";
  document.getElementById("level").textContent = s.level ?? "—";
  document.getElementById("enemies").textContent = s.enemies ?? 0;
  document.getElementById("walking").textContent = s.is_walking ? "sí" : "no";
  document.getElementById("nav").textContent = s.nav || "—";
  document.getElementById("lastcmd").textContent = s.last_cmd || "—";
  const cnt = s.counters || {};
  const CL = [["kill", "kills"], ["loot", "loots"], ["looted", "looted"], ["ball", "intentos"],
              ["captured", "capturas"], ["revive", "revives"], ["skill", "skills"],
              ["pokestop", "pokestops"], ["lure", "lures"]];
  document.getElementById("counters").textContent = CL.map(([k, l]) => `${l}: ${cnt[k] || 0}`).join("   ");
  document.getElementById("btn-start").disabled = !!s.running;
  document.getElementById("btn-stop").disabled = !s.running;
  document.getElementById("btn-pause").textContent = s.paused ? "Reanudar" : "Pausar";

  if (!togglesBuilt) buildToggles();
  const beh = (s.control && s.control.behaviors) || {};
  for (const name of BEHAVIORS) {
    const cb = document.querySelector(`input[data-beh="${name}"]`);
    if (cb && document.activeElement !== cb) cb.checked = !!(beh[name] && beh[name].enabled);
  }
  const ex = (s.control && s.control.explore) || {};
  document.getElementById("home").textContent = ex.home ? `${ex.home[0]},${ex.home[1]},${ex.home[2]}` : "—";
  const rad = document.getElementById("radius");
  if (document.activeElement !== rad && ex.radius != null) rad.value = ex.radius;
  document.getElementById("radius-txt").textContent = rad.value;
  const patrol = document.getElementById("patrol");
  if (document.activeElement !== patrol && ex.patrol != null) patrol.checked = !!ex.patrol;
  const cap = (s.control && s.control.capture) || {};
  const catchall = document.getElementById("catchall");
  if (document.activeElement !== catchall && cap.catch_all != null) catchall.checked = !!cap.catch_all;
  const combat = (s.control && s.control.combat) || {};
  const aoemin = document.getElementById("aoemin");
  if (document.activeElement !== aoemin && combat.lure_visible_min != null) aoemin.value = combat.lure_visible_min;
  const lureto = document.getElementById("lureto");
  if (document.activeElement !== lureto && combat.lure_gather_timeout != null) lureto.value = combat.lure_gather_timeout;
  const inrmin = document.getElementById("inrmin");
  if (document.activeElement !== inrmin && combat.cast_min_in_range != null) inrmin.value = combat.cast_min_in_range;
  const atkrange = document.getElementById("atkrange");
  if (document.activeElement !== atkrange && combat.attack_range != null) atkrange.value = combat.attack_range;
  const engrange = document.getElementById("engrange");
  if (document.activeElement !== engrange && combat.engage_radius != null) engrange.value = combat.engage_radius;
  const panic = document.getElementById("panic");
  if (document.activeElement !== panic && combat.panic_hp != null) panic.value = combat.panic_hp;
  const panicSrc = document.getElementById("panic-src");
  if (panicSrc && document.activeElement !== panicSrc && combat.panic_hp_source != null) panicSrc.value = combat.panic_hp_source;
  const panicSkills = document.getElementById("panic-skills");
  if (panicSkills && document.activeElement !== panicSkills && combat.panic_skills != null) panicSkills.value = combat.panic_skills;
  const buffon = document.getElementById("buffon");
  if (buffon && document.activeElement !== buffon && combat.buff_on_screen != null) buffon.checked = !!combat.buff_on_screen;
  const buffmin = document.getElementById("buffmin");
  if (buffmin && document.activeElement !== buffmin && combat.buff_visible_min != null) buffmin.value = combat.buff_visible_min;
  const rev = (s.control && s.control.revive) || {};
  const revSlot = document.getElementById("rev-slot");
  if (revSlot && document.activeElement !== revSlot && rev.slot != null) revSlot.value = rev.slot;
  const revItem = document.getElementById("rev-item");
  if (revItem && document.activeElement !== revItem && rev.item != null) revItem.value = rev.item;
  const revOut = document.getElementById("rev-out");
  if (revOut && document.activeElement !== revOut && rev.disconnect_when_out != null) revOut.checked = !!rev.disconnect_when_out;
  const revKey = document.getElementById("rev-key");
  if (revKey && document.activeElement !== revKey && rev.logout_key != null) revKey.value = rev.logout_key;
  const readypct = document.getElementById("readypct");
  if (document.activeElement !== readypct && combat.ready_pct != null) readypct.value = combat.ready_pct;
  const skillcd = document.getElementById("skillcd");
  if (document.activeElement !== skillcd && combat.cooldown != null) skillcd.value = combat.cooldown;
  const reqall = document.getElementById("reqall");
  if (document.activeElement !== reqall && combat.require_all_close != null) reqall.checked = !!combat.require_all_close;
  const routeBeh = (s.control && s.control.route_behavior) || {};
  const idleEn = document.getElementById("idleen");
  if (document.activeElement !== idleEn && routeBeh.start_idle_enabled != null) idleEn.checked = !!routeBeh.start_idle_enabled;
  const idleSecs = document.getElementById("idlesecs");
  if (document.activeElement !== idleSecs && routeBeh.start_idle_seconds != null) {
    const v = routeBeh.start_idle_seconds;
    idleSecs.value = Array.isArray(v) ? Math.round((Number(v[0]) + Number(v[v.length - 1])) / 2) : v;
  }

  updatePartySelect(s.party || [], s.active_pokemon || "");

  const moves = s.moves || [];
  if (document.getElementById("skills").childElementCount !== moves.length) {
    buildSkills(moves.length ? moves.map((m) => m.key) : ["1","2","3","4","5","6","7","8","9"]);
  }
  for (const m of moves) {
    const el = document.querySelector(`[data-cd="${m.key}"]`);
    if (el) el.textContent = `${Math.round(m.pct)}%`;
  }
}

/* ---------------- mapa ---------------- */
const canvas = () => document.getElementById("map");

function sx(tx) { const c = canvas(); return (tx - view.cx) * view.scale + c.width / 2; }
function sy(ty) { const c = canvas(); return (ty - view.cy) * view.scale + c.height / 2; }
function tileAt(px, py) {
  const c = canvas();
  return [
    Math.floor((px - c.width / 2) / view.scale + view.cx),
    Math.floor((py - c.height / 2) / view.scale + view.cy),
  ];
}
function visibleRange() {
  const c = canvas();
  const x0 = Math.floor(view.cx - (c.width / 2) / view.scale) - 1;
  const y0 = Math.floor(view.cy - (c.height / 2) / view.scale) - 1;
  const x1 = Math.ceil(view.cx + (c.width / 2) / view.scale) + 1;
  const y1 = Math.ceil(view.cy + (c.height / 2) / view.scale) + 1;
  return [x0, y0, x1, y1];
}

async function fetchBase() {
  if (baseLoading) return;
  const [x0, y0, x1, y1] = visibleRange();
  const key = `${view.z}:${x0},${y0},${x1},${y1}:${view.mode}`;
  if (key === baseKey) return;
  baseLoading = true;
  try {
    const url = `/api/otmm?z=${view.z}&x0=${x0}&y0=${y0}&x1=${x1}&y1=${y1}&mode=${view.mode}`;
    const res = await fetch(url);
    if (!res.ok) { baseCanvas = null; baseKey = key; return; }
    const w = parseInt(res.headers.get("X-Width"), 10);
    const h = parseInt(res.headers.get("X-Height"), 10);
    const buf = await res.arrayBuffer();
    const rgb = new Uint8Array(buf);
    const idata = new ImageData(w, h);
    for (let i = 0, j = 0; i < w * h; i++, j += 3) {
      idata.data[i * 4] = rgb[j];
      idata.data[i * 4 + 1] = rgb[j + 1];
      idata.data[i * 4 + 2] = rgb[j + 2];
      idata.data[i * 4 + 3] = 255;
    }
    const off = document.createElement("canvas");
    off.width = w; off.height = h;
    off.getContext("2d").putImageData(idata, 0, 0);
    baseCanvas = off;
    baseKey = key;
    baseCanvas._x0 = x0; baseCanvas._y0 = y0;
  } catch (e) {
    baseCanvas = null;
  } finally {
    baseLoading = false;
  }
}

function drawPolyline(ctx, wps, color, width, close) {
  if (!wps || wps.length < 1) return;
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.beginPath();
  wps.forEach((p, i) => {
    const X = sx(p[0]) + view.scale / 2, Y = sy(p[1]) + view.scale / 2;
    if (i === 0) ctx.moveTo(X, Y); else ctx.lineTo(X, Y);
  });
  if (close && wps.length > 1) ctx.closePath();
  ctx.stroke();
}

function drawMap() {
  const c = canvas();
  const ctx = c.getContext("2d");
  ctx.fillStyle = "#0a0d11";
  ctx.fillRect(0, 0, c.width, c.height);

  if (baseCanvas) {
    const X = sx(baseCanvas._x0), Y = sy(baseCanvas._y0);
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(baseCanvas, X, Y, baseCanvas.width * view.scale, baseCanvas.height * view.scale);
  }

  const w = worldData || {};
  if (w.home) {
    ctx.strokeStyle = "#4fa3ff66";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.arc(sx(w.home[0]) + view.scale / 2, sy(w.home[1]) + view.scale / 2, (w.radius || 60) * view.scale, 0, Math.PI * 2);
    ctx.stroke();
  }
  drawPolyline(ctx, (w.patrol || []).map((p) => [p[0], p[1]]), "#e0a53a88", 1.5, false);
  if (routeWps.length) {
    drawPolyline(ctx, routeWps, "#4fa3ffcc", 2.5, true);
    routeWps.forEach((p, i) => {
      ctx.fillStyle = i === 0 ? "#57d97a" : "#4fa3ff";
      ctx.beginPath();
      ctx.arc(sx(p[0]) + view.scale / 2, sy(p[1]) + view.scale / 2, 3, 0, Math.PI * 2);
      ctx.fill();
    });
  }
  const px = lastState.x, py = lastState.y;
  if (px !== null && px !== undefined) {
    ctx.fillStyle = "#e5484d";
    ctx.fillRect(sx(px) - 2, sy(py) - 2, view.scale + 4, view.scale + 4);
  }
  document.getElementById("mapinfo").textContent =
    `z=${view.z} · escala ${view.scale} · ${view.mode === "walk" ? "transitabilidad" : "color"}`;
  document.getElementById("maproute").textContent = `ruta: ${routeWps.length} waypoints${view.edit ? " (editando)" : ""}`;
}

function setFollow(on) {
  view.follow = on;
  const b = document.getElementById("m-follow");
  b.classList.toggle("on", on);
}

async function loadRouteIntoEditor() {
  const r = await api("/api/route");
  routeWps = (r.waypoints || []).map((p) => [p[0], p[1], p[2] ?? view.z]);
  document.getElementById("m-loop").checked = r.loop !== false;
  document.getElementById("m-pp").checked = !!r.ping_pong;
  setRoutePokemon(r.pokemon, r.pokemon_slot);
  drawMap();
}

async function refreshRoutes() {
  const data = await api("/api/routes");
  const sel = document.getElementById("m-routes");
  const routes = data.routes || {};
  const cur = sel.value;
  sel.innerHTML = "";
  for (const name of Object.keys(routes).sort()) {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    sel.appendChild(opt);
  }
  if (cur && routes[cur]) sel.value = cur;
}

let pskOrder = {};
let pskKeys = {};
let pskLureSel = {};

function renderPskList(name, skills, list) {
  list.innerHTML = "";
  const ord = pskOrder[name] || [];
  const sel = pskLureSel[name] || new Set();
  ord.forEach((k, idx) => {
    const s = skills[k] || {};
    const row = document.createElement("div");
    row.className = "psk-row";
    const chk = document.createElement("input");
    chk.type = "checkbox";
    chk.checked = sel.has(k);
    chk.title = "usar esta skill en el combo del lure";
    chk.onchange = () => { if (chk.checked) sel.add(k); else sel.delete(k); };
    const label = document.createElement("span");
    label.innerHTML = `<span class="psk-k">${k}</span> <span class="psk-name">${s.name || ""}</span>` +
      (s.aoe ? ' <span class="psk-aoe">AoE</span>' : "") +
      ` <span class="muted small">${s.effect || ""}</span>`;
    const up = document.createElement("button");
    up.className = "mini"; up.textContent = "↑";
    up.onclick = () => { if (idx > 0) { const a = pskOrder[name]; [a[idx - 1], a[idx]] = [a[idx], a[idx - 1]]; renderPskList(name, skills, list); } };
    const down = document.createElement("button");
    down.className = "mini"; down.textContent = "↓";
    down.onclick = () => { const a = pskOrder[name]; if (idx < a.length - 1) { [a[idx], a[idx + 1]] = [a[idx + 1], a[idx]]; renderPskList(name, skills, list); } };
    const sp = document.createElement("span"); sp.className = "psk-sp";
    row.appendChild(chk); row.appendChild(label); row.appendChild(sp);
    row.appendChild(up); row.appendChild(down);
    list.appendChild(row);
  });
}

async function refreshPokemonSkills() {
  const data = await api("/api/pokemon_skills");
  const pokemon = data.pokemon || {};
  const order = data.order || {};
  const lureOrder = data.lure_order || {};
  const box = document.getElementById("psk");
  box.innerHTML = "";
  const names = Object.keys(pokemon).sort();
  document.getElementById("psk-info").textContent =
    names.length ? `${names.length} Pokémon` : "(sin datos — activa un Pokémon)";
  for (const name of names) {
    const skills = pokemon[name];
    const sig = Object.keys(skills).sort().join(",");
    const sameKeys = pskKeys[name] === sig && pskOrder[name];
    let ord;
    if (sameKeys) {
      ord = pskOrder[name];
    } else {
      ord = (order[name] || []).filter((k) => k in skills);
      for (const k of Object.keys(skills)) if (!ord.includes(k)) ord.push(k);
    }
    pskKeys[name] = sig;
    pskOrder[name] = ord;
    if (!sameKeys || !pskLureSel[name]) {
      const saved = (lureOrder[name] || []).filter((k) => k in skills);
      pskLureSel[name] = new Set(saved.length ? saved : Object.keys(skills).filter((k) => skills[k].aoe));
    }
    const activeName = (lastState && lastState.active_pokemon) || "";
    const card = document.createElement("div");
    card.className = "psk-mon";
    if (name === activeName) card.classList.add("psk-active");
    const title = document.createElement("div");
    title.className = "psk-title";
    title.innerHTML = `<b>${name}</b>` +
      (name === activeName ? ' <span class="psk-aoe">activo</span>' : "");
    const save = document.createElement("button");
    save.className = "mini primary";
    save.textContent = "Guardar orden";
    save.onclick = () => api("/api/pokemon_skills/order", "POST", { name, order: pskOrder[name] });
    const saveLure = document.createElement("button");
    saveLure.className = "mini";
    saveLure.textContent = "Guardar combo lure";
    saveLure.title = "las skills marcadas, en el orden de la lista, se usan en cada lure";
    saveLure.onclick = () => {
      const keys = pskOrder[name].filter((k) => pskLureSel[name].has(k));
      api("/api/pokemon_skills/lure_order", "POST", { name, order: keys });
    };
    const del = document.createElement("button");
    del.className = "mini ghost";
    del.textContent = "✕";
    del.title = name === activeName
      ? "no se puede borrar el Pokémon activo" : "borrar del catálogo";
    del.disabled = (name === activeName);
    del.onclick = async () => {
      if (name === activeName) return;
      await api("/api/pokemon_skills/delete", "POST", { name });
      delete pskOrder[name]; delete pskKeys[name]; delete pskLureSel[name];
      await refreshPokemonSkills();
    };
    title.appendChild(saveLure);
    title.appendChild(save);
    title.appendChild(del);
    card.appendChild(title);
    const hint = document.createElement("div");
    hint.className = "muted small";
    hint.textContent = "✔ = skill del combo del lure (se usa en este orden)";
    card.appendChild(hint);
    const list = document.createElement("div");
    list.className = "psk-list";
    card.appendChild(list);
    box.appendChild(card);
    renderPskList(name, skills, list);
  }
}

async function refreshIgnore() {
  const data = await api("/api/ignore");
  const box = document.getElementById("ign");
  box.innerHTML = "";
  const names = data.names || [];
  const ids = data.ids || [];
  const creatures = data.creatures || [];
  document.getElementById("ign-info").textContent =
    `nombres: ${names.length} · ids: ${ids.length} · en pantalla: ${creatures.length}`;

  const ncard = document.createElement("div");
  ncard.className = "psk-mon";
  ncard.innerHTML = "<div class='psk-title'><b>Nombres ignorados</b></div>";
  const nlist = document.createElement("div");
  nlist.className = "psk-list";
  if (!names.length) nlist.innerHTML = "<span class='muted small'>ninguno</span>";
  names.forEach((n) => {
    const row = document.createElement("div");
    row.className = "psk-row";
    row.innerHTML = `<span class="psk-name">${n}</span>`;
    const rm = document.createElement("button");
    rm.className = "mini"; rm.textContent = "quitar";
    rm.onclick = () => api("/api/ignore", "POST", { action: "remove", name: n }).then(refreshIgnore);
    row.appendChild(rm);
    nlist.appendChild(row);
  });
  ncard.appendChild(nlist);
  box.appendChild(ncard);

  const ccard = document.createElement("div");
  ccard.className = "psk-mon";
  ccard.innerHTML = "<div class='psk-title'><b>Criaturas en pantalla</b></div>";
  const clist = document.createElement("div");
  clist.className = "psk-list";
  creatures.forEach((c) => {
    const kind = c.player ? "jugador" : (c.npc ? "npc" : "monstruo");
    const row = document.createElement("div");
    row.className = "psk-row";
    row.innerHTML = `<span class="psk-name">${c.name}</span> ` +
      `<span class="muted small">${kind} · outfit ${c.outfit ?? "?"} · id ${c.id}</span>` +
      (c.ignored ? ' <span class="psk-aoe">ignorado</span>' : "");
    const igId = document.createElement("button");
    igId.className = "mini"; igId.textContent = "ignorar id";
    igId.onclick = () => api("/api/ignore", "POST", { action: "add", id: c.id }).then(refreshIgnore);
    const igName = document.createElement("button");
    igName.className = "mini"; igName.textContent = "ignorar nombre";
    igName.onclick = () => api("/api/ignore", "POST", { action: "add", name: c.name }).then(refreshIgnore);
    row.appendChild(igId); row.appendChild(igName);
    clist.appendChild(row);
  });
  ccard.appendChild(clist);
  box.appendChild(ccard);
}

function wireMap() {
  const c = canvas();
  document.getElementById("m-follow").onclick = () => setFollow(!view.follow);
  document.getElementById("m-mode").onclick = () => {
    view.mode = view.mode === "color" ? "walk" : "color";
    document.getElementById("m-mode").textContent = "Ver: " + (view.mode === "color" ? "color" : "transit.");
    baseKey = ""; fetchBase().then(drawMap);
  };
  document.getElementById("m-zoomin").onclick = () => { view.scale = Math.min(40, view.scale + 2); baseKey = ""; fetchBase().then(drawMap); };
  document.getElementById("m-zoomout").onclick = () => { view.scale = Math.max(2, view.scale - 2); baseKey = ""; fetchBase().then(drawMap); };
  document.getElementById("m-edit").onclick = async () => {
    view.edit = !view.edit;
    document.getElementById("m-edit").classList.toggle("on", view.edit);
    c.style.cursor = view.edit ? "crosshair" : "grab";
    if (view.edit && !routeWps.length) await loadRouteIntoEditor();
    drawMap();
  };
  document.getElementById("m-undo").onclick = () => { routeWps.pop(); drawMap(); };
  document.getElementById("m-clear").onclick = () => { routeWps = []; drawMap(); };
  document.getElementById("m-save").onclick = () => {
    api("/api/route", "POST", {
      waypoints: routeWps, loop: document.getElementById("m-loop").checked,
      ping_pong: document.getElementById("m-pp").checked, enabled: true,
      pokemon: routePokemon.name, pokemon_slot: routePokemon.slot,
    }).then(() => loadRouteIntoEditor());
  };
  document.getElementById("m-load").onclick = async () => {
    const name = document.getElementById("m-routes").value;
    if (!name) return;
    const data = await api("/api/routes");
    const r = (data.routes || {})[name];
    if (!r) return;
    routeWps = (r.waypoints || []).map((p) => [p[0], p[1], p[2] ?? view.z]);
    document.getElementById("m-loop").checked = r.loop !== false;
    document.getElementById("m-pp").checked = !!r.ping_pong;
    setRoutePokemon(r.pokemon, r.pokemon_slot);
    drawMap();
  };
  document.getElementById("m-saveas").onclick = async () => {
    const name = (document.getElementById("m-route-name").value || "").trim();
    if (!name) { alert("Escribe un nombre de ruta"); return; }
    await api("/api/routes", "POST", {
      name, waypoints: routeWps,
      loop: document.getElementById("m-loop").checked,
      ping_pong: document.getElementById("m-pp").checked,
      pokemon: routePokemon.name, pokemon_slot: routePokemon.slot,
    });
    await refreshRoutes();
    document.getElementById("m-routes").value = name;
  };
  document.getElementById("m-delete").onclick = async () => {
    const name = document.getElementById("m-routes").value;
    if (!name) return;
    await api("/api/routes/delete", "POST", { name });
    await refreshRoutes();
  };
  const rpSel = document.getElementById("route-pokemon");
  if (rpSel) rpSel.onchange = () => {
    const opt = rpSel.selectedOptions[0];
    routePokemon = { name: (opt && opt.dataset.name) || "", slot: rpSel.value ? parseInt(rpSel.value, 10) : null };
    updatePartySelect(lastState ? (lastState.party || []) : [],
                     lastState ? (lastState.active_pokemon || "") : "");
  };
  c.addEventListener("wheel", (e) => {
    e.preventDefault();
    const rect = c.getBoundingClientRect();
    const mx = (e.clientX - rect.left) * c.width / rect.width;
    const my = (e.clientY - rect.top) * c.height / rect.height;
    const before = tileAt(mx, my);
    view.scale = Math.max(2, Math.min(40, view.scale + (e.deltaY < 0 ? 1 : -1)));
    const after = tileAt(mx, my);
    view.cx += before[0] - after[0];
    view.cy += before[1] - after[1];
    baseKey = ""; fetchBase().then(drawMap);
  }, { passive: false });
  c.addEventListener("mousedown", (e) => {
    const rect = c.getBoundingClientRect();
    const mx = (e.clientX - rect.left) * c.width / rect.width;
    const my = (e.clientY - rect.top) * c.height / rect.height;
    if (view.edit) {
      const [tx, ty] = tileAt(mx, my);
      routeWps.push([tx, ty, view.z]);
      drawMap();
    } else {
      dragging = { mx, my, cx: view.cx, cy: view.cy };
      setFollow(false);
      c.style.cursor = "grabbing";
    }
  });
  c.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    const rect = c.getBoundingClientRect();
    const mx = (e.clientX - rect.left) * c.width / rect.width;
    const my = (e.clientY - rect.top) * c.height / rect.height;
    view.cx = dragging.cx - (mx - dragging.mx) / view.scale;
    view.cy = dragging.cy - (my - dragging.my) / view.scale;
    baseKey = ""; fetchBase().then(drawMap);
  });
  window.addEventListener("mouseup", () => { dragging = null; canvas().style.cursor = view.edit ? "crosshair" : "grab"; });
}

/* ---------------- polling ---------------- */
async function pollState() {
  const s = await api("/api/state");
  if (s && Object.keys(s).length) renderState(s);
}
async function pollLog() {
  const data = await api("/api/log?tail=200");
  const el = document.getElementById("log");
  el.textContent = (data.lines || []).join("\n");
  if (logAutoScroll) el.scrollTop = el.scrollHeight;
}
async function pollWorld() {
  const s = lastState;
  if (s.x !== undefined && s.x !== null) {
    if (!view.initialized) {
      view.cx = s.x; view.cy = s.y;
      view.initialized = true;
    }
    if (s.z !== undefined && s.z !== null && s.z !== view.z) { view.z = s.z; baseKey = ""; }
    if (view.follow) { view.cx = s.x; view.cy = s.y; }
  }
  worldData = await api(`/api/world?z=${view.z}`);
  if (!baseCanvas || baseKey === "") await fetchBase();
  drawMap();
}

function wire() {
  document.getElementById("btn-start").onclick = () => api("/api/bot", "POST", { action: "start" });
  document.getElementById("btn-stop").onclick = () => api("/api/bot", "POST", { action: "stop" });
  document.getElementById("btn-attach").onclick = async () => {
    const btn = document.getElementById("btn-attach");
    const old = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Atachando…";
    try {
      const r = await api("/api/attach", "POST", {});
      if (r.ok) {
        alert(`Cliente atachado\npid: ${r.pid}\nmydata: ${r.mydata}\n` +
              `agente: ${r.installed ? "inyectado OK" : "NO inyectado"}` +
              (r.install_output ? `\n\n${r.install_output}` : ""));
      } else {
        alert("Error al atachar: " + (r.error || "desconocido"));
      }
    } catch (e) {
      alert("Error: " + e);
    } finally {
      btn.disabled = false;
      btn.textContent = old;
    }
  };
  document.getElementById("btn-pause").onclick = () => api("/api/control", "POST", { paused: !(lastState.paused) });
  document.getElementById("btn-panic").onclick = async () => {
    await api("/api/command", "POST", { cmd: "stop" });
    await api("/api/control", "POST", { paused: true });
  };
  document.getElementById("btn-home").onclick = () => {
    if (lastState.x == null) return;
    api("/api/control", "POST", { explore: { home: [lastState.x, lastState.y, lastState.z] } });
  };
  document.getElementById("btn-home-clear").onclick = () => api("/api/control", "POST", { explore: { home: null } });
  const rad = document.getElementById("radius");
  rad.oninput = () => { document.getElementById("radius-txt").textContent = rad.value; };
  rad.onchange = () => api("/api/control", "POST", { explore: { radius: parseInt(rad.value, 10) } });
  document.getElementById("patrol").onchange = (e) => api("/api/control", "POST", { explore: { patrol: e.target.checked } });
  document.getElementById("catchall").onchange = (e) => api("/api/control", "POST", { capture: { catch_all: e.target.checked } });
  document.getElementById("aoemin").onchange = (e) => {
    const n = parseInt(e.target.value, 10);
    if (n >= 1) api("/api/control", "POST", { combat: { lure_visible_min: n } });
  };
  document.getElementById("lureto").onchange = (e) => {
    const n = parseInt(e.target.value, 10);
    if (n >= 0) api("/api/control", "POST", { combat: { lure_gather_timeout: n } });
  };
  document.getElementById("inrmin").onchange = (e) => {
    const n = parseInt(e.target.value, 10);
    if (n >= 1) api("/api/control", "POST", { combat: { cast_min_in_range: n } });
  };
  document.getElementById("atkrange").onchange = (e) => {
    const n = parseInt(e.target.value, 10);
    if (n >= 1) api("/api/control", "POST", { combat: { attack_range: n } });
  };
  document.getElementById("engrange").onchange = (e) => {
    const n = parseInt(e.target.value, 10);
    if (n >= 0) api("/api/control", "POST", { combat: { engage_radius: n } });
  };
  document.getElementById("panic").onchange = (e) => {
    const n = parseInt(e.target.value, 10);
    if (n >= 1) api("/api/control", "POST", { combat: { panic_hp: n } });
  };
  document.getElementById("panic-src").onchange = (e) =>
    api("/api/control", "POST", { combat: { panic_hp_source: e.target.value } });
  document.getElementById("panic-skills").onchange = (e) =>
    api("/api/control", "POST", { combat: { panic_skills: e.target.value } });
  document.getElementById("buffon").onchange = (e) =>
    api("/api/control", "POST", { combat: { buff_on_screen: e.target.checked } });
  document.getElementById("buffmin").onchange = (e) => {
    const n = parseInt(e.target.value, 10);
    if (n >= 1) api("/api/control", "POST", { combat: { buff_visible_min: n } });
  };
  const revSlot = document.getElementById("rev-slot");
  if (revSlot) revSlot.onchange = (e) => {
    const n = parseInt(e.target.value, 10);
    if (n >= 1) api("/api/control", "POST", { revive: { slot: n } });
  };
  const revItem = document.getElementById("rev-item");
  if (revItem) revItem.onchange = (e) => {
    const n = parseInt(e.target.value, 10);
    if (n >= 1) api("/api/control", "POST", { revive: { item: n } });
  };
  const revOut = document.getElementById("rev-out");
  if (revOut) revOut.onchange = (e) =>
    api("/api/control", "POST", { revive: { disconnect_when_out: e.target.checked } });
  const revKey = document.getElementById("rev-key");
  if (revKey) revKey.onchange = (e) =>
    api("/api/control", "POST", { revive: { logout_key: (e.target.value || "F12") } });
  document.getElementById("readypct").onchange = (e) => {
    const n = parseInt(e.target.value, 10);
    if (n >= 1) api("/api/control", "POST", { combat: { ready_pct: n } });
  };
  document.getElementById("skillcd").onchange = (e) => {
    const n = parseFloat(e.target.value);
    if (!isNaN(n) && n >= 0) api("/api/control", "POST", { combat: { cooldown: n } });
  };
  document.getElementById("reqall").onchange = (e) =>
    api("/api/control", "POST", { combat: { require_all_close: e.target.checked } });
  document.getElementById("idleen").onchange = (e) =>
    api("/api/control", "POST", { route_behavior: { start_idle_enabled: e.target.checked } });
  document.getElementById("idlesecs").onchange = (e) => {
    const n = parseInt(e.target.value, 10);
    if (n >= 0) api("/api/control", "POST", { route_behavior: { start_idle_seconds: n } });
  };
  document.getElementById("btn-log-clear").onclick = (e) => {
    logAutoScroll = !logAutoScroll;
    e.target.textContent = logAutoScroll ? "auto-scroll" : "scroll off";
  };
  document.getElementById("psk-refresh").onclick = refreshPokemonSkills;
  document.getElementById("ign-refresh").onclick = refreshIgnore;
  wireMap();
  setFollow(true);
}

wire();
loadRouteIntoEditor();
refreshRoutes();
refreshPokemonSkills();
refreshIgnore();
setInterval(pollState, 300);
setInterval(pollLog, 1000);
setInterval(pollWorld, 1000);
setInterval(refreshPokemonSkills, 4000);
setInterval(refreshIgnore, 5000);
pollState();
pollWorld();
