local addonName, QG = ...
local Snapshot = {}
QG.snapshot = Snapshot

local E, Q, R = QG.engine, QG.questLog, QG.restedXP
local captureDelay = 0.65
local inWorld, logSettled, pending = false, false, false
local ticket
local accepted = {}
local snapshotEvents = {
    QUEST_LOG_UPDATE = true, QUEST_ACCEPTED = true, QUEST_REMOVED = true,
    QUEST_TURNED_IN = true, UNIT_QUEST_LOG_CHANGED = true, PLAYER_LEVEL_UP = true,
    QUEST_QUERY_COMPLETE = true,
}

local function trusted(api, value)
    return type(api.issecretvalue) ~= "function" or not api.issecretvalue(value)
end

local function text(api, value)
    if trusted(api, value) and type(value) == "string" then
        value = value:match("^%s*(.-)%s*$")
        if value and value ~= "" then return value end
    end
end

local function number(api, value)
    if trusted(api, value) and type(value) == "number" and value > 0 and value < math.huge then
        return math.floor(value)
    end
end

local function call(fn, ...)
    if type(fn) == "function" then
        local ok, a, b, c, d = pcall(fn, ...)
        if ok then return a, b, c, d end
    end
end

local function forever(api, version)
    return (api.WOW_PROJECT_CAMELOT and api.WOW_PROJECT_ID == api.WOW_PROJECT_CAMELOT) or
        (version and version:match("^1%.60%."))
end

local function identity(api)
    local patch, build, _, interface = call(api.GetBuildInfo)
    patch, interface = text(api, patch), number(api, interface)
    local given, surname = call(api.UnitName, "player")
    given = text(api, given)
    if given == api.UNKNOWNOBJECT or given == api.UNKNOWN then given = nil end
    if given and forever(api, patch) then
        surname = text(api, surname)
        if surname and not given:find("[%s%-]") then given = given .. " " .. surname end
    end
    local realm = text(api, call(api.GetRealmName))
    local normalizedRealm = text(api, call(api.GetNormalizedRealmName))
    local realmId = number(api, call(api.GetRealmID))
    local player = Q.player(api)
    return {
        character = given,
        realm = realm or normalizedRealm or (realmId and tostring(realmId)),
        normalizedRealm = normalizedRealm,
        realmId = realmId,
        clientVersion = patch,
        clientBuild = text(api, build) or number(api, build),
        clientInterface = interface,
        level = number(api, player.level),
        faction = text(api, player.faction),
        class = text(api, player.class),
        race = text(api, player.race),
    }
end

local function key(value)
    if type(value) == "string" then return value:lower():match("^%s*(.-)%s*$") end
end

local function characterKey(value)
    local normalized = key(value)
    return normalized and normalized:gsub("[%s%-]+", "-")
end

local function realmMatches(wanted, live)
    wanted = key(wanted)
    return wanted and wanted ~= "" and (wanted == key(live.realm) or wanted == key(live.normalizedRealm) or
        (live.realmId and wanted == tostring(live.realmId)))
end

local function routeState(route, details, status, message)
    details.status, details.message = status, message
    return route, message, details
end

local function versionKey(value)
    local normalized = key(value)
    return normalized and normalized:gsub("^v", "")
end

local function terminalMatches(route, info, target)
    local last = route[#route]
    if type(last) ~= "table" or type(info.terminalChapterId) ~= "string" then return false end
    for _, chapter in ipairs(route) do
        if type(chapter) ~= "table" or #E.list(chapter.steps) == 0 then return false end
    end
    local minimum, maximum = E.questId(last.levelMin), E.questId(last.levelMax)
    return last.id == info.terminalChapterId and minimum and maximum and minimum <= target and maximum >= target
end

function Snapshot.routeFor(api, data, guideContext)
    local details = {complete = false, trusted = false}
    if not R.authorized() then
        return routeState({}, details, "disabled", R.authorizationNotice)
    end
    if data.schemaVersion ~= 1 then
        return routeState({}, details, "unavailable", "Published route unavailable. Unsupported data format. Publish again and use /reload.")
    end
    local route = E.list(data.route)
    if #route == 0 then
        return routeState({}, details, "unavailable", "Published route unavailable. Publish a route from the desktop and use /reload.")
    end
    local info = E.list(data.routeInfo)
    details.targetLevel, details.terminalChapterId = E.questId(info.targetLevel), info.terminalChapterId
    details.guideRelease, details.clientVersion = info.guideRelease, info.clientVersion
    details.publishedAt, details.chapterCount = data.publishedAt, #route
    local context = data.routeContext
    if context == nil then
        return routeState(route, details, "unknown", "Published route loaded · legacy route with unverified ownership and coverage.")
    end
    if type(context) ~= "table" then
        return routeState({}, details, "mismatched", "Published route identity is unreadable.")
    end
    local ok, live = pcall(identity, api)
    if not ok or not live.character or not live.realm then
        return routeState({}, details, "unknown", "Published route is waiting for a verified character and realm.")
    end
    if characterKey(context.character) ~= characterKey(live.character) then
        return routeState({}, details, "mismatched", "Published route belongs to a different character.")
    end
    if not realmMatches(context.realm, live) then
        return routeState({}, details, "mismatched", "Published route belongs to a different or unverified realm.")
    end
    if context.clientInterface ~= nil then
        local expected = E.questId(context.clientInterface)
        if not expected or expected ~= live.clientInterface then
            return routeState({}, details, "mismatched", "Published route targets a different or unverified client.")
        end
    end
    if (context.faction and key(context.faction) ~= key(live.faction)) or
       (context.class and key(context.class) ~= key(live.class)) then
        return routeState({}, details, "mismatched", "Published route targets a different faction or class.")
    end
    local metadata = api.C_AddOns and api.C_AddOns.GetAddOnMetadata or api.GetAddOnMetadata
    local liveRelease = versionKey(guideContext and guideContext.version or call(metadata, "RXPGuides", "Version"))
    local expectedRelease = versionKey(info.guideRelease)
    if expectedRelease and liveRelease and expectedRelease ~= liveRelease then
        return routeState({}, details, "stale",
            "Published route is stale for this RestedXP release. Republish the route and use /reload.")
    end
    local expectedClient, liveClient = versionKey(info.clientVersion), versionKey(live.clientVersion)
    if expectedClient and liveClient then
        local full = live.clientBuild and liveClient .. "." .. live.clientBuild
        if expectedClient ~= liveClient and expectedClient ~= full then
            return routeState({}, details, "stale",
                "Published route is stale for this game client version. Republish the route and use /reload.")
        end
    end
    if guideContext and guideContext.guide then
        local _, mismatch, active = R.matchRoute(route, guideContext)
        if active and active.status == "stale" then
            return routeState({}, details, "stale", mismatch)
        end
    end
    if not expectedRelease or not liveRelease or not expectedClient or not liveClient then
        return routeState(route, details, "unknown", "Published route loaded · source version or full coverage unverified.")
    end
    details.trusted = true
    details.complete = info.complete == true and details.targetLevel ~= nil and
        terminalMatches(route, info, details.targetLevel) == true
    if info.complete == true and not details.complete then
        details.trusted = false
        return routeState(route, details, "unknown", "Published route loaded · its stated terminal chapter could not be verified.")
    end
    return routeState(route, details, "verified")
end

local function record(raw, live)
    if type(raw) ~= "table" or raw.schemaVersion ~= 1 or
       characterKey(raw.character) ~= characterKey(live.character) or not realmMatches(raw.realm, live) then return end
    return raw
end

local function stepFlags(flags)
    local result = {}
    for index, value in pairs(E.list(flags)) do
        local step = E.questId(index)
        if step and (value == true or value == 1) then result[#result + 1] = step end
    end
    table.sort(result)
    return result
end

local function capture(api, previous, settled, hints)
    local live = identity(api)
    if not live.character or not live.realm then return nil, "Waiting for character identity" end
    local capturedAt = number(api, call(api.GetServerTime)) or number(api, call(api.time))
    if not capturedAt then return nil, "Capture time unavailable" end
    previous = record(previous, live)

    local candidates = {}
    for id in pairs(hints) do candidates[id] = true end
    for _, value in ipairs(E.list(previous and previous.active)) do
        local id = E.questId(value)
        if id then candidates[id] = true end
    end
    local active, completed, ready = Q.capture(api, candidates)
    -- During initial world entry, empty lists can precede quest data loading.
    if not settled then
        if active and #active == 0 then active, ready = nil, nil end
        if completed and #completed == 0 then completed = nil end
    end
    if previous then
        if type(previous.active) == "table" and active == nil then
            return nil, "Quest log is incomplete. Keeping the previous capture"
        end
        if type(previous.completed) == "table" and (completed == nil or
           (#completed == 0 and #previous.completed > 0)) then
            return nil, "Quest history is incomplete. Keeping the previous capture"
        end
    end

    local context = R.context(api)
    local getMetadata = api.C_AddOns and api.C_AddOns.GetAddOnMetadata or api.GetAddOnMetadata
    local snapshot = {
        schemaVersion = 1, capturedAt = capturedAt, character = live.character, realm = live.realm,
        realmId = live.realmId, level = live.level, faction = live.faction, class = live.class, race = live.race,
        clientVersion = live.clientVersion, clientBuild = live.clientBuild and tostring(live.clientBuild),
        clientInterface = live.clientInterface,
        restedXPVersion = text(api, context.version),
        addonVersion = text(api, call(getMetadata, addonName, "Version")),
        active = active, completed = completed, ready = ready,
    }
    if context.guide then
        snapshot.currentGuide = text(api, context.name)
        snapshot.currentGuideGroup = text(api, context.group)
        snapshot.currentGuideSubgroup = text(api, context.guide.subgroup)
        snapshot.currentGuideKey = text(api, context.guide.key)
        snapshot.currentStep = context.index
        snapshot.currentStepId = context.stepId
        snapshot.skippedSteps = stepFlags(context.skipped)
    elseif previous and previous.currentGuide and context.addon and not settled then
        return nil, "Waiting for the active guide. Keeping the previous capture"
    end
    return snapshot, active and completed and "Character snapshot captured" or "Partial character snapshot captured"
end

local function inCombat()
    return call(_G.InCombatLockdown) == true
end

local function flush()
    if not inWorld or not pending then return end
    if inCombat() then
        Snapshot.status = "Snapshot capture queued until combat ends"
        return
    end
    pending = false
    local ok, result, status = pcall(capture, _G, RXPQuestGuardSnapshot, logSettled, accepted)
    Snapshot.status = ok and status or "Snapshot APIs unavailable. Keeping the previous capture"
    if ok and result then
        RXPQuestGuardSnapshot = result
        if result.active then accepted = {} end
    end
end

function Snapshot.request()
    if not inWorld then return end
    pending = true
    if ticket then return end
    if inCombat() or not (C_Timer and type(C_Timer.After) == "function") then
        flush()
        return
    end
    local scheduled = {}
    ticket = scheduled
    C_Timer.After(captureDelay, function()
        if ticket ~= scheduled then return end
        ticket = nil
        flush()
    end)
end

function Snapshot.event(event, first, second)
    if event == "PLAYER_ENTERING_WORLD" then
        inWorld, logSettled = true, false
        Snapshot.request()
    elseif event == "PLAYER_LEAVING_WORLD" or event == "PLAYER_LOGOUT" then
        -- WoW is responsible for persisting SavedVariables during reload or logout.
        pending = inWorld
        flush()
        inWorld, pending, ticket = false, false, nil
    elseif event == "PLAYER_REGEN_ENABLED" then
        if pending then Snapshot.request() end
    elseif snapshotEvents[event] and (event ~= "UNIT_QUEST_LOG_CHANGED" or first == "player") then
        if event == "QUEST_LOG_UPDATE" or event == "QUEST_REMOVED" or event == "QUEST_ACCEPTED" then
            logSettled = inWorld or logSettled
        end
        if event == "QUEST_ACCEPTED" and inWorld then
            local id = E.questId(second or first)
            if id then accepted[id] = true end
        end
        Snapshot.request()
    end
end
