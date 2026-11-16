local addonName, QG = ...
local E, R, QuestLog = QG.engine, QG.restedXP, QG.questLog
local Snapshot = QG.snapshot
local DATA = QG.data or {}
local route = {}
local routeNotice, routeDetails
local quests = DATA.schemaVersion == 1 and E.list(DATA.quests) or {}
local scopes = {step = "Current step", ["10"] = "Next 10 steps", ["30"] = "Next 30 steps",
    chapter = "Current chapter", next = "Next chapter", route = "Full route"}
local order = {"step", "10", "30", "chapter", "next", "route"}
local lookaheadChoices = {10, 20, 30, 50, 100}
local autoSettings = {"autoPreflight", "scanZoneChange", "scanGuideChange", "scanUpcomingSteps",
    "warningNotifications", "suppressRepeated"}
local ready, enteredWorld = false, false
local queued, queueTicket, combatAuto, combatManual
local observed, observedZone, listener, manual = {}, nil, nil, nil
local lastLiveGuide, priorCache
local announced, dismissed = {}, {}
local limit, timer = 10000, 0

QG.scopes, QG.scopeOrder, QG.lookaheadChoices = scopes, order, lookaheadChoices

local function scopeLabel(scope)
    return scopes[scope] or "Next " .. tostring(scope) .. " steps"
end

local function verifyLocalRoute(context)
    local available
    available, routeNotice, routeDetails = Snapshot.routeFor(_G, DATA, context)
    if route ~= available and manual then manual = nil end
    route = available
end

local function note(message)
    if DEFAULT_CHAT_FRAME then
        DEFAULT_CHAT_FRAME:AddMessage("|cfff3c96bRXP Quest Guard|r  " .. message:gsub("|", "||"))
    end
end
QG.note = note

local function localStep(chapter, raw, player)
    local faction = E.factionApplicability(chapter.faction, player)
    return {chapter = chapter.name, index = raw.index, actions = raw.actions, guards = raw.guards,
        conditional = raw.conditional or chapter.conditional or faction == nil, optional = raw.optional,
        source = "Local route", skipped = raw.skipped or faction == false,
        pickupUnverified = not (routeDetails and routeDetails.trusted)}
end

local function recordPast(prior, step)
    if step.skipped and not step.playerSkipped then return end
    for _, action in ipairs(E.list(step.actions)) do
        local id = E.questId(action.questId)
        if id and action.kind == "accept" and not action.skipped and not action.skipIfMissing then
            E.recordPickup(prior, id, step, action)
        end
    end
end

local function walk(prior, steps, chapter, first, lastIndex, opts, past)
    local branch = false
    for index = first, lastIndex do
        if #steps >= limit then return branch end
        local raw = chapter.steps[index]
        if type(raw) == "table" then
            local earlierBranch = opts.branch
            opts.branch = earlierBranch or branch
            opts.current = opts.live and opts.active[index]
            opts.skipped = opts.live and opts.skipIndexes[index]
            local step = R.step(chapter, raw, index, opts)
            opts.branch = earlierBranch
            branch = branch or step.jumps
            if past then recordPast(prior, step) else steps[#steps + 1] = step end
        end
    end
    return branch
end

local function appendLocal(prior, steps, chapter, firstOrdinal, past, player, lastOrdinal, preview)
    for _, raw in ipairs(E.list(chapter.steps)) do
        if #steps >= limit then return false end
        local index = E.questId(raw.index)
        if index and (past and index < firstOrdinal or not past and index >= firstOrdinal and
                      (not lastOrdinal or index <= lastOrdinal)) then
            local step = localStep(chapter, raw, player)
            if preview then step.pickupUnverified = true end
            if past then recordPast(prior, step) else steps[#steps + 1] = step end
        end
    end
    return true
end

local function appendFuture(prior, steps, context, scope, liveRoute, livePosition, localPosition, notice, opts, budget)
    local covered, nextName = 0, nil
    local aligned = true
    if localPosition then
        local available = liveRoute and (#liveRoute - livePosition) or 0
        for offset = 1, available do
            local localChapter, loaded = route[localPosition + offset], liveRoute[livePosition + offset]
            if not localChapter or localChapter.name ~= loaded.name or
               (localChapter.group or "") ~= (loaded.group or "") or
               (localChapter.subgroup or "") ~= (loaded.subgroup or "") then
                aligned = false
                break
            end
        end
        if not aligned then
            context.pathMismatch = true
            notice("The local and loaded chapter orders differ. Showing the loaded path.")
        end
    end
    local function visit(chapter, live)
        local rows = E.list(chapter.steps)
        if #rows == 0 or #steps >= limit or budget == 0 then return false end
        local count = live and #rows or E.questId(rows[#rows].index)
        if not count then return false end
        local lastIndex = budget and math.min(budget, count) or count
        if live then
            walk(prior, steps, chapter, 1, lastIndex, opts, false)
        elseif not appendLocal(prior, steps, chapter, 1, false, opts.player, lastIndex) then
            return false
        end
        if budget then budget = budget - lastIndex end
        nextName = nextName or chapter.name
        covered = covered + 1
        return true
    end
    local usePublishedFirst = aligned and localPosition and routeDetails and routeDetails.trusted
    if liveRoute and not usePublishedFirst then
        for index = livePosition + 1, #liveRoute do
            if not visit(liveRoute[index], true) then break end
        end
    end
    if aligned and localPosition and (scope ~= "next" or covered == 0) then
        local maxChapter = scope == "next" and math.min(#route, localPosition + 1) or #route
        for index = localPosition + covered + 1, maxChapter do
            if not visit(route[index], false) then break end
        end
    end
    context.nextName, context.covered = nextName, covered
    return budget
end

local function skipSignature(indexes)
    local ordered = {}
    for key, skipped in pairs(E.list(indexes)) do
        if skipped then ordered[#ordered + 1] = tostring(key) end
    end
    table.sort(ordered)
    return table.concat(ordered, ",")
end

local function earlierPickups(context, scope, opts, steps)
    local localPosition, ambiguous, match = R.matchRoute(route, context)
    local liveRoute, livePosition, liveNotice, liveCoverage
    local cache = priorCache
    local linked = #route > 0 and route or nil
    local trust = routeDetails and routeDetails.trusted
    local skipped = skipSignature(context.skipped)
    local guides = context.addon and context.addon.guides
    local compatible = cache and cache.guide == context.guide and cache.route == linked and
        cache.position == localPosition and cache.trust == trust and cache.guides == guides

    if not compatible or scope == "next" or scope == "route" then
        liveRoute, livePosition, liveNotice, liveCoverage = R.loadedRoute(context, scope ~= "route")
    else
        liveRoute, livePosition, liveNotice, liveCoverage =
            cache.nextRoute, cache.nextPosition, cache.nextNotice, cache.nextCoverage
    end
    if not compatible then
        local previous = {}
        if localPosition then
            for index = 1, localPosition - 1 do
                appendLocal(previous, steps, route[index], math.huge, true, opts.player)
            end
        end
        local earlierOpts = {api = _G, stateFor = opts.stateFor, player = opts.player}
        for index = 1, livePosition - 1 do
            local chapter = liveRoute[index]
            if type(chapter.steps) == "table" then
                walk(previous, steps, chapter, 1, #chapter.steps, earlierOpts, true)
            end
        end
        cache = {guide = context.guide, route = linked, position = localPosition, trust = trust,
            guides = guides, previous = previous}
        priorCache = cache
    end
    if scope ~= "route" then
        cache.nextRoute, cache.nextPosition, cache.nextNotice, cache.nextCoverage =
            liveRoute, livePosition, liveNotice, liveCoverage
    elseif not cache.nextRoute then
        cache.nextRoute, cache.nextPosition = {context.guide, liveRoute[livePosition + 1]}, 1
    end
    if cache.through == nil or cache.through >= context.index or cache.skipped ~= skipped then
        cache.pickups, cache.through, cache.branch = {}, 0, nil
        for id, earlier in pairs(cache.previous) do cache.pickups[id] = earlier end
    end
    opts.branch = cache.branch
    local branched = walk(cache.pickups, steps, context.guide, cache.through + 1, context.index - 1, opts, true)
    cache.through, cache.skipped, cache.branch = context.index - 1, skipped, cache.branch or branched
    opts.branch = nil
    return cache.pickups, localPosition, ambiguous, liveRoute, livePosition, liveNotice, match, liveCoverage
end

local function scanPlan(context, scope, stateFor, player, combat, liveOnly, inventory)
    if not R.authorized() then context = R.context(_G) end
    verifyLocalRoute(context)
    local selected
    if not liveOnly then selected = manual end
    local prior, steps, notices = {}, {}, {}
    local function notice(message)
        if message then notices[#notices + 1] = message end
    end
    local routeStatus = routeDetails and routeDetails.status or "unknown"
    local publishedComplete = routeDetails and routeDetails.complete == true
    local completedPlan = false
    if routeStatus == "verified" and not publishedComplete then
        notice("Published route is incomplete for level " .. tostring(routeDetails.targetLevel or 60) .. ".")
    end
    local opts = {api = _G, stateFor = stateFor, player = player, skipGuards = context.skipGuards}
    if context.guide and not selected then
        local active = {}
        for _, step in ipairs(E.list(context.active)) do
            if type(step) == "table" and step.active and not step.completed and E.questId(step.index) then
                active[step.index] = true
            end
        end
        if context.guide.steps[context.index].active ~= false then active[context.index] = true end
        opts.active, opts.skipIndexes, opts.live = active, context.skipped, true
        local current = context.guide
        local localPosition, liveRoute, livePosition, liveNotice, match, liveCoverage
        if not combat then
            local ambiguous
            prior, localPosition, ambiguous, liveRoute, livePosition, liveNotice, match, liveCoverage =
                earlierPickups(context, scope, opts, steps)
            notice(ambiguous)
            if match and match.status ~= "matched" then
                routeStatus, publishedComplete = match.status, false
            end
        elseif priorCache and priorCache.guide == current then
            prior = priorCache.pickups
        end
        local finish = tonumber(scope) and context.index + tonumber(scope) or #current.steps
        if scope == "step" or combat then finish = context.index end
        local sticky = {}
        for index in pairs(active) do
            if index < context.index then sticky[#sticky + 1] = index end
        end
        table.sort(sticky)
        for _, index in ipairs(sticky) do walk(prior, steps, current, index, index, opts, false) end
        walk(prior, steps, current, context.index, math.min(finish, #current.steps), opts, false)
        local unused = tonumber(scope) and math.max(0, finish - #current.steps)
        if not combat and (scope == "next" or scope == "route" or unused and unused > 0) then
            local futureOpts = {api = _G, stateFor = stateFor, player = player, skipGuards = context.skipGuards}
            local unspent = appendFuture(prior, steps, context, scope, liveRoute, livePosition, localPosition,
                notice, futureOpts, unused and unused > 0 and unused or nil)
            if context.pathMismatch then
                routeStatus, publishedComplete = "mismatched", false
            end
            if scope == "next" and context.covered == 0 and not (publishedComplete and localPosition == #route) then
                notice(liveNotice or "The next chapter has no loaded or local quest metadata.")
            end
            if unspent and unspent > 0 and not (publishedComplete and localPosition and
               localPosition + context.covered == #route) then
                notice("Lookahead ends after the available linked steps. " .. unspent .. " requested steps remain unscanned.")
            end
            if scope == "route" then
                completedPlan = publishedComplete and localPosition and localPosition + context.covered == #route and
                    #steps < limit or liveCoverage and liveCoverage.status == "terminal" and
                    liveCoverage.lastLevel and liveCoverage.lastLevel >= 60 and livePosition and
                    livePosition + context.covered == #liveRoute and #steps < limit
                if not completedPlan then
                    local lastStep = steps[#steps]
                    local stop = lastStep and (" Last scanned chapter " .. lastStep.chapter .. ".") or ""
                    notice("Full route coverage to level 60 could not be confirmed." .. stop)
                    if liveNotice and context.covered > 0 then notice(liveNotice) end
                end
            end
        end
        context.locate = function(id) return R.lookupPickup(context, id, opts) end
    else
        local start = selected and route[selected.chapter]
        if start then
            context.name, context.index, context.manual = start.name, selected.step, true
            local lastStep
            if combat or scope == "step" then lastStep = selected.step
            elseif tonumber(scope) then lastStep = selected.step + tonumber(scope) end
            if not combat then
                for index = 1, selected.chapter - 1 do
                    appendLocal(prior, steps, route[index], math.huge, true, player, nil, true)
                end
                appendLocal(prior, steps, start, selected.step, true, player, nil, true)
            end
            appendLocal(prior, steps, start, selected.step, false, player, lastStep)
            if not combat then
                local finish = scope == "route" and #route or scope == "next" and
                    math.min(#route, selected.chapter + 1) or selected.chapter
                for index = selected.chapter + 1, finish do appendLocal(prior, steps, route[index], 1, false, player) end
                completedPlan = scope == "route" and publishedComplete and #steps < limit
            end
        else
            notice(#route > 0 and "Select a local chapter with /qg chapters." or "Check a quest by ID with /qg check <id>.")
        end
    end
    if #steps >= limit then notice("The scan reached its 10,000-step limit.") end
    if combat and scope ~= "step" then
        notice("Preflight resumes after combat. Current objectives are still checked.")
    end
    notice(routeNotice)
    local chapters = {}
    local numberOfChapters = 0
    for _, step in ipairs(steps) do
        if step.chapter and not chapters[step.chapter] then
            numberOfChapters = numberOfChapters + 1
            chapters[step.chapter] = true
        end
    end
    local coverage = {
        scope = combat and "step" or scope, steps = #steps, chapters = numberOfChapters,
        upcomingChapters = context.covered or 0, truncated = #steps >= limit,
        route = routeStatus, publishedComplete = publishedComplete,
        routeComplete = completedPlan == true,
        combatDeferred = combat and scope ~= "step",
    }
    local report = E.scan({quests = quests, steps = steps, prior = prior, stateFor = stateFor,
        locate = context.locate, player = player, coverage = coverage, inventory = inventory})
    report.coverage = coverage
    return report, table.concat(notices, " ")
end
QG.scanPlan = scanPlan

local function isCombat()
    return type(InCombatLockdown) == "function" and InCombatLockdown()
end

local function filter(report)
    local visible, warnings = {}, 0
    for _, issue in ipairs(report.issues) do
        if not dismissed[issue.key] and not (issue.classification == "Skippable" and QG.settings.ignored[issue.id]) then
            visible[#visible + 1] = issue
            if issue.severity == "warning" or issue.severity == "blocker" then warnings = warnings + 1 end
        else
            issue.severity, issue.code, issue.message = nil, nil, nil
        end
    end
    report.issues, report.warnings = visible, warnings
end

local function alertRank(issue)
    if issue.severity == "blocker" then return 3 end
    if issue.severity == "warning" then return 2 end
    if issue.code == "starter-ready" then return 1 end
end

local function alertResolved(record, stateFor, inventory, player, combat)
    local state = stateFor(record.id)
    if state.active == true or state.completed == true or
       (record.code == "prerequisite" and E.requirements(record.meta, stateFor, {}) == true) then
        return true
    end
    if not combat and inventory and record.code == "starter-ready" then
        local observed, count = inventory(record.id, record.itemId)
        return not observed and count == 0
    end
    if not combat and inventory and record.code == "unaccepted" then
        local item = E.inspectStarter(record.meta, state, record.id, inventory)
        return E.canActivate(record.meta, state, item, player, stateFor)
    end
end

local function notify(report, stateFor, inventory, combat)
    local mode = QG.settings.notify
    if not QG.settings.warningNotifications or mode == "off" then return end
    for key, record in pairs(announced) do
        if alertResolved(record, stateFor, inventory, report.player, combat) then announced[key] = nil end
    end
    local batch, priority, first, total = {}, 0, nil, 0
    for _, issue in ipairs(report.issues) do
        local rank = alertRank(issue)
        local key = (issue.chapter or "") .. ":" .. issue.id .. ":" .. (issue.code or "")
        local seen = announced[key]
        if rank and (not QG.settings.suppressRepeated or not seen or seen.rank < rank) then
            local urgency = (rank > 1 and 10 or 0) + (issue.current and 4 or 0) + rank
            if not batch[key] then
                batch[key] = rank
                total = total + 1
            end
            if priority < urgency then first, priority = issue, urgency end
            if not seen or seen.rank < rank then
                announced[key] = {id = issue.id, code = issue.code, meta = issue.meta, rank = rank,
                    itemId = issue.item and issue.item.itemId}
            end
        end
    end
    if first then
        local suffix = first.current and "" or (" in " .. tostring(first.chapter) .. " step " .. tostring(first.index))
        local prefix = "Quest not accepted"
        if first.code == "prerequisite" then prefix = "Quest prerequisite missing" end
        if first.code == "starter-ready" then prefix = "Quest-starting item available" end
        local body = prefix .. suffix .. "\n" .. first.name .. " (" .. first.id .. ")"
        if first.code == "starter-ready" then
            local item = first.item
            local duration = E.timer(first.meta, first.state)
            body = body .. "\n" .. (item.name or ("Item " .. item.itemId)) .. "  •  Activate manually when ready"
            if duration then body = body .. "  •  " .. E.duration(duration) .. " limit" end
            if item.source then body = body .. "\n" .. item.source end
        elseif first.pickup then
            body = body .. "  •  Pickup in " .. first.pickup.chapter
        end
        if total > 1 then body = body .. "\n" .. (total - 1) .. " more in /qg" end
        if DATA.sourceUrl then body = body .. "\nForeverDB.net  •  " .. DATA.sourceUrl end
        if mode == "compact" and QG.toast then
            QG.toast(body, first.code == "starter-ready" and "info" or nil)
        else
            note((body:gsub("\n", " — ")))
        end
    end
end

function QG.refresh(scope, explicit, trigger)
    if not QG.settings then return end
    local context = R.context(_G)
    if context.guide ~= lastLiveGuide then
        lastLiveGuide, priorCache = context.guide, nil
    end
    local read, player = QuestLog.reader(_G, ready), QuestLog.player(_G)
    local combat = isCombat()
    local inventory = QuestLog.inventory(_G, ready, combat)
    scope = scope or QG.settings.scope
    if combat and scope ~= "step" and explicit then combatManual = scope end
    local report, notice = scanPlan(context, scope, read, player, combat, trigger ~= nil, inventory)
    report.requestedScope, report.scope = scope, scopeLabel(report.coverage.scope)
    report.notice, report.context, report.player, report.data = notice, context, player, DATA
    report.coverage.trigger = trigger
    filter(report)
    if trigger then QG.lastAutomaticReport = report end
    if not trigger or not manual then
        QG.report = report
        if QG.render then QG.render(report) end
    end
    if not explicit and not context.manual then notify(report, read, inventory, combat) end
end

function QG.dismiss(row)
    if row then
        dismissed[row.key] = true
        QG.refresh(QG.report and QG.report.requestedScope, true)
    end
end

function QG.ignore(row)
    if row and row.classification == "Skippable" then
        QG.settings.ignored[row.id] = true
        QG.refresh(QG.report and QG.report.requestedScope, true)
    end
end

function QG.check(id)
    id = E.questId(id)
    if not id then note("Use /qg check <quest ID>."); return end
    local read = QuestLog.reader(_G, ready)
    local state = read(id)
    local meta = quests[id] or {}
    local name = state.name or meta.name or ("Quest " .. id)
    local source = DATA.sourceUrl and ("  •  ForeverDB.net  •  " .. DATA.sourceUrl) or ""
    local status = E.status(state)
    local item
    if state.active == false and state.completed == false then
        item = E.inspectStarter(meta, state, id, QuestLog.inventory(_G, ready, isCombat()))
        if E.canActivate(meta, state, item, QuestLog.player(_G), read) then status = "Ready to activate from item" end
    end
    note(name .. " (" .. id .. "): " .. status .. source)
    if status == "Ready to activate from item" then
        local duration = E.timer(meta, state)
        note((item.name or ("Item " .. item.itemId)) .. " is in your bags. Activate manually when ready." ..
            (duration and (" Reported time limit " .. E.duration(duration) .. ".") or ""))
    elseif item then
        local present = item.present == true and "in your bags" or item.present == false and "absent from your bags" or
            "bag presence unknown"
        note((item.name or ("Item " .. item.itemId)) .. " · " .. present ..
            (item.verified and " · verified mapping" or " · quest-start mapping unverified for Forever"))
    end
    if state.active and state.remainingSeconds then
        note("Time remaining at check " .. E.duration(state.remainingSeconds) .. " · Live WoW quest API")
    end
    local context = R.context(_G)
    if context.guide and context.index > 1 then
        local last
        for index, step in ipairs(context.guide.steps) do
            if index >= context.index then break end
            for _, element in ipairs(E.list(step.elements)) do
                if type(element) == "table" and element.tag == "complete" and E.questId(element.questId) == id then
                    last = index
                end
            end
        end
        if last then note("Historical guide objective at step " .. last .. ". Current step " .. context.index .. ".") end
    end
end

local function addonVersion()
    local metadata = C_AddOns and C_AddOns.GetAddOnMetadata or GetAddOnMetadata
    if type(metadata) == "function" then
        local ok, version = pcall(metadata, addonName, "Version")
        if ok and type(version) == "string" and version ~= "" then return version end
    end
    return "unavailable from client metadata"
end

local function snapshotStatus()
    local capture = RXPQuestGuardSnapshot
    if type(capture) ~= "table" then
        return "Quest snapshot unavailable. Wait for quest data to load or leave combat."
    end
    if capture.schemaVersion ~= 1 then return "Quest snapshot unavailable. Unsupported snapshot format." end
    local active = type(capture.active) == "table" and #capture.active
    local completed = type(capture.completed) == "table" and #capture.completed
    local state = active and completed and "ready (last capture)" or "partial"
    return "Quest snapshot " .. state .. " · " .. (active or "Unknown") .. " active · " ..
        (completed or "Unknown") .. " completed"
end

local function routeStatus(context)
    if DATA.schemaVersion ~= 1 then
        return "Published route unavailable. Unsupported data format. Publish again from the desktop and use /reload."
    end
    local ok, chapters, reason, details = pcall(Snapshot.routeFor, _G, DATA, context)
    if not ok or type(chapters) ~= "table" then
        return "Published route status unavailable. The route could not be read."
    end
    if type(reason) == "string" and reason ~= "" then return reason end
    if #chapters == 0 then return "Published route unavailable. Publish a route from the desktop and use /reload." end
    if context and context.guide then
        local _, mismatch = R.matchRoute(chapters, context)
        if mismatch then return mismatch end
    end
    local status = "Published route loaded · " .. #chapters .. " chapters"
    if details and details.status == "verified" then
        local target = tostring(details.targetLevel or 60)
        status = status .. (details.complete and " · verified to level " or " · incomplete for level ") .. target
    end
    return status
end

local function printStatus()
    note("Addon version " .. addonVersion())
    local ok, context = pcall(R.context, _G)
    if ok and type(context) == "table" then
        local connection = type(context.status) == "string" and context.status:gsub("^RestedXP ", "")
        note("RestedXP " .. (connection or "connection unavailable") ..
            (type(context.version) == "string" and " · " .. context.version or ""))
        if context.guide and type(context.name) == "string" and type(context.index) == "number" then
            local stable = context.stepId and " · Stable step ID " .. context.stepId or ""
            note("Active guide " .. context.name .. " · Current step " .. context.index .. stable)
        elseif R.authorized() then
            note("Active guide and current step unavailable. Select a guide in RestedXP and check its connection.")
        end
    else
        note("RestedXP connection, active guide and current step unavailable. The guide state could not be read.")
    end
    note(snapshotStatus())
    if type(Snapshot.status) == "string" then note("Snapshot capture " .. Snapshot.status) end
    note(routeStatus(ok and context or nil))
    local automatic = QG.lastAutomaticReport
    if not R.authorized() then
        note("Automatic preflight is waiting for guide authorization. Direct quest checks are available.")
    elseif not QG.settings.autoPreflight then
        note("Automatic preflight is off. Run /qg scan for a manual check.")
    elseif automatic then
        local coverage = automatic.coverage
        note("Last automatic scan " .. automatic.scope .. " · " .. coverage.steps ..
            " steps across " .. coverage.chapters .. " chapters" ..
            (coverage.combatDeferred and " · pending until combat ends" or ""))
        if automatic.notice ~= "" then note(automatic.notice) end
    else
        note("Automatic preflight is on and waiting for game state.")
    end
end

local reasons = {"login", "guide", "zone", "step", "quest", "bag", "preference", "gossip"}
local questEvents = {
    QUEST_ACCEPTED = true, QUEST_REMOVED = true, QUEST_TURNED_IN = true,
    QUEST_LOG_UPDATE = true, UNIT_QUEST_LOG_CHANGED = true, PLAYER_LEVEL_UP = true,
    QUEST_QUERY_COMPLETE = true,
}

local function automaticScope(reason)
    local settings = QG.settings
    if not ready or not R.authorized() or not settings.autoPreflight then return end
    if reason == "guide" then return settings.scanGuideChange and "next" or nil end
    if reason == "zone" and not settings.scanZoneChange then return end
    if reason == "login" and settings.scanGuideChange then return "next" end
    if reason ~= "gossip" and settings.scanUpcomingSteps then return tostring(settings.lookahead) end
    return "step"
end

local function flushAutomatic()
    local pending = queued
    queued = nil
    for _, reason in ipairs(reasons) do
        if pending and pending[reason] then
            local scope = automaticScope(reason)
            if scope then
                if isCombat() and scope ~= "step" then
                    combatAuto = combatAuto or {}
                    for waiting in pairs(pending) do combatAuto[waiting] = true end
                end
                QG.refresh(scope, false, reason)
                return
            end
        end
    end
end

local function requestAutomatic(reason)
    if not automaticScope(reason) then return end
    queued = queued or {}
    queued[reason] = true
    if queueTicket then return end
    if not (C_Timer and type(C_Timer.After) == "function") then
        flushAutomatic()
        return
    end
    local scheduled = {}
    queueTicket = scheduled
    C_Timer.After(0.24, function()
        if queueTicket ~= scheduled then return end
        queueTicket = nil
        flushAutomatic()
    end)
end

local function observeGuide(quiet, event)
    if not ready or not R.authorized() then return end
    local context = R.context(_G)
    local reason
    if context.guide ~= observed.guide or context.status ~= observed.status or
       context.name ~= observed.name or context.group ~= observed.group then
        reason = "guide"
    elseif context.index ~= observed.index or context.stepId ~= observed.stepId then
        reason = "step"
    elseif event == "RXP_STEP_COMPLETE" then
        reason = "quest"
    end
    observed = context
    if reason and not quiet then
        Snapshot.request()
        requestAutomatic(reason)
    end
    return reason
end

local function observeZone()
    local name
    local getter = GetRealZoneText or GetZoneText
    if type(getter) == "function" then
        local ok, value = pcall(getter)
        if ok and type(value) == "string" and value ~= "" then name = value end
    end
    if C_Map and type(C_Map.GetBestMapForUnit) == "function" then
        local ok, map = pcall(C_Map.GetBestMapForUnit, "player")
        if ok and type(map) == "number" then name = tostring(map) .. ":" .. (name or "") end
    end
    local changed = not name or name ~= observedZone
    observedZone = name
    return changed
end

function QG.autoSettingsChanged()
    if not QG.settings.autoPreflight then
        queued, queueTicket, combatAuto = nil, nil, nil
    else
        requestAutomatic("preference")
    end
end

local function listen()
    if not R.authorized() or listener or not LibStub then return end
    local ok, lib = pcall(function() return LibStub("AceEvent-3.0", true) end)
    if not ok or not lib or type(lib.RegisterMessage) ~= "function" then return end
    listener = {}
    local function guideChanged(event)
        observeGuide(false, event)
    end
    for _, name in ipairs({"RXP_STEP_ACTIVATED", "RXP_STEP_DEACTIVATED", "RXP_GUIDE_LOADED", "RXP_STEP_COMPLETE"}) do
        pcall(lib.RegisterMessage, listener, name, guideChanged)
    end
end

function QG.guideAccessHelp()
    note("Use /qg guides allow only for self-authored guides or guides you are licensed to analyze automatically.")
    note("Run /qg guides off before loading a paid or restricted guide.")
end

local function setGuideAccess(allowed)
    if QG.settings.authorizedGuides ~= allowed then
        QG.settings.authorizedGuides = allowed
        observed, manual, lastLiveGuide, priorCache = {}, nil, nil, nil
        queued, queueTicket, combatAuto, combatManual = nil, nil, nil, nil
        announced, dismissed, QG.lastAutomaticReport = {}, {}, nil
        Snapshot.request()
        if allowed then
            listen()
            observeGuide(true)
            requestAutomatic("login")
        end
        QG.refresh("step", true)
    end
    note(allowed and "Authorized guide analysis is on. Turn it off before loading a paid or restricted guide." or
        "Guide analysis is off. Live quest checks and capture are still available.")
end
QG.setGuideAccess = setGuideAccess

local function slash(message)
    local command, arg, extra = message:match("^%s*(%S*)%s*(%S*)%s*(%S*)")
    command = command:lower()
    if command == "check" then
        QG.check(arg)
    elseif command == "scan" or command == "next" then
        QG.refresh(command == "next" and "next" or scopes[arg] and arg or QG.settings.scope, true)
        QG.open("report")
    elseif command == "status" then
        printStatus()
    elseif command == "debug" then
        QG.refresh(nil, true)
        QG.open("debug")
    elseif command == "settings" then
        QG.open("settings")
    elseif command == "guides" then
        if arg == "off" then
            setGuideAccess(false)
        elseif arg == "allow" and extra == "" then
            setGuideAccess(true)
        else
            note(R.authorized() and "Authorized guide analysis is on." or "Guide analysis is off.")
            QG.guideAccessHelp()
        end
    elseif command == "live" then
        manual = nil
        QG.refresh(nil, true)
        QG.open("report")
    elseif command == "chapters" then
        verifyLocalRoute()
        if #route == 0 then note(routeNotice or "No local chapters are loaded.") end
        for index, chapter in ipairs(route) do note(index .. ". " .. chapter.name) end
        if #route > 0 then note("Use /qg chapter <number> [starting step]. Use /qg live to resume live view.") end
    elseif command == "chapter" then
        verifyLocalRoute()
        local index = E.questId(arg)
        if index and route[index] then
            manual = {chapter = index, step = E.questId(extra) or 1}
            QG.refresh(nil, true)
            QG.open("report")
        else
            note(routeNotice or "Use /qg chapters for available chapter numbers.")
        end
    elseif command == "" then
        QG.toggle()
    else
        note("/qg, /qg scan [step|10|30|chapter|next|route], /qg next, /qg status, /qg debug, /qg check <id>, /qg guides, /qg chapters, /qg settings")
    end
end

local function loadSettings()
    if type(RXPQuestGuardSettings) ~= "table" then RXPQuestGuardSettings = {} end
    local settings = RXPQuestGuardSettings
    if not scopes[settings.scope] then settings.scope = "30" end
    if settings.notify == "off" and settings.warningNotifications == nil then settings.warningNotifications = false end
    if settings.notify ~= "chat" then settings.notify = "compact" end
    for _, name in ipairs(autoSettings) do
        if type(settings[name]) ~= "boolean" then settings[name] = true end
    end
    settings.authorizedGuides = settings.authorizedGuides == true
    local lookahead = tonumber(settings.lookahead) or tonumber(settings.scope) or 30
    if lookahead < 1 or lookahead > 100 or lookahead ~= math.floor(lookahead) then lookahead = 30 end
    settings.lookahead = lookahead
    if settings.minimap == nil then settings.minimap = true end
    if type(settings.ignored) ~= "table" then settings.ignored = {} end
    QG.settings = settings
end

local frame = CreateFrame("Frame")
for _, event in ipairs({"ADDON_LOADED", "PLAYER_ENTERING_WORLD", "PLAYER_LEAVING_WORLD", "PLAYER_LOGOUT",
    "QUEST_LOG_UPDATE", "QUEST_ACCEPTED", "QUEST_QUERY_COMPLETE",
    "QUEST_REMOVED", "QUEST_TURNED_IN", "UNIT_QUEST_LOG_CHANGED", "PLAYER_LEVEL_UP", "PLAYER_REGEN_ENABLED",
    "ZONE_CHANGED_NEW_AREA", "BAG_UPDATE_DELAYED", "ITEM_DATA_LOAD_RESULT", "GET_ITEM_INFO_RECEIVED",
    "GOSSIP_SHOW", "GOSSIP_CLOSED"}) do
    pcall(frame.RegisterEvent, frame, event)
end
frame:SetScript("OnEvent", function(_, event, arg, arg2)
    Snapshot.event(event, arg, arg2)
    if event == "ADDON_LOADED" then
        if arg == addonName then
            loadSettings()
            SLASH_RXPQUESTGUARD1 = "/qg"
            SlashCmdList.RXPQUESTGUARD = slash
            if QG.setupWindow then QG.setupWindow() end
        elseif arg ~= "RXPGuides" then
            return
        end
        listen()
        observeGuide(false)
    elseif event == "PLAYER_ENTERING_WORLD" then
        ready = true
        QuestLog.invalidateInventory()
        listen()
        local reason = observeGuide(true)
        local newZone = observeZone()
        if not enteredWorld or arg == true or arg2 == true then
            enteredWorld = true
            requestAutomatic("login")
        elseif reason == "guide" then
            requestAutomatic(reason)
        elseif newZone then
            requestAutomatic("zone")
        end
    elseif event == "PLAYER_LEAVING_WORLD" or event == "PLAYER_LOGOUT" then
        ready = false
        queued, queueTicket, combatAuto, combatManual = nil, nil, nil, nil
    elseif event == "PLAYER_REGEN_ENABLED" then
        local automatic, manualScope = combatAuto, combatManual
        combatAuto, combatManual = nil, nil
        if automatic then
            queued = queued or {}
            for reason in pairs(automatic) do queued[reason] = true end
            queueTicket = nil
            flushAutomatic()
        end
        if manualScope then QG.refresh(manualScope, true) end
    elseif event == "ZONE_CHANGED_NEW_AREA" then
        if observeZone() then requestAutomatic("zone") end
    elseif event == "BAG_UPDATE_DELAYED" then
        QuestLog.invalidateInventory()
        requestAutomatic("bag")
    elseif event == "ITEM_DATA_LOAD_RESULT" or event == "GET_ITEM_INFO_RECEIVED" then
        local itemId = E.questId(arg)
        if itemId and arg2 ~= false and QuestLog.invalidateInventory(itemId) then requestAutomatic("bag") end
    elseif questEvents[event] and (event ~= "UNIT_QUEST_LOG_CHANGED" or arg == "player") then
        if event == "QUEST_ACCEPTED" or event == "QUEST_REMOVED" or event == "QUEST_TURNED_IN" then
            QuestLog.invalidateInventory()
            local id = (event == "QUEST_ACCEPTED" and E.questId(arg2)) or E.questId(arg)
            if id then
                for key, issue in pairs(announced) do
                    if issue.id == id then announced[key] = nil end
                end
            end
        end
        requestAutomatic("quest")
    elseif event == "GOSSIP_SHOW" or event == "GOSSIP_CLOSED" then
        requestAutomatic("gossip")
    end
end)
frame:SetScript("OnUpdate", function(_, elapsed)
    if not ready or not R.authorized() then return end
    timer = timer + elapsed
    if timer < 0.45 then return end
    timer = 0
    observeGuide(false)
end)
