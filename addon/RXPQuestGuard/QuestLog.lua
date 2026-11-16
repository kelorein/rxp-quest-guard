local _, QG = ...
local QuestLog = {}
QG.questLog = QuestLog
local inventoryCache, inventoryAPI

local function read(fn, ...)
    if type(fn) ~= "function" then return nil end
    local ok, a, b, c, d = pcall(fn, ...)
    if ok then return a, b, c, d end
end

local function flag(value)
    if type(value) == "boolean" then return value end
    if value == 1 then return true end
    if value == 0 then return false end
end

local function readable(api, value)
    return type(api.issecretvalue) ~= "function" or not api.issecretvalue(value)
end

local function positiveTime(api, value)
    if readable(api, value) and type(value) == "number" and value > 0 and value < math.huge then
        return value
    end
end

local function readBags(api)
    local container = api.C_Container or {}
    local sizes = container.GetContainerNumSlots or api.GetContainerNumSlots
    local getInfo = container.GetContainerItemInfo
    local getId = container.GetContainerItemID or api.GetContainerItemID
    local getQuest = container.GetContainerItemQuestInfo or api.GetContainerItemQuestInfo
    local result = {items = {}, starters = {}}
    if type(sizes) ~= "function" or (type(getInfo) ~= "function" and type(getId) ~= "function") then
        return result
    end
    local bagIndex = api.Enum and api.Enum.BagIndex or {}
    local inventory = api.Constants and api.Constants.InventoryConstants or {}
    local backpack = bagIndex.Backpack or api.BACKPACK_CONTAINER or 0
    local last = inventory.NumBagSlots or api.NUM_BAG_FRAMES or api.NUM_BAG_SLOTS or 4
    if type(backpack) ~= "number" or type(last) ~= "number" or backpack < 0 or last > 8 then return result end
    result.complete = true
    for bag = backpack, last do
        local ok, slots = pcall(sizes, bag)
        if ok and readable(api, slots) and type(slots) == "number" and slots >= 0 and slots <= 200 then
            for slot = 1, slots do
                local info, rawId
                if type(getInfo) == "function" then
                    ok, info = pcall(getInfo, bag, slot)
                    if ok and type(info) == "table" then rawId = info.itemID end
                    if not ok then result.complete = false end
                end
                if type(getId) == "function" and rawId == nil then
                    ok, rawId = pcall(getId, bag, slot)
                    if not ok then rawId, result.complete = nil, false end
                end
                local id = readable(api, rawId) and QG.engine.questId(rawId)
                if id then
                    local stack = type(info) == "table" and info.stackCount
                    local counts = result.items[id] or {count = 0}
                    counts.count = counts.count + (positiveTime(api, stack) or 1)
                    result.items[id] = counts
                    local questInfo, questId, active = read(getQuest, bag, slot)
                    if type(questInfo) == "table" then questId, active = questInfo.questID, questInfo.isActive end
                    local quest = readable(api, questId) and QG.engine.questId(questId)
                    if quest and readable(api, active) then
                        if counts.questId and counts.questId ~= quest then
                            counts.conflicting = true
                        end
                        counts.questId, counts.active = quest, flag(active)
                        -- The stock bag UI draws the quest-start bang for this exact predicate.
                        if counts.active == false and not result.starters[quest] then
                            local name = type(info) == "table" and info.itemName
                            if not readable(api, name) or type(name) ~= "string" or name == "" then name = nil end
                            result.starters[quest] = {itemId = id, name = name, source = "Live WoW bag API"}
                        end
                    end
                end
            end
        else
            result.complete = false
        end
    end
    return result
end

function QuestLog.invalidateInventory(itemId)
    if itemId and (not inventoryCache or not inventoryCache.items[itemId]) then return false end
    inventoryCache, inventoryAPI = nil, nil
    return true
end

function QuestLog.inventory(api, enabled, cachedOnly)
    if not enabled then return nil end
    local counts = {}
    return function(questId, itemId)
        if inventoryAPI ~= api or not inventoryCache then
            if cachedOnly then return nil end
            local ok, bags = pcall(readBags, api)
            if ok then
                inventoryAPI, inventoryCache = api, bags
            else
                inventoryAPI, inventoryCache = api, {items = {}, starters = {}}
            end
        end
        local bags = inventoryCache
        local item = bags.items[itemId]
        if item then return bags.starters[questId], item.count, item end
        if itemId and bags.complete then return bags.starters[questId], 0 end
        if itemId and counts[itemId] == nil then
            local fn = api.C_Item and api.C_Item.GetItemCount
            local count
            if type(fn) == "function" then
                count = read(fn, itemId, false, false, false, false)
            else
                count = read(api.GetItemCount, itemId, false, false)
            end
            if positiveTime(api, count) then
                local equipped = api.C_Item and api.C_Item.IsEquippedItem or api.IsEquippedItem
                local isEquipped = read(equipped, itemId)
                if not readable(api, isEquipped) or flag(isEquipped) ~= false then count = nil end
            end
            counts[itemId] = readable(api, count) and type(count) == "number" and count >= 0 and
                count < math.huge and count or false
        end
        return bags.starters[questId], counts[itemId] or nil
    end
end

local function countIsValid(value)
    return type(value) == "number" and value >= 0 and value <= 2000 and value == math.floor(value)
end

local function modernEntries(log)
    local count, total = read(log.GetNumQuestLogEntries)
    if not countIsValid(count) or type(log.GetInfo) ~= "function" then return nil end
    local entries, found, clean, collapsed = {}, 0, true, false
    for index = 1, count do
        local entry = read(log.GetInfo, index)
        if type(entry) ~= "table" then
            clean = false
        elseif entry.isHeader then
            collapsed = collapsed or not not entry.isCollapsed
        else
            local id = QG.engine.questId(entry.questID)
            if id and not entries[id] then
                entries[id], found = entry, found + 1
            else
                clean = false
            end
        end
    end
    if not countIsValid(total) then total = nil end
    local complete = clean and (total and total == found or not total and not collapsed)
    return entries, complete, clean and total or nil, found
end

local function legacyEntries(api)
    local count, total = read(api.GetNumQuestLogEntries)
    if not countIsValid(count) or type(api.GetQuestLogTitle) ~= "function" then return nil end
    local entries, found, clean, hasCollapsed = {}, 0, true, false
    for index = 1, count do
        local ok, title, _, _, header, collapsed, ready, _, questId = pcall(api.GetQuestLogTitle, index)
        if not ok then
            clean = false
        elseif header then
            hasCollapsed = hasCollapsed or not not collapsed
        elseif QG.engine.questId(questId) then
            local id = QG.engine.questId(questId)
            if entries[id] then clean = false else found = found + 1 end
            entries[id] = {title = title, ready = flag(ready), index = index}
        else
            clean = false
        end
    end
    if not countIsValid(total) then total = nil end
    local complete = clean and (total and total == found or not total and not hasCollapsed)
    return entries, complete, clean and total or nil, found
end

local function completedSet(api, log)
    local all = read(log.GetAllCompletedQuestIDs)
    if type(all) == "table" then
        local lookup, count, maximum = {}, 0, 0
        for index, value in pairs(all) do
            local id = QG.engine.questId(value)
            if type(index) ~= "number" or index < 1 or index ~= math.floor(index) or not id then
                return nil
            end
            lookup[id], count, maximum = true, count + 1, math.max(index, maximum)
        end
        if maximum == count then return lookup end
    end
    if type(api.GetQuestsCompleted) == "function" then
        local lookup = {}
        local ok, returned = pcall(api.GetQuestsCompleted, lookup)
        if ok and (returned == nil or type(returned) == "table") then
            local result = {}
            for key, value in pairs(returned or lookup) do
                local id = QG.engine.questId(key)
                if not id or flag(value) == nil then return nil end
                if flag(value) then result[id] = true end
            end
            return result
        end
    end
end

local function readyForTurnIn(log, id, entry)
    local ready = flag(read(log.ReadyForTurnIn, id))
    if ready == nil then ready = flag(read(log.IsComplete, id)) end
    if ready == nil then ready = entry.ready end
    return ready
end

local function sortedIds(values)
    local ids = {}
    for id in pairs(values) do ids[#ids + 1] = id end
    table.sort(ids)
    return ids
end

function QuestLog.capture(api, candidates)
    local log = api.C_QuestLog or {}
    local entries, complete, total, found = modernEntries(log)
    if not entries then entries, complete, total, found = legacyEntries(api) end
    if entries and not complete and total and found < total then
        -- Visible rows may omit a collapsed category. Recover known IDs only if
        -- the live per-ID API and independent total agree on the entire log.
        for id in pairs(candidates) do
            if not entries[id] and flag(read(log.IsOnQuest, id)) == true then
                entries[id], found = {}, found + 1
            end
        end
        complete = found == total
    end
    local active, ready
    if complete then
        active, ready = sortedIds(entries), {}
        for _, id in ipairs(active) do
            local value = readyForTurnIn(log, id, entries[id])
            if value == nil then
                ready = nil
                break
            elseif value then
                ready[#ready + 1] = id
            end
        end
    end
    local completed = completedSet(api, log)
    return active, completed and sortedIds(completed), ready
end

function QuestLog.reader(api, ready)
    local log = api.C_QuestLog or {}
    local records, entries, entriesComplete, completed, offers = {}
    local function stateFor(id)
        if records[id] then return records[id] end
        local state = {}
        records[id] = state
        if not ready then return state end

        state.active = flag(read(log.IsOnQuest, id))
        if state.active == nil then
            local index = read(log.GetLogIndexForQuestID or api.GetQuestLogIndexByID, id)
            if type(index) == "number" and index > 0 then state.active = true end
        end
        if state.active == nil or state.active == true and not log.GetTitleForQuestID then
            if entries == nil then
                entries, entriesComplete = modernEntries(log)
                if entries == nil then entries, entriesComplete = legacyEntries(api) end
                entries = entries or {}
            end
            local entry = entries[id]
            if entry then
                state.active, state.name, state.ready = true, entry.title, entry.ready
            elseif state.active == nil and entriesComplete then
                state.active = false
            end
        end

        state.completed = flag(read(log.IsQuestFlaggedCompleted, id))
        if state.completed == nil then state.completed = flag(read(api.IsQuestFlaggedCompleted, id)) end
        if state.completed == nil then
            if completed == nil then completed = completedSet(api, log) or false end
            if completed then state.completed = not not completed[id] end
        end
        state.name = state.name or read(log.GetTitleForQuestID, id)
        if not state.name and QG.restedXP and QG.restedXP.authorized() then
            local saved = QG.engine.list(api.RXPCData)
            local global = QG.engine.list(api.RXPData)
            state.name = QG.engine.list(saved.questNameCache)[id] or QG.engine.list(global.questNames)[id]
        end
        if type(state.name) ~= "string" then state.name = nil end
        if state.name == "" then state.name = nil end
        if state.active == false and state.completed == false then
            state.disabled = flag(read(log.IsQuestDisabledForSession, id))
            if offers == nil then
                offers = {}
                local available = api.C_GossipInfo and read(api.C_GossipInfo.GetAvailableQuests)
                for _, entry in ipairs(QG.engine.list(available)) do
                    local questId = type(entry) == "table" and QG.engine.questId(entry.questID)
                    if questId then offers[questId] = true end
                end
            end
            state.offered = offers[id]
        end
        if state.active == true then
            state.ready = readyForTurnIn(log, id, state)
            local objectives = read(log.GetQuestObjectives, id)
            if type(objectives) == "table" then state.objectives = objectives end
            local total, elapsed = read(log.GetTimeAllowed, id)
            if positiveTime(api, total) and readable(api, elapsed) and type(elapsed) == "number" and
               elapsed >= 0 and elapsed <= total then
                state.timerSeconds = total
                state.remainingSeconds = total - elapsed
            end
        end
        return state
    end
    return stateFor
end

function QuestLog.player(api)
    local _, class = read(api.UnitClass, "player")
    local _, race = read(api.UnitRace, "player")
    return {
        level = read(api.UnitLevel, "player"),
        faction = read(api.UnitFactionGroup, "player"),
        class = class,
        race = race,
    }
end
