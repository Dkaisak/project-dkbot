-- Agente Lua PXG v3: estado + comandos. Cancela su tick anterior al reejecutarse.
local DIR = "/home/dkaisak/Descargas/pxg-linux/mydata"
local STATE = DIR .. "/pxg_bot_state.json"
local CMD = DIR .. "/pxg_bot_cmd.txt"

if PXG_EVENT and g_eventDispatcher and g_eventDispatcher.cancel then
  pcall(function() g_eventDispatcher.cancel(PXG_EVENT) end)
end
PXG_GEN = (PXG_GEN or 0) + 1
local mygen = PXG_GEN

local function readCmd()
  local f = io.open(CMD, "r")
  if not f then return nil end
  local s = f:read("*a"); f:close()
  local removed = false
  if os and os.remove then removed = os.remove(CMD) end
  if not removed then
    local w = io.open(CMD, "w"); if w then w:close() end
  end
  return s
end

local function isKind(c, fn)
  local ok, r = pcall(function() return c[fn] and c[fn](c) end)
  return ok and r == true
end

local function creatureAt(x, y, z)
  local c = g_map.getCreatureById and nil
  local center = (g_map.getCentralPosition and g_map.getCentralPosition()) or nil
  local ok, specs = nil, nil
  if center then ok, specs = pcall(g_map.getSpectators, g_map, center, false) end
  if ok and type(specs) == "table" then
    for _, cc in ipairs(specs) do
      local okp, cp = pcall(function() return cc:getPosition() end)
      if okp and cp and cp.x == x and cp.y == y and cp.z == z then return cc end
    end
  end
  local lp = g_game.getLocalPlayer()
  if lp then
    for dx = -8, 8 do
      for dy = -8, 8 do
        local okq, t = pcall(g_map.getTile, { x = lp:getPosition().x + dx, y = lp:getPosition().y + dy, z = z })
        if okq and t then
          local okc, crs = pcall(function() return t:getCreatures() end)
          if okc and crs then
            for _, cc in ipairs(crs) do
              local okp, cp = pcall(function() return cc:getPosition() end)
              if okp and cp and cp.x == x and cp.y == y and cp.z == z then return cc end
            end
          end
        end
      end
    end
  end
  return nil
end

local function exec(line)
  local op, arg = line:match("^%s*(%a+)%s*(.-)%s*$")
  if op == "walk" then return g_game.walk(tonumber(arg))
  elseif op == "loot" then return g_game.collectLoot()
  elseif op == "nav" then
    local x, y, z = arg:match("(-?%d+)%s+(-?%d+)%s+(-?%d+)")
    local lp = g_game.getLocalPlayer()
    if x and lp then
      x, y, z = tonumber(x), tonumber(y), tonumber(z)
      local p = lp:getPosition()
      local function tryPath(tx, ty)
        local ok, path = pcall(g_map.findPath, p, { x = tx, y = ty, z = z }, 128, 0)
        if ok and type(path) == "table" and #path > 0 then return path end
        return nil
      end
      local path = tryPath(x, y)
      if not path then
        local cands = { { x + 1, y }, { x - 1, y }, { x, y + 1 }, { x, y - 1 },
                        { x + 1, y + 1 }, { x - 1, y - 1 }, { x + 1, y - 1 }, { x - 1, y + 1 } }
        for _, c in ipairs(cands) do
          path = tryPath(c[1], c[2])
          if path then break end
        end
      end
      if path then return g_game.walk(path[1]) end
      return "no-path"
    end
    return "bad-pos"
  elseif op == "turn" then return g_game.turn(tonumber(arg))
  elseif op == "stop" then return g_game.stop()
  elseif op == "cancelattack" then return g_game.cancelAttack()
  elseif op == "useinv" then return g_game.useInventoryItem(tonumber(arg))
  elseif op == "say" then return g_game.talk(arg)
  elseif op == "attackat" then
    local x, y, z = arg:match("(-?%d+)%s+(-?%d+)%s+(-?%d+)")
    local c = creatureAt(tonumber(x), tonumber(y), tonumber(z))
    if c then return g_game.attack(c) end
    return "no-creature"
  elseif op == "key" then
    if g_keyboard and g_keyboard.pressKey then return g_keyboard.pressKey(arg, "", "", "") end
    return "no-keyboard"
  end
  return "unknown:" .. tostring(op)
end

local function snapCreature(c)
  local okp, cp = pcall(function() return c:getPosition() end)
  if not okp or not cp then return nil end
  return {
    name = tostring(c:getName()), x = cp.x, y = cp.y, z = cp.z,
    hp = c:getHealthPercent(),
    player = isKind(c, "isPlayer"), monster = isKind(c, "isMonster"), npc = isKind(c, "isNpc"),
  }
end

local function snapshot()
  local lp = g_game.getLocalPlayer()
  if not lp then return { connected = false } end
  local p = lp:getPosition()
  local st = {
    connected = true, name = lp:getName(),
    hp = lp:getHealth(), maxhp = lp:getMaxHealth(), hppct = lp:getHealthPercent(),
    level = lp:getLevel(), x = p.x, y = p.y, z = p.z, dir = lp:getDirection(),
    attacking = (g_game.getAttackingCreature and g_game.getAttackingCreature() ~= nil) or false,
    attacking_name = (function()
      local ok, ac = pcall(function() return g_game.getAttackingCreature() end)
      return ok and ac and tostring(ac:getName()) or ""
    end)(),
    controlling = (function()
      local ok, cc = pcall(function() return g_game.getControllingCreature() end)
      return ok and cc and tostring(cc:getName()) or ""
    end)(),
    creatures = {}, nearby = {}, battle = {}, moves = {}, bag = {},
  }
  local center = (g_map.getCentralPosition and g_map.getCentralPosition()) or p
  local ok, specs = pcall(g_map.getSpectators, g_map, center, false)
  if ok and type(specs) == "table" then
    for i, c in ipairs(specs) do
      local sc = snapCreature(c)
      if sc then sc.id = i; st.creatures[#st.creatures + 1] = sc end
    end
  end
  local R = PXG_SCAN_RADIUS or 20
  for dx = -R, R do
    for dy = -R, R do
      local okq, t = pcall(g_map.getTile, { x = p.x + dx, y = p.y + dy, z = p.z })
      if okq and t then
        local crs = {}
        local okc, tcs = pcall(function() return t:getCreatures() end)
        if okc and tcs then
          for _, cc in ipairs(tcs) do
            local sc = snapCreature(cc)
            if sc then crs[#crs + 1] = sc end
          end
        end
        if #crs > 0 then
          st.nearby[#st.nearby + 1] = { dx = dx, dy = dy, ids = {}, creatures = crs }
        end
      end
    end
  end
  local okr, root = pcall(function() return g_ui.getRootWidget() end)
  if okr and root then
    local okm, bar = pcall(function() return root:recursiveGetChildById("movesBar") end)
    if okm and bar then
      local okk, kids = pcall(function() return bar:getChildren() end)
      if okk and kids then
        for _, w in ipairs(kids) do
          local hk = w:getChildById("moveHotkey")
          local shown = true
          local ovis, vis = pcall(function() return w:isVisible() end)
          if ovis then shown = vis end
          if hk and shown then
            local ok1, k = pcall(function() return hk:getText() end)
            local pr = w:getChildById("progressRect")
            local pct = 100
            if pr then
              local ok2, p = pcall(function() return pr:getPercent() end)
              if ok2 and type(p) == "number" then pct = p end
            end
            if ok1 and k and k ~= "" then
              st.moves[#st.moves + 1] = { key = tostring(k), pct = pct }
            end
          end
        end
      end
    end
  end

  local okr2, root2 = pcall(function() return g_ui.getRootWidget() end)
  if okr2 and root2 then
    local okp, panel = pcall(function() return root2:recursiveGetChildById("battlePanel") end)
    if okp and panel then
      local okk, kids = pcall(function() return panel:getChildren() end)
      if okk and kids then
        for i, b in ipairs(kids) do
          local oc, cr = pcall(function() return b.getCreature and b:getCreature() end)
          if oc and cr then
            local okq, cp = pcall(function() return cr:getPosition() end)
            local okn, nm = pcall(function() return cr:getName() end)
            st.battle[#st.battle + 1] = {
              name = tostring(okn and nm or "?"), own = (i == 1),
              x = okq and cp.x or 0, y = okq and cp.y or 0, z = okq and cp.z or 0,
            }
          end
        end
      end
    end
  end
  local okmp, mp = pcall(function() return g_game.getMapPanel() end)
  if okmp and mp then
    local okc, cam = pcall(function() return mp:getCameraPosition() end)
    if okc and cam then st.camera = { x = cam.x, y = cam.y } end
    local okrc, rct = pcall(function() return mp:getRect() end)
    if okrc and rct then
      st.map_rect = { x = rct.x, y = rct.y, w = rct.width, h = rct.height }
    end
  end
  local okc, conts = pcall(function() return g_game.getContainers() end)
  if okc and conts then
    for _, c in ipairs(conts) do
      local oi, items = pcall(function() return c:getItems() end)
      if oi and items then
        for _, it in ipairs(items) do
          local oid, id = pcall(function() return it:getId() end)
          if oid and id then st.bag[#st.bag + 1] = id end
        end
      end
    end
  end
  return st
end

local function tick()
  if PXG_GEN ~= mygen then return end
  pcall(function()
    local cmd = readCmd()
    if cmd and #cmd > 0 then
      for line in cmd:gmatch("[^\n]+") do pcall(exec, line) end
    end
  end)
  local ok, st = pcall(snapshot)
  if not ok then st = { error = tostring(st) } end
  local oke, json = pcall(cjson.encode, st)
  if oke then
    local f = io.open(STATE, "w")
    if f then f:write(json); f:close() end
  end
pcall(function() g_game.changeAutoLoot(true) end)
pcall(function() PXG_EVENT = g_eventDispatcher.schedule(tick, 200) end)
end

pcall(function() PXG_EVENT = g_eventDispatcher.schedule(tick, 200) end)
