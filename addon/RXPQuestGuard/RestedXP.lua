local _, QG = ...
local R = {}
QG.restedXP = R
local E = QG.engine
local SOURCE = "RestedXP loaded guide"
R.authorizationNotice = "Guide analysis is off. Use /qg guides for permitted guide setup."
local guardTags = {
    isonquest = "active", isnotonquest = "not_active",
    isquestturnedin = "completed", isquestavailable = "not_completed",
    isquestcomplete = "ready", isquestnotcomplete = "not_ready",
}

local function contextAddon(api)
    if not api.LibStub then return nil end
    local ok, lib = pcall(function() return api.LibStub("AceAddon-3.0", true) end)
    if ok and lib and type(lib.GetAddon) == "function" then
        local found, addon = pcall(lib.GetAddon, lib, "RXPGuides", true)
        if found and type(addon) == "table" then return addon end
    end
end

local function metadata(api)
    local fn = api.C_AddOns and api.C_AddOns.GetAddOnMetadata or api.GetAddOnMetadata
    if type(fn) == "function" then
        local ok, version = pcall(fn, "RXPGuides", "Version")
        if ok then return version end
    end
end

local function stableStepId(value)
    local id = tonumber(value)
    if id and math.abs(id) < 2^53 and id == math.floor(id) then return id end
end

function R.authorized()
    return QG.settings ~= nil and QG.settings.authorizedGuides == true
end

function R.context(api)
    if not R.authorized() then return {version = metadata(api), status = "Guide analysis off"} end
    local addon = contextAddon(api)
    local saved = E.list(api.RXPCData)
    local guide = addon and addon.currentGuide
    local steps = type(guide) == "table" and guide.steps
    local guideProgress = type(guide) == "table" and E.list(saved.guideProgress)[guide.key]
    local index = E.questId(type(guideProgress) == "table" and guideProgress.step) or E.questId(saved.currentStep)
    local result = {addon = addon, version = addon and addon.release or metadata(api), index = index,
        group = saved.currentGuideGroup, name = saved.currentGuideName}
    if type(steps) == "table" and index and type(steps[index]) == "table" and
       type(steps[index].elements) == "table" and
       type(guide.name) == "string" and not guide.empty and
       (not saved.currentGuideName or saved.currentGuideName == guide.name) then
        result.guide, result.group, result.name = guide, guide.group, guide.name
        result.stepId = stableStepId(steps[index].stepId)
        if not result.stepId and type(guideProgress) == "table" and E.questId(guideProgress.step) == index then
            result.stepId = stableStepId(guideProgress.stepId)
        end
        if not result.stepId and saved.currentGuideName == guide.name and saved.currentGuideGroup == guide.group and
           E.questId(saved.currentStep) == index then
            result.stepId = stableStepId(saved.currentStepId)
        end
        result.active = E.list(addon.RXPFrame and addon.RXPFrame.activeSteps or api.RXPFrame and api.RXPFrame.activeSteps)
        result.skipped = E.list(saved.stepSkip)
        result.skipGuards = addon.settings and addon.settings.profile and addon.settings.profile.disableAutoSkip
        result.status = "Connected"
    elseif addon then
        result.status = "Guide unavailable or incompatible"
    elseif result.version then
        result.status = "RestedXP disabled or incompatible"
    else
        result.status = "RestedXP unavailable"
    end
    return result
end

local function location(element, api)
    local x, y, id = tonumber(element.x), tonumber(element.y), E.questId(element.zone)
    if not (x and y and x >= 0 and x <= 100 and y >= 0 and y <= 100) then return nil end
    local zone
    if id and api.C_Map and type(api.C_Map.GetMapInfo) == "function" then
        local ok, info = pcall(api.C_Map.GetMapInfo, id)
        if ok and type(info) == "table" then zone = info.name end
    end
    return {zone = zone, mapId = id, x = x, y = y}
end

local function unsupportedTag(tag)
    return tag:sub(1, 2) == "is" or tag == "skiponquest" or tag == "skipto" or
        tag == "spellmissing" or tag == "subzoneskip" or tag == "dmf" or tag == "nodmf" or
        tag == "holiday" or tag == "dungeon" or tag == "singlebox" or tag == "next"
end

local function actionId(id, guide, stateFor)
    local aliases = E.list(guide.mirrorID)[id]
    if type(aliases) == "table" then
        for _, alias in ipairs(aliases) do
            local alternate = E.questId(alias)
            if alternate and stateFor(alternate).active == true then return alternate end
        end
    end
    return id, type(aliases) == "table" and #aliases > 0
end

function R.step(guide, raw, index, opts)
    local skip, current = opts.skipped, opts.current
    local level = tonumber(raw.level)
    local filter = guide.enabledFor or guide.faction
    local faction = E.factionApplicability(filter, opts.player)
    local profileConditional = not opts.live and type(filter) == "string" and filter ~= "" and
        filter:lower() ~= "horde" and filter:lower() ~= "alliance"
    local step = {
        chapter = guide.name, chapterId = R.guideKey(guide), index = index, source = SOURCE, current = current,
        optional = not not raw.optional, actions = {}, guards = {},
        skipped = skip or raw.disabled or raw.tip or raw.hidetip or current and raw.hidewindow or faction == false,
        playerSkipped = not not skip,
        conditional = opts.branch or profileConditional or faction == nil or (not current and (raw.requires ~= nil or raw.sticky or raw.include or
            raw.hidewindow or level and (not opts.player.level or level > opts.player.level))),
    }
    local waypoints, pickup = {}, false
    for _, element in ipairs(E.list(raw.elements)) do
        if type(element) == "table" then
            local tag = type(element.tag) == "string" and element.tag:lower() or ""
            if tag == "goto" or tag == "waypoint" then
                waypoints[#waypoints + 1] = element
            elseif tag == "accept" or tag == "complete" or tag == "turnin" or tag == "abandon" then
                local rawId = tonumber(element.questId)
                local id = rawId and E.questId(math.abs(rawId))
                if id then
                    pickup = pickup or tag == "accept"
                    local unresolved
                    id, unresolved = actionId(id, guide, opts.stateFor)
                    local title = type(element.title) == "string" and element.title ~= "" and element.title or nil
                    step.actions[#step.actions + 1] = {
                        questId = id, kind = tag, name = title, objectiveIndex = E.questId(element.obj),
                        optional = element.optional, conditional = unresolved,
                        skipIfMissing = element.skipIfMissing or rawId < 0,
                        skipped = element.disabled or (current and element.skip) or element.textOnly,
                        finished = current and element.completed,
                        requiredTurnIn = E.questId(element.requiredTurnIn),
                    }
                end
            elseif guardTags[tag] and not opts.skipGuards then
                local ids = {}
                local conditional = element.account
                for _, value in ipairs(type(element.questIds) == "table" and element.questIds or {element.questId}) do
                    local id = E.questId(value)
                    if id then
                        local unresolved
                        id, unresolved = actionId(id, guide, opts.stateFor)
                        conditional = conditional or unresolved
                        ids[#ids + 1] = id
                    end
                end
                step.guards[#step.guards + 1] = {kind = guardTags[tag], questIds = ids, conditional = conditional}
            elseif not guardTags[tag] and unsupportedTag(tag) then
                step.conditional = true
            end
            if tag == "skipto" or tag == "skiponquest" or tag == "next" or element.skipTo then
                step.jumps = true
            end
        end
    end
    if pickup and #waypoints == 1 then
        local point = location(waypoints[1], opts.api)
        for _, action in ipairs(step.actions) do
            if action.kind == "accept" then action.location = point end
        end
    end
    return step
end

function R.lookupPickup(context, id, opts)
    local index = context.addon and context.addon.pickUpList
    local found
    for _, entry in ipairs(E.list(E.list(index)[id])) do
        if type(entry) == "table" then
            local guide, raw = entry.guide, entry.step
            if type(guide) == "table" and type(raw) == "table" and type(guide.name) == "string" and
               (not context.group or guide.group == context.group) then
                local step = R.step(guide, raw, E.questId(raw.index), opts)
                for _, action in ipairs(step.actions) do
                    if action.questId == id and action.kind == "accept" then
                        if found and (found.chapter ~= guide.name or found.index ~= step.index) then return nil end
                        found = {chapter = guide.name, index = step.index, source = SOURCE,
                            unknownOrder = true, unverified = true, location = action.location}
                    end
                end
            end
        end
    end
    return found
end

local function cleanName(value)
    return value:match("^%s*(.-)%s*$")
end

local function nextTarget(guide)
    local target = guide.next
    if type(target) ~= "string" or target == "" then return end
    if target:find(";", 1, true) then return nil, nil, "RestedXP lists alternative next chapters." end
    local group, name = target:match("^(.-)\\(.*)$")
    return cleanName(group or guide.group or ""), cleanName(name or target)
end

local function guideKey(guide)
    return (guide.group or "") .. "|" .. (guide.subgroup or "") .. "|" .. (guide.name or "")
end
R.guideKey = guideKey

function R.loadedRoute(context, stopAfterNext)
    local current, addon = context.guide, context.addon
    local guides = addon and addon.guides
    if not current or type(guides) ~= "table" then
        return {current}, 1, nil, {status = "unavailable"}
    end
    local byKey, byName, predecessors = {}, {}, {}
    for _, guide in pairs(guides) do
        if type(guide) == "table" and type(guide.name) == "string" and not guide.empty then
            local key = guideKey(guide)
            if not byKey[key] then
                byKey[key] = guide
                local rawName = (guide.group or "") .. "|" .. guide.name
                if byName[rawName] ~= nil then byName[rawName] = false else byName[rawName] = guide end
                local group, name = nextTarget(guide)
                if group then
                    local target = group .. "|" .. name
                    if predecessors[target] ~= nil then predecessors[target] = false else predecessors[target] = guide end
                end
            end
        end
    end
    local route, seen, position = {current}, {[guideKey(current)] = true}, 1
    local pointer = current
    for _ = 1, 40 do
        local earlier = predecessors[(pointer.group or "") .. "|" .. pointer.name]
        if not earlier or seen[guideKey(earlier)] then break end
        table.insert(route, 1, earlier)
        position = position + 1
        seen[guideKey(earlier)], pointer = true, earlier
    end
    pointer = current
    local notice
    local coverage = {status = "linked"}
    for _ = 1, stopAfterNext and 1 or 40 do
        local group, name, problem = nextTarget(pointer)
        if problem then
            notice, coverage.status = problem, "ambiguous"
        elseif not group then
            coverage.status = "terminal"
        end
        if not group then break end
        local later = byName[group .. "|" .. name]
        if not later then
            notice = "The named next chapter is unavailable or ambiguous."
            coverage.status = "unavailable"
            break
        end
        if seen[guideKey(later)] then
            notice = "The loaded guide route contains a loop."
            coverage.status = "ambiguous"
            break
        end
        route[#route + 1], seen[guideKey(later)], pointer = later, true, later
    end
    local maximum = type(pointer.name) == "string" and pointer.name:match("^%d+%s*%-%s*(%d+)%s") or nil
    if not stopAfterNext and coverage.status == "linked" then coverage.status = "limited" end
    coverage.lastChapter = pointer.name
    coverage.lastLevel = tonumber(maximum)
    return route, position, notice, coverage
end

function R.matchRoute(route, context)
    if not context.guide then return nil end
    if #route == 0 then return nil end
    local result
    for index, chapter in ipairs(route) do
        if chapter.name == context.name and (chapter.group or "") == (context.group or "") and
           (chapter.subgroup or "") == (context.guide.subgroup or "") then
            if result then return nil, "Several published chapters match the active guide.", {status = "mismatched"} end
            result = index
        end
    end
    if not result then
        return nil, "Published route does not contain the active guide variant.", {status = "mismatched"}
    end
    local chapter = route[result]
    if chapter.id and chapter.id ~= guideKey(context.guide) then
        return nil, "Published chapter identity does not match the loaded guide.", {status = "mismatched"}
    end
    local expected, loaded = E.questId(chapter.guideVersion), E.questId(context.guide.version)
    if expected and loaded and expected ~= loaded then
        return nil, "Published route is stale for this guide version. Republish the route and use /reload.",
            {status = "stale", guideVersion = loaded, expectedGuideVersion = expected}
    end
    return result, nil, {status = "matched", guideVersion = loaded, expectedGuideVersion = expected}
end
