local _, QG = ...
local E = {}
QG.engine = E

function E.questId(value)
    local id = tonumber(value)
    if id and id >= 1 and id <= 2147483647 and id == math.floor(id) then return id end
end

function E.list(value)
    if type(value) == "table" then return value end
    return {}
end

function E.status(state)
    if state.active == true then
        return state.ready == true and "Ready to turn in" or "Active"
    end
    if state.completed == true then return "Completed" end
    if state.active == false and state.completed == false then
        return state.offered and "Available to accept" or "Not accepted"
    end
    return "Unknown"
end

function E.factionApplicability(faction, player)
    if type(faction) ~= "string" then return true end
    local expected = faction:lower()
    if expected ~= "horde" and expected ~= "alliance" then return true end
    if type(player.faction) == "string" then return expected == player.faction:lower() end
end

function E.conflict(meta, field)
    for _, conflict in ipairs(E.list(meta.conflicts)) do
        if type(conflict) == "table" and conflict.field == field and conflict.unresolved ~= false then
            return true
        end
    end
    return false
end

local function gameBuild(value)
    local text = type(value) == "string" and value or type(value) == "number" and tostring(value)
    if text then
        return text:match("^%d+%.%d+%.%d+[. +_-](%d+)$") or text:match("^WOW%-(%d+)patch") or text:match("^(%d+)$")
    end
end

local currentBuild
if type(GetBuildInfo) == "function" then
    local _, build = GetBuildInfo()
    currentBuild = gameBuild(build)
end

local function weakClaim(claim)
    local confidence = type(claim.confidence) == "string" and claim.confidence:lower() or ""
    local sourcedBuild = gameBuild(claim.gameVersion)
    return (currentBuild and sourcedBuild and currentBuild ~= sourcedBuild) or
        claim.inheritedClassic == true or claim.guideDerived == true or confidence:find("classic", 1, true) or
        confidence:find("guide", 1, true) or confidence:find("unverified", 1, true)
end

local function advisory(meta, field)
    local claims = E.list(E.list(meta.fieldEvidence)[field])
    local hasWeak = false
    for _, claim in ipairs(claims) do
        if type(claim) == "table" then
            if not weakClaim(claim) then return false end
            hasWeak = true
        end
    end
    return hasWeak
end

function E.actionable(meta, field)
    return not E.conflict(meta, field) and not advisory(meta, field)
end
local actionable = E.actionable

local function matchesRestriction(meta, player, future)
    local level = actionable(meta, "minLevel") and tonumber(meta.minLevel) or nil
    local faction = E.factionApplicability(actionable(meta, "faction") and meta.faction, player)
    if faction == false then return false end
    local classes = E.list(actionable(meta, "classes") and meta.classes)
    if #classes > 0 and type(player.class) == "string" then
        local matches = false
        for _, class in ipairs(classes) do
            if type(class) == "string" and class:upper() == player.class:upper() then
                matches = true
                break
            end
        end
        if not matches then return false end
    end
    if level and player.level and player.level < level then
        if not future then return false end
        return nil
    end
    if faction == nil or level and not player.level or #classes > 0 and not player.class then return nil end
    return true
end

local function requirementGroup(group, stateFor, planned)
    local unknown = false
    for _, requirement in ipairs(group) do
        local id = type(requirement) == "table" and E.questId(requirement.questId)
        local state = id and stateFor(id) or {}
        local field = id and requirement.state == "active" and "active" or "completed"
        local forecast = planned[id]
        local actual = state[field]
        if forecast and forecast[field] ~= nil then actual = forecast[field] end
        if actual == false then return false end
        if actual == nil then unknown = true end
    end
    if not unknown then return true end
end

-- A flat list is all-of. Each group is all-of and the groups are alternatives.
function E.requirements(meta, stateFor, planned)
    if not actionable(meta, "requirements") or not actionable(meta, "requirementGroups") then return nil end
    local fixed = requirementGroup(E.list(meta.requirements), stateFor, planned)
    if fixed == false then return false end
    local groups = E.list(meta.requirementGroups)
    if #groups == 0 then return fixed end
    local unknown = false
    for _, group in ipairs(groups) do
        local possible
        if type(group) == "table" and #group > 0 then possible = requirementGroup(group, stateFor, planned) end
        if possible == true then return fixed end
        if possible == nil then unknown = true end
    end
    if not unknown then return false end
end

local function starterCandidate(meta)
    if type(meta.startItem) == "table" and E.questId(meta.startItem.itemId) then return meta.startItem end
    for _, claim in ipairs(E.list(E.list(meta.fieldEvidence).startItem)) do
        local value = type(claim) == "table" and claim.value
        if type(value) == "table" and E.questId(value.itemId) then return value end
    end
end

function E.inspectStarter(meta, state, questId, inventory)
    local candidate = starterCandidate(meta)
    local candidateId = candidate and E.questId(candidate.itemId)
    local observed, count, bagItem
    if inventory then observed, count, bagItem = inventory(questId, candidateId) end
    local missing = state.active == false and state.completed == false
    if type(observed) == "table" and E.questId(observed.itemId) and missing then
        return {itemId = observed.itemId, name = observed.name or
            (candidateId == observed.itemId and candidate and candidate.name), present = true,
            verified = true, source = "Live WoW bag API", disagrees = candidateId and candidateId ~= observed.itemId}
    end
    if not candidateId then return nil end
    local disagrees = type(bagItem) == "table" and (bagItem.conflicting or
        bagItem.questId and (bagItem.questId ~= questId or bagItem.active == true and missing))
    local trusted = meta.startItem == candidate and candidate.verifiedForever == true and
        actionable(meta, "startItem") and not disagrees
    local present
    if type(count) == "number" then present = count > 0 end
    return {itemId = candidateId, name = candidate.name, present = present, verified = trusted,
        disagrees = disagrees, source = trusted and (meta.source or "Published Forever evidence") or nil}
end

function E.canActivate(meta, state, item, player, stateFor)
    return item and item.verified and item.present == true and state.active == false and
        state.completed == false and not state.disabled and matchesRestriction(meta, player, false) ~= false and
        E.requirements(meta, stateFor, {}) ~= false
end

function E.timer(meta, state)
    if state.active and type(state.timerSeconds) == "number" then
        return state.timerSeconds, state.remainingSeconds
    end
    local duration = actionable(meta, "timerSeconds") and tonumber(meta.timerSeconds)
    if duration and duration > 0 and duration < math.huge and duration == math.floor(duration) then return duration end
end

function E.duration(seconds)
    local minutes = math.floor(seconds / 60)
    local hours = math.floor(minutes / 60)
    if hours > 0 then
        if minutes % 60 == 0 then return hours .. " hr" end
        return hours .. " hr " .. (minutes % 60) .. " min"
    end
    if minutes > 0 then return minutes .. " min" end
    return math.ceil(seconds) .. " sec"
end

local function guardValue(guard, stateFor, planned)
    local kind = guard.kind
    if kind == "unknown" or guard.conditional then return nil end
    local ids = E.list(guard.questIds or guard.quest_ids)
    if #ids == 0 then return nil end
    local reverse = kind == "not_active" or kind == "not_completed" or kind == "not_ready"
    local all = kind == "not_active"
    local field = kind == "active" or kind == "not_active"
    field = field and "active" or (kind == "ready" or kind == "not_ready") and "ready" or "completed"
    local unknown = false
    for _, rawId in ipairs(ids) do
        local id = E.questId(rawId)
        local state = id and stateFor(id) or {}
        local forecast = planned[id]
        local value = state[field]
        local active = state.active
        if forecast and forecast.active ~= nil then active = forecast.active end
        if field == "ready" and active == false then value = false end
        if forecast and forecast[field] ~= nil then value = forecast[field] end
        if value == nil then
            unknown = true
        else
            if reverse then value = not value end
            if all ~= value then return value end
        end
    end
    if not unknown then return all end
end

local function applicability(step, stateFor, planned, future)
    local uncertain = step.conditional
    for _, guard in ipairs(E.list(step.guards)) do
        local value = guardValue(guard, stateFor, planned)
        if value == false and not future then return false end
        -- A currently-false condition can change along a future branch.
        if value ~= true then uncertain = true end
    end
    if not uncertain then return true end
end

local function pickup(known, id, step, action)
    local previous = known[id]
    local location = action.location
    if previous and previous.chapter == step.chapter and previous.index == step.index and
       type(previous.location) == "table" then
        local merged = {}
        for name, value in pairs(previous.location) do merged[name] = value end
        for name, value in pairs(E.list(location)) do merged[name] = value end
        if location and location.x then merged.mapId = location.mapId end
        location = merged
    end
    known[id] = {
        chapter = step.chapter,
        index = step.index,
        conditional = step.conditional or action.conditional or action.requiredTurnIn ~= nil or #E.list(step.guards) > 0,
        optional = step.optional or action.optional,
        source = step.source,
        location = location,
        flagged = step.playerSkipped,
        unverified = step.pickupUnverified or action.pickupUnverified,
        current = step.current == true,
    }
end

local function guardProtects(step, id)
    for _, guard in ipairs(E.list(step.guards)) do
        local ids = E.list(guard.questIds or guard.quest_ids)
        if not guard.conditional and #ids == 1 and E.questId(ids[1]) == id and
           (guard.kind == "active" or guard.kind == "ready" or guard.kind == "completed") then
            return true
        end
    end
    return false
end

local function classify(meta, step, action)
    local classification = actionable(meta, "classification") and meta.classification
    if step.optional or action.optional or classification == "optional" or
       classification == "breadcrumb" then return "Skippable" end
    if classification == "mandatory" then return "Mandatory" end
    return "Unknown"
end

local function pickupLocation(meta, earlier)
    if E.conflict(meta, "startLocation") then return nil, true end
    local supplied = type(meta.startLocation) == "table" and meta.startLocation
    local source = supplied and type(supplied.source) == "string" and supplied.source:lower() or ""
    if not actionable(meta, "startLocation") or source:find("classic", 1, true) then supplied = nil end
    local guideLocation = earlier and earlier.location
    if supplied and guideLocation and supplied.mapId and guideLocation.mapId == supplied.mapId and
       type(supplied.x) == "number" and type(supplied.y) == "number" and
       type(guideLocation.x) == "number" and type(guideLocation.y) == "number" and
       (math.abs(supplied.x - guideLocation.x) > 3 or math.abs(supplied.y - guideLocation.y) > 3) then
        return nil, true, supplied, guideLocation
    end
    return supplied or guideLocation, false
end

local function rowFor(meta, state, step, action, id, prior, atCurrent)
    local name = state.name or meta.name or action.name
    local location, conflict, published, guideLocation = pickupLocation(meta, prior[id])
    return {
        key = (step.chapterId or step.chapter or "?") .. ":" .. (step.index or "?") .. ":" .. id ..
            (action.kind == "accept" and ":pickup" or ":objective"),
        id = id, name = name or ("Quest " .. id), meta = meta, state = state,
        status = E.status(state), kind = action.kind, objectiveIndex = action.objectiveIndex,
        chapter = step.chapter, index = step.index, source = step.source,
        pickup = prior[id], location = location, locationConflict = conflict,
        publishedLocation = published, guideLocation = guideLocation,
        classification = classify(meta, step, action), current = atCurrent, guarded = guardProtects(step, id),
    }
end

local function categorize(report, row, category)
    row.category = category
    local key = category .. ":" .. row.id
    if not report.counted[key] then
        report.counted[key] = true
        report.counts[category] = report.counts[category] + 1
    end
end

local function addIssue(report, row, category, code, message, severity)
    categorize(report, row, category)
    if report.seen[row.key] then return end
    report.seen[row.key] = true
    row.code, row.message, row.severity = code, message, severity
    report.issues[#report.issues + 1] = row
end

local function knownPickup(row)
    local earlier = row.pickup
    return earlier and not earlier.unknownOrder and not earlier.ahead and not earlier.unverified and
        not earlier.conditional and not earlier.optional
end

local function explainDependencies(row, quests, stateFor)
    local summary = {}
    local function append(group, option)
        for _, requirement in ipairs(E.list(group)) do
            local id = type(requirement) == "table" and E.questId(requirement.questId)
            if id then
                local state = stateFor(id)
                local name = state.name or E.list(quests[id]).name
                local condition = requirement.state == "active" and "active" or "completed"
                local value = state[condition]
                local status = "Unknown"
                if value == true then
                    status = condition == "active" and "Active" or "Completed"
                elseif value == false then
                    status = condition == "active" and "Not active" or "Not completed"
                end
                summary[#summary + 1] = {id = id, name = name, requirement = condition, result = status, option = option}
            end
            if #summary >= 24 then return end
        end
    end
    if actionable(row.meta, "requirements") and actionable(row.meta, "requirementGroups") then
        append(row.meta.requirements)
        for index, group in ipairs(E.list(row.meta.requirementGroups)) do
            if #summary >= 24 then break end
            append(group, index)
        end
    end
    row.dependencies = summary
end

local function pickupAction(report, row, info, stateFor, planned)
    local id, action = row.id, info.action
    if info.satisfied then
        categorize(report, row, "already_satisfied")
        return
    end
    local required = E.requirements(row.meta, stateFor, planned)
    local gated = E.questId(action.requiredTurnIn)
    local gateMet = not gated or requirementGroup({{questId = gated}}, stateFor, planned) == true
    local optional = row.classification == "Skippable"
    if info.runnable == false then
        row.status = "Not eligible by known restrictions"
        if row.current then
            addIssue(report, row, "needs_review", "restriction",
                "The player does not match a known quest restriction.", "review")
        end
    elseif required == false and row.current and info.missing and not info.uncertain and gateMet then
        row.status = "Prerequisites missing"
        addIssue(report, row, optional and "optional_objective" or "confirmed_blocker",
            "prerequisite", "A documented quest prerequisite is unsatisfied.", optional and "info" or "blocker")
    else
        row.status = row.current and row.status or "Future planned quest"
        categorize(report, row, "future_planned")
        if info.runnable == true and required == true and gateMet and not info.uncertain and not optional and
           not action.skipIfMissing then
            planned[id] = {active = true}
        end
    end
end

local function objectiveAction(report, row, info, stateFor, planned)
    local id, action = row.id, info.action
    if info.satisfied then
        categorize(report, row, "already_satisfied")
        if row.current and row.status == "Completed" and not action.finished then
            addIssue(report, row, "already_satisfied", "already-completed",
                "The game records this quest as completed.", "info")
        end
    elseif info.currentPickup or not row.current and (info.projected or info.intended) then
        row.status = "Future planned quest"
        categorize(report, row, "future_planned")
    elseif info.runnable ~= false and not action.skipIfMissing and not row.guarded and info.missing then
        local earlier = knownPickup(row)
        local optional = row.classification == "Skippable"
        local absent = "The objective requires a quest absent from the live log and completion history."
        if info.uncertain then
            addIssue(report, row, "needs_review", "unaccepted",
                "The objective depends on a branch whose conditions may change.", "review")
        elseif optional then
            addIssue(report, row, "optional_objective", "unaccepted", absent .. " This is skippable.", "info")
        elseif row.current and E.requirements(row.meta, stateFor, planned) == false then
            addIssue(report, row, "confirmed_blocker", "prerequisite",
                absent .. " A documented prerequisite is unsatisfied.", "blocker")
        elseif earlier then
            addIssue(report, row, "confirmed_missing_pickup", "unaccepted",
                absent .. " The applicable pickup is earlier in the route.", "warning")
        elseif row.current then
            addIssue(report, row, "unaccepted_objective", "unaccepted", absent, "warning")
        else
            addIssue(report, row, "needs_review", "unaccepted",
                "No applicable earlier pickup is confirmed in the available route data.", "review")
        end
    elseif row.status == "Unknown" then
        categorize(report, row, "needs_review")
    elseif info.runnable == false and info.missing then
        row.status = "Not eligible by known restrictions"
    end
    local expectedActive = row.state.active
    if info.forecast and info.forecast.active ~= nil then expectedActive = info.forecast.active end
    if action.kind == "turnin" and not info.uncertain and row.classification ~= "Skippable" and
       expectedActive == true and info.runnable == true then
        planned[id] = {active = false, completed = true}
    end
end

local function queuedPickups(steps)
    local remaining = {}
    for _, step in ipairs(steps) do
        if not step.skipped then
            for _, action in ipairs(E.list(step.actions)) do
                local id = action.kind == "accept" and E.questId(action.questId)
                if id and not action.skipped then remaining[id] = (remaining[id] or 0) + 1 end
            end
        end
    end
    return remaining
end

function E.scan(input)
    local report = {issues = {}, current = {}, seen = {}, counted = {}, checked = 0, unknown = 0,
        coverage = input.coverage, counts = {confirmed_blocker = 0, confirmed_missing_pickup = 0,
            unaccepted_objective = 0, optional_objective = 0, needs_review = 0,
            future_planned = 0, already_satisfied = 0, ready_to_activate = 0, conflicting_sources = 0}}
    local quests, stateFor = E.list(input.quests), input.stateFor
    local steps, player = E.list(input.steps), input.player or {}
    local prior, planned, intended = {}, {}, {}
    local remaining = #steps > 1 and queuedPickups(steps) or {}
    for id, earlier in pairs(E.list(input.prior)) do prior[id] = earlier end
    for _, step in ipairs(steps) do
        local atCurrent = step.current == true
        if not step.skipped then
            local applies = applicability(step, stateFor, planned, not atCurrent)
            for _, action in ipairs(E.list(step.actions)) do
                local id = E.questId(action.questId)
                if id and not action.skipped then
                    local kind = action.kind
                    if kind == "accept" and remaining[id] then remaining[id] = remaining[id] - 1 end
                    if applies ~= false then
                        local meta, state = E.list(quests[id]), stateFor(id)
                        local row = rowFor(meta, state, step, action, id, prior, atCurrent)
                        local runnable
                        if state.disabled == true then
                            runnable = false
                        else
                            runnable = matchesRestriction(meta, player, not atCurrent)
                        end
                        local forecast = planned[id]
                        local missing = state.active == false and state.completed == false
                        if not row.pickup and input.locate and missing and (kind == "complete" or kind == "turnin") then
                            row.pickup = input.locate(id)
                            row.location, row.locationConflict, row.publishedLocation, row.guideLocation =
                                pickupLocation(meta, row.pickup)
                        end
                        local active = state.active and (atCurrent or not forecast or forecast.active ~= false)
                        local currentPickup = row.pickup and row.pickup.ahead and row.pickup.current and
                            row.pickup.chapter == step.chapter and row.pickup.index == step.index
                        local info = {action = action, missing = missing, runnable = runnable, forecast = forecast,
                            uncertain = applies == nil or action.conditional == true or runnable == nil,
                            satisfied = active == true or state.completed == true,
                            projected = forecast and (forecast.completed or forecast.active),
                            currentPickup = currentPickup,
                            intended = intended[id] or (remaining[id] or 0) > 0 and not knownPickup(row)}
                        report.checked = report.checked + 1
                        if row.status == "Unknown" then report.unknown = report.unknown + 1 end
                        if atCurrent then report.current[#report.current + 1] = row end
                        local itemReady
                        if missing and (kind == "accept" or kind == "complete" or kind == "turnin") then
                            row.item = E.inspectStarter(meta, state, id, input.inventory)
                            itemReady = E.canActivate(meta, state, row.item, player, stateFor)
                        end
                        if itemReady then
                            row.status = "Ready to activate from item"
                            addIssue(report, row, "ready_to_activate", "starter-ready",
                                "The quest-starting item is in your bags. Activate it manually when you are ready.", "info")
                        elseif row.item and row.item.verified and row.item.present == nil and
                               not info.projected and not info.intended and
                               E.requirements(meta, stateFor, planned) ~= false then
                            row.verification = "Insufficient information"
                            addIssue(report, row, "needs_review", "starter-unknown",
                                "This quest starts from an item. Its presence in your bags could not be confirmed.", "review")
                        elseif kind == "accept" then
                            pickupAction(report, row, info, stateFor, planned)
                            if runnable ~= false then
                                pickup(prior, id, step, action)
                                prior[id].ahead = true
                                intended[id] = true
                            end
                        elseif kind == "complete" or kind == "turnin" then
                            objectiveAction(report, row, info, stateFor, planned)
                        elseif kind == "abandon" and not info.uncertain then
                            planned[id] = {active = false, completed = forecast and forecast.completed}
                            intended[id] = nil
                        end
                        local conflicted = row.locationConflict or row.item and row.item.disagrees
                        if not conflicted then
                            for _, conflict in ipairs(E.list(meta.conflicts)) do
                                if type(conflict) == "table" and conflict.unresolved ~= false then
                                    conflicted = true
                                    break
                                end
                            end
                        end
                        if conflicted then
                            row.verification = "Conflicting sources"
                            if not row.code and not info.satisfied and row.category ~= "future_planned" then
                                addIssue(report, row, "conflicting_sources", "conflicting-sources",
                                    "Available sources disagree. Verify this information before acting on it.", "review")
                            end
                        elseif not info.satisfied and row.category == "needs_review" then
                            row.verification = "Insufficient information"
                        end
                        if row.code == "prerequisite" or atCurrent then explainDependencies(row, quests, stateFor) end
                    end
                end
            end
        end
    end
    report.counted = nil
    return report
end

E.recordPickup = pickup
