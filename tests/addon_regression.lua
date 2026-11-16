-- Run from the repository root with Lua 5.1 or a Lua 5.1-compatible runner.
local root = "addon/RXPQuestGuard/"
local addon = {settings = {authorizedGuides = true}}
local function loadAddon(file, target)
    assert(loadfile(root .. file .. ".lua"))("RXPQuestGuard", target or addon)
end
for _, name in ipairs({"Data", "Engine", "QuestLog", "RestedXP", "Snapshot"}) do loadAddon(name) end

local E, Q, R = addon.engine, addon.questLog, addon.restedXP
local active, completed = {[95261] = true, [2981] = true}, {[95260] = true}
local readyIds = {[2981] = true}
local states = function(id)
    return {active = active[id] == true, completed = completed[id] == true, ready = readyIds[id] == true}
end
local function action(kind, questId)
    return {kind = kind, questId = questId}
end
local function scan(steps, quests, prior, stateFor)
    return E.scan({steps = steps, quests = quests, prior = prior, stateFor = stateFor or states})
end
local function step(chapter, index, current, ...)
    return {chapter = chapter, index = index, current = current, actions = {...}, source = "Fixture"}
end
local function regressions()
    local prior = {[95882] = {chapter = "40-42 Marsh I", index = 9,
        location = {name = "WANTED! sign"}}}
    local metadata = {[95882] = {name = "WANTED: Scoutmaster Vargas"}}
    local result = scan({step("43-44 Marsh II", 42, true, action("complete", 95882))}, metadata, prior)
    assert(#result.issues == 1 and result.issues[1].severity == "warning")
    assert(result.issues[1].name == "WANTED: Scoutmaster Vargas")
    assert(result.issues[1].pickup.chapter == "40-42 Marsh I")
    assert(result.issues[1].location.name == "WANTED! sign" and result.issues[1].classification == "Unknown")

    result = scan({step("43-44 Marsh II", 42, false, action("complete", 95882))}, metadata, prior)
    assert(#result.issues == 1 and result.issues[1].severity == "warning" and not result.issues[1].current)
    assert(result.issues[1].index == 42)

    result = scan({step("Marsh", 1, true, action("complete", 95261))}, {})
    assert(#result.issues == 0 and result.current[1].status == "Active")
    result = scan({step("Pine Reach", 1, true, action("turnin", 2981))}, {})
    assert(#result.issues == 0 and result.current[1].status == "Ready to turn in")

    local requires = {[95261] = {requirements = {{questId = 95260}}},
        [95267] = {requirements = {{questId = 95261}}}}
    assert(E.requirements(requires[95261], states, {}) == true)
    assert(E.requirements(requires[95267], states, {}) == false)
    result = scan({step("Marsh", 1, true, action("accept", 95267))}, requires)
    assert(#result.issues == 1 and result.issues[1].code == "prerequisite")
    result = scan({step("Marsh", 1, true, action("turnin", 95261)),
        step("Marsh", 2, false, action("accept", 95267)),
        step("Marsh", 3, false, action("complete", 95267))}, requires)
    assert(#result.issues == 0)
    local alternatives = {requirementGroups = {{{questId = 1}}, {{questId = 95260}, {questId = 95261, state = "active"}}}}
    assert(E.requirements(alternatives, states, {}) == true)

    result = scan({step("Marsh", 39, true, action("complete", 95882))},
        {[95882] = {classification = "optional"}}, prior)
    assert(#result.issues == 1 and result.issues[1].severity == "info" and result.issues[1].classification == "Skippable")
    result = scan({step("Before", 1, true), step("Future", 1, false, action("accept", 2)),
        step("Future", 2, false, action("complete", 2))}, {})
    assert(#result.issues == 0)

    local guarded = step("Marsh", 42, true, action("complete", 95882))
    guarded.guards = {{kind = "active", questIds = {95882}}}
    assert(#scan({guarded}, metadata, prior).issues == 0)
    guarded.current = false
    assert(#scan({guarded}, metadata, prior).issues == 0)
    guarded.guards[1].questIds = {1, 2}
    assert(scan({guarded}, metadata, prior).issues[1].severity == "review")
    local skippable = step("Marsh", 42, true, action("complete", 95882))
    skippable.actions[1].skipIfMissing = true
    assert(#scan({skippable}, metadata, prior).issues == 0)
    result = scan({step("Marsh", 42, true, action("complete", 95882))}, metadata, prior, function() return {} end)
    assert(#result.issues == 0 and result.unknown == 1)
end

local function questApi()
    local mock = {C_QuestLog = {
        IsOnQuest = function() return false end,
        IsQuestFlaggedCompleted = function() error("Unsupported") end,
        GetAllCompletedQuestIDs = function() return {95260} end,
    }, C_GossipInfo = {GetAvailableQuests = function() return {{questID = 95882}} end}}
    local read = Q.reader(mock, true)
    assert(E.status(read(95260)) == "Completed")
    assert(E.status(read(95882)) == "Available to accept")
    assert(E.status(Q.reader(mock, false)(95260)) == "Unknown")
    local legacy = {
        GetNumQuestLogEntries = function() return 2 end,
        GetQuestLogTitle = function(index)
            if index == 1 then return "Collapsed zone", 0, 0, true, true end
            return "It Takes Guts", 45, 0, false, false, nil, 0, 95261
        end,
        GetQuestsCompleted = function(into) into[95260] = true end,
    }
    read = Q.reader(legacy, true)
    assert(E.status(read(95261)) == "Active")
    assert(E.status(read(95882)) == "Unknown")
    assert(E.status(read(95260)) == "Completed")
end

local function rawStep(index, ...)
    return {index = index, elements = {...}}
end
local function guide(name, nextName, steps)
    return {name = name, next = nextName, group = "Example Leveling Route (H)",
        subgroup = "Example Levels 40-50", key = "Example|Leveling|" .. name, steps = steps}
end
local first = guide("40-42 Marsh I", "42-42 North Pass", {})
local second = guide("42-42 North Pass", "42-43 Mica Flats", {})
local third = guide("42-43 Mica Flats", "43-44 Marsh II", {})
local final = guide("43-44 Marsh II", "44-45 Pine Reach", {})
local future = guide("44-45 Pine Reach", nil, nil)
for i = 1, 9 do first.steps[i] = rawStep(i) end
for i = 1, 62 do final.steps[i] = rawStep(i) end
local objective = {tag = "complete", questId = 95882, obj = 1, title = "WANTED: Scoutmaster Vargas"}
first.steps[9] = rawStep(9, {tag = "accept", questId = 95882})
final.steps[41] = rawStep(41, {tag = "goto"})
final.steps[42] = rawStep(42, objective)
final.steps[46] = rawStep(46, {tag = "turnin", questId = 95882})
final.steps[62].stepId = 7100062

local function bridge()
    local live = {currentGuide = final, guides = {first, second, third, final, future},
        release = "v4.11.36", RXPFrame = {activeSteps = {final.steps[41]}}}
    local lib = {GetAddon = function(_, name) assert(name == "RXPGuides"); return live end}
    local api = {LibStub = function() return lib end,
        RXPCData = {currentGuideName = final.name, currentGuideGroup = final.group, currentStep = 41}}
    local context = R.context(api)
    assert(context.status == "Connected" and context.guide == final and context.index == 41)
    local route, index = R.loadedRoute(context)
    assert(#route == 5 and index == 4 and route[1] == first and route[5] == future)
    local converted = R.step(final, final.steps[42], 42, {api = api, player = {}, stateFor = states})
    assert(converted.actions[1].questId == 95882 and converted.actions[1].objectiveIndex == 1)
    assert(#converted.guards == 0 and objective.skip == nil and objective.completed == nil)
    final.next = "44-45 Pine Reach;44-45 Alternate"
    local ambiguous, _, notice = R.loadedRoute(context)
    assert(#ambiguous == 4 and notice:find("alternative"))
    final.next = "44-45 Pine Reach"
    local unavailable = R.context({GetAddOnMetadata = function() return "future" end})
    assert(unavailable.status:find("incompatible") and unavailable.guide == nil)
    return live, api
end

local function snapshotLifecycle(live, bindings)
    local Snapshot = addon.snapshot
    local timers, clock, reads, combat = {}, 1764000000, 0, false
    local logIds, logTotal, collapsed = {2981, 95261}, 2, false
    local simulatedActive = {[2981] = true, [95261] = true}
    local completedIds = {95260}
    local readinessAvailable = true
    local surname, realm = "Meadow", "Example Shard"
    local chapter = live.currentGuide
    chapter.steps[41].stepId = 7100041
    bindings.RXPCData.stepSkip = {[5] = true, ["3"] = true, [6] = false, wrong = true}
    bindings.RXPCData.currentStepId = 99
    LibStub, RXPCData = bindings.LibStub, bindings.RXPCData
    UnitName = function() return "Ellie", surname end
    GetRealmName = function() return realm end
    GetRealmID = function() return 9701 end
    GetNormalizedRealmName = function() return "ExampleShard" end
    UnitLevel = function() return 43 end
    UnitFactionGroup = function() return "Horde" end
    UnitClass = function() return "Hunter", "HUNTER" end
    UnitRace = function() return "Orc", "Orc" end
    GetBuildInfo = function() return "1.60.1", "70938", "", 16001 end
    GetAddOnMetadata = function(name, field)
        if name == "RXPQuestGuard" and field == "Version" then return "0.2.0" end
    end
    InCombatLockdown = function() return combat end
    GetServerTime = function() return clock end
    C_Timer = {After = function(_, callback) timers[#timers + 1] = callback end}
    C_QuestLog = {
        GetNumQuestLogEntries = function() reads = reads + 1; return #logIds + 1, logTotal end,
        GetInfo = function(index)
            if index == 1 then return {isHeader = true, isCollapsed = collapsed} end
            return {questID = logIds[index - 1]}
        end,
        GetAllCompletedQuestIDs = function() return completedIds end,
        IsOnQuest = function(id) return simulatedActive[id] == true end,
        ReadyForTurnIn = function(id) if readinessAvailable then return id == 2981 end end,
    }
    local function tick()
        local run = timers
        timers = {}
        for _, callback in ipairs(run) do callback() end
    end

    local originalIds, originalCompleted, originalTotal = logIds, completedIds, logTotal
    logIds, completedIds, logTotal = {}, {}, 0
    RXPQuestGuardSnapshot = nil
    Snapshot.event("PLAYER_ENTERING_WORLD")
    tick()
    assert(RXPQuestGuardSnapshot.active == nil and RXPQuestGuardSnapshot.completed == nil)
    Snapshot.event("QUEST_LOG_UPDATE")
    tick()
    assert(#RXPQuestGuardSnapshot.active == 0 and #RXPQuestGuardSnapshot.completed == 0)
    Snapshot.event("PLAYER_LOGOUT")
    logIds, completedIds, logTotal, reads = originalIds, originalCompleted, originalTotal, 0

    RXPQuestGuardSnapshot = nil
    Snapshot.event("QUEST_LOG_UPDATE")
    assert(#timers == 0)
    Snapshot.event("PLAYER_ENTERING_WORLD")
    Snapshot.event("QUEST_LOG_UPDATE")
    Snapshot.event("UNIT_QUEST_LOG_CHANGED", "player")
    assert(#timers == 1 and reads == 0 and RXPQuestGuardSnapshot == nil)
    tick()
    local initial = RXPQuestGuardSnapshot
    assert(initial.character == "Ellie Meadow" and initial.realm == "Example Shard" and initial.realmId == 9701)
    assert(initial.schemaVersion == 1 and initial.capturedAt == clock and initial.clientBuild == "70938")
    assert(initial.clientInterface == 16001 and initial.class == "HUNTER" and initial.race == "Orc")
    assert(initial.addonVersion == "0.2.0" and initial.restedXPVersion == "v4.11.36")
    assert(initial.currentGuide == chapter.name and initial.currentGuideGroup == chapter.group)
    assert(initial.currentGuideSubgroup == chapter.subgroup and initial.currentGuideKey == chapter.key)
    assert(initial.currentStep == 41 and initial.currentStepId == 7100041)
    assert(#initial.skippedSteps == 2 and initial.skippedSteps[1] == 3 and initial.skippedSteps[2] == 5)
    assert(initial.active[1] == 2981 and initial.active[2] == 95261 and initial.completed[1] == 95260)
    assert(#initial.ready == 1 and initial.ready[1] == 2981)

    collapsed, logIds = true, {2981}
    clock = clock + 2
    Snapshot.event("QUEST_LOG_UPDATE")
    tick()
    assert(RXPQuestGuardSnapshot.capturedAt == clock and RXPQuestGuardSnapshot.active[2] == 95261)

    logTotal, simulatedActive[95882] = 3, true
    clock = clock + 2
    combat = true
    Snapshot.event("QUEST_ACCEPTED", 95882)
    Snapshot.event("QUEST_LOG_UPDATE")
    assert(#timers == 0 and RXPQuestGuardSnapshot.capturedAt ~= clock)
    combat = false
    Snapshot.event("PLAYER_REGEN_ENABLED")
    tick()
    assert(RXPQuestGuardSnapshot.capturedAt == clock and RXPQuestGuardSnapshot.active[3] == 95882)

    local lastHealthy = RXPQuestGuardSnapshot
    clock, logTotal = clock + 2, 4
    Snapshot.event("QUEST_LOG_UPDATE")
    tick()
    assert(RXPQuestGuardSnapshot == lastHealthy and Snapshot.status:find("previous capture"))
    logTotal, completedIds = 3, {}
    Snapshot.event("QUEST_LOG_UPDATE")
    tick()
    assert(RXPQuestGuardSnapshot == lastHealthy and Snapshot.status:find("Quest history"))

    completedIds = {95260, 95882}
    logTotal, readinessAvailable = 3, false
    live.currentGuide = nil
    Snapshot.request()
    assert(#timers == 1)
    Snapshot.event("PLAYER_LOGOUT")
    assert(RXPQuestGuardSnapshot.capturedAt == clock and RXPQuestGuardSnapshot.completed[2] == 95882)
    assert(RXPQuestGuardSnapshot.ready == nil and RXPQuestGuardSnapshot.currentGuide == nil)
    tick()
    assert(RXPQuestGuardSnapshot.capturedAt == clock)

    local data = {schemaVersion = 1, route = {{name = "Test"}},
        routeContext = {character = "ellie-meadow", realm = "ExampleShard", faction = "Horde",
            class = "HUNTER", clientInterface = 16001}}
    assert(Snapshot.routeFor(_G, data) == data.route)
    data.routeContext.realm = "9701"
    assert(Snapshot.routeFor(_G, data) == data.route)
    data.routeContext.realm = "91"
    local route, reason = Snapshot.routeFor(_G, data)
    assert(#route == 0 and reason:find("realm"))
    data.routeContext.realm, data.routeContext.clientInterface = "Example Shard", 99999
    route, reason = Snapshot.routeFor(_G, data)
    assert(#route == 0 and reason:find("client"))
    data.routeContext.clientInterface, surname = 16001, "Creek"
    route, reason = Snapshot.routeFor(_G, data)
    assert(#route == 0 and reason:find("character"))
    data.routeContext = nil
    assert(Snapshot.routeFor(_G, data) == data.route)

    RXPQuestGuardSnapshot, C_Timer = nil, nil
    LibStub, RXPCData = nil, nil
end

local function mockWidgets()
    local frames = {}
    local methods = {}
    function methods:CreateTexture() return setmetatable({}, {__index = methods}) end
    function methods:CreateFontString() return setmetatable({}, {__index = methods}) end
    function methods:GetStringHeight() return 20 end
    function methods:GetFrameLevel() return 5 end
    function methods:SetScript(event, callback) self.scripts[event] = callback end
    function methods:RegisterEvent(event) self.events[event] = true end
    function methods:SetText(text) self.textValue = text end
    function methods:SetSize(width, height) self.width, self.height = width, height end
    function methods:SetPoint(...) self.anchor = {...} end
    function methods:GetParent() return self.parent end
    function methods:SetParent(parent) self.parent = parent end
    function methods:Show() self.visible = true end
    function methods:Hide() self.visible = false end
    function methods:IsShown() return self.visible == true end
    function methods:SetShown(shown) self.visible = not not shown end
    setmetatable(methods, {__index = function() return function() end end})
    CreateFrame = function(kind, name, parent)
        local widget = setmetatable({scripts = {}, events = {}, kind = kind, name = name, parent = parent}, {__index = methods})
        frames[#frames + 1] = widget
        if name then frames[name] = widget end
        return widget
    end
    return frames
end

local function runtime(live, bindings)
    local messages = {}
    local frames = mockWidgets()
    UIParent, Minimap, UISpecialFrames, SlashCmdList = {}, CreateFrame("Frame"), {}, {}
    DEFAULT_CHAT_FRAME = {AddMessage = function(_, text) messages[#messages + 1] = text end}
    C_QuestLog = {
        IsOnQuest = function(id) return active[id] == true end,
        IsQuestFlaggedCompleted = function(id) return completed[id] == true end,
    }
    LibStub, RXPCData = bindings.LibStub, bindings.RXPCData
    GetAddOnMetadata = function(name, field)
        if field == "Version" then return name == "RXPQuestGuard" and "0.3.0" or "v4.11.36" end
    end
    GetBuildInfo = function() return "1.60.1", "70938", "", 16001 end
    UnitName, GetRealmName = function() return "Fixture", "Hunter" end, function() return "Example Realm" end
    local deferred = false
    InCombatLockdown = function() return deferred end
    local function indexed(name, steps)
        return {name = name, group = final.group, subgroup = final.subgroup, steps = steps}
    end
    addon.data = {schemaVersion = 1, source = "Authorized test data", quests = {[95882] = {name = objective.title}},
        routeContext = {character = "Fixture Hunter", realm = "Example Realm", clientInterface = 16001},
        routeInfo = {targetLevel = 60, guideRelease = "v4.11.36", clientVersion = "1.60.1.70938", complete = false},
        route = {
            indexed(first.name, {{index = 9, actions = {{questId = 95882, kind = "accept",
                location = {name = "WANTED! sign", worldMapId = 7001, worldFloor = 0,
                    worldX = -4800, worldY = -7900}}}}}),
            indexed(second.name, {}), indexed(third.name, {}),
            indexed(final.name, {{index = 42, actions = {{questId = 95882, kind = "complete"}}}}),
            indexed(future.name, {{index = 5, actions = {{questId = 2981, kind = "complete"}}}}),
        }}
    RXPQuestGuardSettings = {authorizedGuides = true}
    loadAddon("Core")
    local eventFrame = frames[#frames]
    loadAddon("Window")
    eventFrame.scripts.OnEvent(eventFrame, "ADDON_LOADED", "RXPQuestGuard")
    eventFrame.scripts.OnEvent(eventFrame, "PLAYER_ENTERING_WORLD")
    assert(addon.report.warnings == 2 and not addon.report.issues[1].current)
    assert(addon.report.issues[1].pickup.chapter == first.name)
    local slash = SlashCmdList.RXPQUESTGUARD
    assert(SLASH_RXPQUESTGUARD1 == "/qg" and type(slash) == "function")
    local button = frames.RXPQuestGuardMinimapButton
    assert(button and button.kind == "Button" and button:GetParent() == Minimap)
    assert(button.width == 30 and button.height == 30 and button.anchor[2] == Minimap)
    assert(button:IsShown() and not frames.RXPQuestGuardWindow:IsShown())
    button.scripts.OnClick(button, "LeftButton")
    assert(frames.RXPQuestGuardWindow:IsShown())
    button.scripts.OnClick(button, "LeftButton")
    assert(not frames.RXPQuestGuardWindow:IsShown())

    -- A collector can move the button and retain control of the anchor across visibility changes.
    local collector = CreateFrame("Frame")
    button:SetParent(collector)
    button:SetPoint("CENTER", collector)
    local count = #frames
    addon.setupWindow()
    assert(#frames == count and frames.RXPQuestGuardMinimapButton == button)
    slash("settings")
    local visibility
    for _, widget in ipairs(frames) do
        if type(widget.textValue) == "string" and widget.textValue:find("^Minimap button") then
            visibility = widget
            break
        end
    end
    assert(visibility and visibility.scripts.OnClick)
    visibility.scripts.OnClick(visibility)
    assert(not button:IsShown() and button:GetParent() == collector and button.anchor[2] == collector)
    visibility.scripts.OnClick(visibility)
    assert(button:IsShown() and button:GetParent() == collector and button.anchor[2] == collector)
    frames.RXPQuestGuardWindow:Hide()

    local start = #messages + 1
    slash("  StAtUs  ")
    local output = table.concat(messages, "\n", start)
    assert(output:find("Addon version 0.3.0", 1, true))
    assert(output:find("RestedXP Connected", 1, true) and output:find(final.name, 1, true))
    assert(output:find("Current step 41", 1, true) and output:find("Quest snapshot unavailable", 1, true))
    assert(output:find("Published route loaded", 1, true))
    assert(not frames.RXPQuestGuardWindow:IsShown())

    slash("next")
    assert(frames.RXPQuestGuardWindow:IsShown())
    assert(addon.report.context.nextName == "44-45 Pine Reach" and addon.report.context.covered == 1)
    slash("debug")
    slash("settings")
    slash("check 95261")
    assert(messages[#messages]:find("95261.*Active"))

    live.guides = {}  -- User-local metadata supplies the earlier chapter while RestedXP only has the current guide.
    addon.refresh("30", true)
    assert(addon.report.issues[1].pickup.location.worldX == -4800)
    assert(addon.report.issues[1].index == 42)

    deferred = true
    addon.refresh("30", true)
    assert(addon.report.warnings == 0 and addon.report.notice:find("combat"))
    deferred = false
    eventFrame.scripts.OnEvent(eventFrame, "PLAYER_REGEN_ENABLED")
    assert(addon.report.warnings == 2)

    RXPCData.currentStep = 42
    final.steps[42].active = true
    eventFrame.scripts.OnUpdate(eventFrame, 0.5)
    assert(addon.report.issues[1].current and addon.report.issues[1].pickup.chapter == first.name)

    active[95882] = true
    eventFrame.scripts.OnEvent(eventFrame, "QUEST_LOG_UPDATE")
    assert(addon.report.warnings == 0)
    active[95882] = nil
    live.currentGuide = nil
    slash("chapter 4 41")
    assert(addon.report.context.manual and addon.report.issues[1].pickup.chapter == first.name)

    live.currentGuide = final
    addon.data.routeContext = {character = "Other Character", realm = "Other Realm", clientInterface = 16001}
    slash("live")
    assert(addon.report.warnings == 1 and addon.report.issues[1].current)
    assert(addon.report.issues[1].pickup == nil and addon.report.notice:find("Published route"))
    start = #messages + 1
    slash("status")
    assert(table.concat(messages, "\n", start):find("Published route belongs to a different character", 1, true))
    eventFrame.scripts.OnEvent(eventFrame, "PLAYER_LOGOUT")
end

local function reloadStatus(live, bindings)
    local saved = {schemaVersion = 1, active = {}, completed = {}, capturedAt = 1000}
    for i = 1, 23 do saved.active[i] = 10000 + i end
    for i = 1, 507 do saved.completed[i] = 20000 + i end
    local settings = {minimap = false, scope = "10", notify = "off", ignored = {[95882] = true},
        authorizedGuides = true}
    local function reload()
        local messages, fresh = {}, {}
        local frames = mockWidgets()
        UIParent, Minimap, UISpecialFrames, SlashCmdList = {}, CreateFrame("Frame"), {}, {}
        DEFAULT_CHAT_FRAME = {AddMessage = function(_, text) messages[#messages + 1] = text end}
        RXPQuestGuardSettings, RXPQuestGuardSnapshot = settings, saved
        for _, name in ipairs({"Data", "Engine", "QuestLog", "RestedXP", "Snapshot", "Core"}) do loadAddon(name, fresh) end
        local eventFrame = frames[#frames]
        loadAddon("Window", fresh)
        eventFrame.scripts.OnEvent(eventFrame, "ADDON_LOADED", "RXPQuestGuard")
        assert(fresh.settings == settings and not settings.minimap and settings.ignored[95882] and
            settings.authorizedGuides)
        assert(frames.RXPQuestGuardMinimapButton and not frames.RXPQuestGuardMinimapButton:IsShown())
        assert(RXPQuestGuardSnapshot == saved and type(SlashCmdList.RXPQUESTGUARD) == "function")
        return messages, frames, eventFrame
    end
    LibStub, RXPCData = bindings.LibStub, bindings.RXPCData
    RXPCData.currentStep = 62
    local _, first, logoutFrame = reload()
    logoutFrame.scripts.OnEvent(logoutFrame, "PLAYER_LOGOUT")
    local messages, second = reload()
    assert(first.RXPQuestGuardMinimapButton ~= second.RXPQuestGuardMinimapButton)
    local slash = SlashCmdList.RXPQUESTGUARD
    slash("status")
    local output = table.concat(messages, "\n")
    assert(output:find("Current step 62", 1, true) and output:find("Stable step ID 7100062", 1, true))
    assert(output:find("Quest snapshot ready", 1, true) and output:find("23 active", 1, true))
    assert(output:find("507 completed", 1, true) and output:find("Publish a route from the desktop", 1, true))
    slash("")
    assert(second.RXPQuestGuardWindow:IsShown()) -- Slash access still works when the icon is hidden.

    live.currentGuide = nil
    RXPQuestGuardSnapshot = {schemaVersion = 1, active = {95261}}
    local start = #messages + 1
    slash("status")
    output = table.concat(messages, "\n", start)
    assert(output:find("Active guide and current step unavailable", 1, true))
    assert(output:find("Quest snapshot partial", 1, true) and output:find("Unknown completed", 1, true))
end

local function automaticProtection(live, bindings)
    local addon, messages, timers, callbacks, scans = {}, {}, {}, {}, {}
    local frames = mockWidgets()
    UIParent, Minimap, UISpecialFrames, SlashCmdList = {}, CreateFrame("Frame"), {}, {}
    DEFAULT_CHAT_FRAME = {AddMessage = function(_, text) messages[#messages + 1] = text end}
    C_Timer = {After = function(_, callback) timers[#timers + 1] = callback end}
    local zone, combat = "Marsh", false
    GetRealZoneText, GetZoneText = function() return zone end, function() return zone end
    InCombatLockdown = function() return combat end
    C_QuestLog = {
        IsOnQuest = function(id) return active[id] == true end,
        IsQuestFlaggedCompleted = function(id) return completed[id] == true end,
    }
    LibStub = function(name, silent)
        if name == "AceEvent-3.0" then
            return {RegisterMessage = function(_, event, callback) callbacks[event] = callback end}
        end
        return bindings.LibStub(name, silent)
    end
    RXPCData = bindings.RXPCData
    local previous = final.steps[43]
    final.steps[43] = rawStep(43, {tag = "complete", questId = 95261, title = "It Takes Guts"})
    RXPQuestGuardSettings, RXPQuestGuardSnapshot = {notify = "chat", authorizedGuides = true}, nil
    for _, name in ipairs({"Data", "Engine", "QuestLog", "RestedXP", "Snapshot", "Core"}) do loadAddon(name, addon) end
    local eventFrame = frames[#frames]
    local refresh = addon.refresh
    addon.refresh = function(scope, ...)
        scans[#scans + 1] = tostring(scope)
        return refresh(scope, ...)
    end
    loadAddon("Window", addon)

    local function game(event, ...)
        assert(eventFrame.events[event], "Missing registration for " .. event)
        eventFrame.scripts.OnEvent(eventFrame, event, ...)
    end
    local function settle()
        for _ = 1, 10 do
            if #timers == 0 then return end
            local batch = timers
            timers = {}
            for _, callback in ipairs(batch) do callback() end
        end
        error("Preflight callbacks did not settle")
    end
    local function warnings()
        local count = 0
        for _, text in ipairs(messages) do
            if text:find("Scoutmaster Vargas", 1, true) then count = count + 1 end
        end
        return count
    end

    game("ADDON_LOADED", "RXPQuestGuard")
    game("PLAYER_ENTERING_WORLD")
    settle()
    assert(addon.settings.autoPreflight and addon.settings.lookahead == 30 and addon.settings.suppressRepeated)
    assert(addon.settings.scanZoneChange and addon.settings.scanGuideChange and addon.settings.scanUpcomingSteps)
    assert(addon.settings.warningNotifications)
    assert(#scans >= 1 and scans[#scans] == "next")
    assert(warnings() == 1, "Login should warn about the upcoming Vargas objective")
    for _, issue in ipairs(addon.report.issues) do assert(issue.id ~= 95261) end
    assert(addon.report.counts.already_satisfied >= 1)

    local before = #scans
    zone = "Amber Crossing"
    game("ZONE_CHANGED_NEW_AREA")
    game("ZONE_CHANGED_NEW_AREA")
    game("QUEST_LOG_UPDATE")
    assert(#scans == before)
    settle()
    assert(#scans == before + 1 and scans[#scans] == "30")
    assert(warnings() == 1, "Rechecking a missing quest should retain the finding without repeating the alert")
    local stillMissing
    for _, issue in ipairs(addon.report.issues) do if issue.id == 95882 then stillMissing = true end end
    assert(stillMissing)

    before = #scans
    RXPCData.currentStep = 42
    eventFrame.scripts.OnUpdate(eventFrame, 0.6)
    settle()
    assert(#scans == before + 1 and scans[#scans] == "30" and addon.report.context.index == 42)
    assert(warnings() == 1, "Reaching the warned objective should not replay the alert")

    active[95882] = true
    game("QUEST_ACCEPTED", 1, 95882)
    settle()
    for _, issue in ipairs(addon.report.issues) do assert(issue.id ~= 95882) end
    active[95882] = nil
    game("QUEST_REMOVED", 95882)
    settle()
    assert(warnings() == 2, "A genuine state change should rearm the warning")

    combat, zone = true, "Marsh"
    game("ZONE_CHANGED_NEW_AREA")
    settle()
    assert(addon.report.notice:find("combat", 1, true) and not addon.report.context.covered)
    combat = false
    game("PLAYER_REGEN_ENABLED")
    settle()
    assert(not addon.report.coverage.combatDeferred and addon.report.coverage.scope == "30")

    local originalFutureSteps = future.steps
    future.steps = {}
    for index = 1, 40 do future.steps[index] = rawStep(index) end
    before = #scans
    RXPCData.currentStep = 60
    eventFrame.scripts.OnUpdate(eventFrame, 0.6)
    settle()
    assert(#scans == before + 1 and addon.report.coverage.scope == "30")
    assert(addon.report.coverage.chapters == 2 and addon.report.coverage.steps == 31,
        "The current step plus 30 upcoming steps should cross the linked chapter boundary")
    future.steps = originalFutureSteps

    before = #scans
    completed[95882] = true
    game("QUEST_TURNED_IN", 95882)
    settle()
    assert(#scans == before + 1 and warnings() == 2)
    completed[95882] = nil

    before = #scans
    live.currentGuide, RXPCData.currentGuideName, RXPCData.currentStep = first, first.name, 1
    assert(callbacks.RXP_GUIDE_LOADED, "Missing RestedXP guide-loaded listener")()
    settle()
    assert(#scans == before + 1 and scans[#scans] == "next" and addon.report.context.name == first.name)

    before = #scans
    addon.settings.scanZoneChange = false
    game("ZONE_CHANGED_NEW_AREA")
    settle()
    assert(#scans == before)
    addon.settings.autoPreflight = false
    local captures, request = 0, addon.snapshot.request
    addon.snapshot.request = function()
        captures = captures + 1
        request()
    end
    zone = "Amber Crossing"
    game("ZONE_CHANGED_NEW_AREA")
    game("QUEST_LOG_UPDATE")
    settle()
    assert(#scans == before and captures > 0)
    game("PLAYER_LOGOUT")
    final.steps[43] = previous
    C_Timer, GetRealZoneText, GetZoneText = nil, nil, nil
end

local function guideAuthorization(live, bindings)
    local fresh, messages = {}, {}
    local frames = mockWidgets()
    SlashCmdList = {}
    RXPQuestGuardSettings, RXPQuestGuardSnapshot = {scope = "10", minimap = false}, nil
    DEFAULT_CHAT_FRAME = {AddMessage = function(_, text) messages[#messages + 1] = text end}
    C_Timer = nil
    UnitName = function() return "Example", "Character" end
    GetRealmName = function() return "Example Realm" end
    GetServerTime = function() return 1764000000 end
    C_QuestLog = {
        GetNumQuestLogEntries = function() return 1, 1 end,
        GetInfo = function() return {questID = 95261} end,
        GetAllCompletedQuestIDs = function() return {95260} end,
        IsOnQuest = function(id) return id == 95261 end,
        IsQuestFlaggedCompleted = function(id) return id == 95260 end,
    }
    local guideReads, publishedReads, progressReads = 0, 0, 0
    LibStub = function(...)
        guideReads = guideReads + 1
        return bindings.LibStub(...)
    end
    RXPCData = setmetatable({}, {__index = function(_, key)
        progressReads = progressReads + 1
        return bindings.RXPCData[key]
    end})
    fresh.data = setmetatable({schemaVersion = 1, quests = {}}, {__index = function(_, key)
        if key == "route" then publishedReads = publishedReads + 1; return {} end
    end})
    for _, name in ipairs({"Engine", "QuestLog", "RestedXP", "Snapshot", "Core"}) do loadAddon(name, fresh) end
    local frame = frames[#frames]
    fresh.open = function() end
    frame.scripts.OnEvent(frame, "ADDON_LOADED", "RXPQuestGuard")
    frame.scripts.OnEvent(frame, "PLAYER_ENTERING_WORLD")
    frame.scripts.OnEvent(frame, "QUEST_LOG_UPDATE")
    frame.scripts.OnUpdate(frame, 2)
    local slash = SlashCmdList.RXPQUESTGUARD
    slash("status")
    slash("scan route")
    slash("chapters")
    slash("check 95261")
    assert(not fresh.settings.authorizedGuides and not fresh.settings.minimap and fresh.settings.scope == "10")
    assert(guideReads == 0 and progressReads == 0 and publishedReads == 0,
        ("Unauthorized guide access counts: %d loaded, %d progress, %d published"):format(
            guideReads, progressReads, publishedReads))
    assert(fresh.report.coverage.route == "disabled" and fresh.report.coverage.steps == 0)
    assert(RXPQuestGuardSnapshot.active[1] == 95261 and RXPQuestGuardSnapshot.currentGuide == nil)
    assert(table.concat(messages, "\n"):find("95261): Active", 1, true))

    slash("guides allow")
    assert(fresh.settings.authorizedGuides and guideReads > 0 and progressReads > 0 and publishedReads > 0)
    assert(RXPQuestGuardSnapshot.currentGuide == live.currentGuide.name)
    guideReads, progressReads, publishedReads = 0, 0, 0
    slash("guides off")
    frame.scripts.OnEvent(frame, "QUEST_LOG_UPDATE")
    frame.scripts.OnUpdate(frame, 2)
    assert(not fresh.settings.authorizedGuides and guideReads == 0 and progressReads == 0 and publishedReads == 0)
    assert(fresh.lastAutomaticReport == nil and RXPQuestGuardSnapshot.currentGuide == nil)
    frame.scripts.OnEvent(frame, "PLAYER_LOGOUT")
end

regressions()
questApi()
runtime(bridge())
reloadStatus(bridge())
snapshotLifecycle(bridge())
automaticProtection(bridge())
guideAuthorization(bridge())
print("Addon Lua 5.1 regressions passed (guide authorization, quest checks, triggers, combat, launcher, status and reload).")
