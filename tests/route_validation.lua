-- Route confidence and quest classification regressions shared by the Lua harness.
local addon = {settings = {authorizedGuides = true}}
for _, file in ipairs({"Engine", "QuestLog", "RestedXP", "Snapshot"}) do
    assert(loadfile("addon/RXPQuestGuard/" .. file .. ".lua"))("RXPQuestGuard", addon)
end

local E, R, Snapshot = addon.engine, addon.restedXP, addon.snapshot
local active, completed = {[95261] = true}, {[95260] = true}
local function current(id)
    return {active = active[id] == true, completed = completed[id] == true}
end

local function step(index, kind, id, isCurrent, optional)
    return {chapter = "Marsh", index = index, current = isCurrent, optional = optional,
        actions = {{kind = kind, questId = id}}}
end

local prior = {[95882] = {chapter = "Marsh I", index = 9}}
local function scan(steps, options)
    options = options or {}
    options.steps, options.stateFor = steps, current
    return E.scan(options)
end

local report = scan({step(42, "complete", 95882), step(46, "turnin", 95882)}, {prior = prior})
assert(report.counts.confirmed_missing_pickup == 1 and #report.issues == 2)
assert(report.issues[1].category == "confirmed_missing_pickup" and report.issues[1].severity == "warning")
prior[95882].unverified = true
report = scan({step(42, "complete", 95882)}, {prior = prior})
assert(report.counts.confirmed_missing_pickup == 0 and report.counts.needs_review == 1)
report = scan({step(42, "complete", 95882, true)}, {prior = prior})
assert(report.counts.unaccepted_objective == 1 and report.issues[1].severity == "warning")
assert(report.counts.confirmed_missing_pickup == 0)

report = scan({step(42, "complete", 95882, true, true)}, {prior = prior})
assert(report.counts.optional_objective == 1 and report.issues[1].severity == "info")
local requirements = {[95267] = {requirements = {{questId = 95261, state = "completed"}}}}
report = scan({step(62, "accept", 95267, true)}, {quests = requirements})
assert(report.counts.confirmed_blocker == 1 and report.issues[1].severity == "blocker")
assert(report.issues[1].classification == "Unknown")
report = scan({step(62, "complete", 95261, true), step(64, "turnin", 95261),
    step(65, "accept", 95267), step(66, "complete", 95267)}, {quests = requirements})
assert(#report.issues == 0 and report.counts.already_satisfied == 1 and report.counts.future_planned == 1)
assert(report.current[1].status == "Active")

local conditional = step(1, "accept", 95267)
conditional.conditional = true
report = scan({conditional, step(2, "complete", 95267)}, {quests = requirements})
assert(#report.issues == 0 and report.counts.future_planned == 1)
report = scan({step(1, "complete", 95882), step(2, "accept", 95882)})
assert(#report.issues == 0 and report.counts.future_planned == 1)
local sameStep = step(1, "accept", 95882, true)
sameStep.actions[2] = {kind = "complete", questId = 95882}
report = scan({sameStep})
assert(#report.issues == 0 and report.counts.future_planned == 1)
report = scan({step(1, "complete", 95882)}, {quests = {[95882] = {faction = "Alliance"}}, player = {faction = "Horde"}})
assert(#report.issues == 0)

local api = {
    GetBuildInfo = function() return "1.60.1", "70938", "", 16001 end,
    GetRealmName = function() return "Test Realm" end,
    UnitName = function() return "Sample", "Character" end,
}
local context = {version = "v4.11.36"}
local route = {
    {id = "H|Example|58-59 First", name = "58-59 First", group = "H", subgroup = "Example",
        guideVersion = 15, levelMin = 58, levelMax = 59, steps = {{index = 1, actions = {}}}},
    {id = "H|Example|59-60 Last", name = "59-60 Last", group = "H", subgroup = "Example",
        levelMin = 59, levelMax = 60, steps = {{index = 1, actions = {}}}},
}
local data = {
    schemaVersion = 1, route = route,
    routeContext = {character = "Sample-Character", realm = "Test Realm", clientInterface = 16001},
    routeInfo = {targetLevel = 60, terminalChapterId = route[2].id, complete = true,
        guideRelease = "v4.11.36", clientVersion = "1.60.1.70938"},
}
local usable, message, details = Snapshot.routeFor(api, data, context)
assert(usable == route and not message and details.status == "verified" and details.trusted and details.complete)
assert(details.targetLevel == 60 and details.chapterCount == 2)
local guide = {name = route[1].name, group = route[1].group, subgroup = route[1].subgroup, version = 15}
context.name, context.group, context.guide = guide.name, guide.group, guide
local index = R.matchRoute(route, context)
assert(index == 1)
guide.version = 16
index, message, details = R.matchRoute(route, context)
assert(not index and details.status == "stale" and message:find("guide version", 1, true))
usable, message, details = Snapshot.routeFor(api, data, context)
assert(#usable == 0 and details.status == "stale" and message:find("guide version", 1, true))
guide.version = 15
context.version = "v4.12.0"
usable, message, details = Snapshot.routeFor(api, data, context)
assert(#usable == 0 and details.status == "stale" and message:find("RestedXP", 1, true))
context.version = "v4.11.36"
data.routeContext.realm = "Unrelated Realm"
usable, _, details = Snapshot.routeFor(api, data, context)
assert(#usable == 0 and details.status == "mismatched")
data.routeContext.realm = "Test Realm"
data.routeInfo.clientVersion = "1.60.1.99999"
usable, _, details = Snapshot.routeFor(api, data, context)
assert(#usable == 0 and details.status == "stale")
data.routeInfo.clientVersion = "1.60.1.70938"
local steps = route[2].steps
route[2].steps = {}
usable, _, details = Snapshot.routeFor(api, data, context)
assert(usable == route and details.status == "unknown" and not details.trusted and not details.complete)
route[2].steps = steps
data.routeInfo.terminalChapterId = "unknown"
usable, _, details = Snapshot.routeFor(api, data, context)
assert(usable == route and details.status == "unknown" and not details.trusted and not details.complete)
data.routeContext, data.routeInfo = nil, nil
usable, _, details = Snapshot.routeFor(api, data)
assert(usable == route and details.status == "unknown" and not details.complete)

guide.next = route[2].name
context.addon = {guides = {guide, route[2]}}
local loaded, position, _, coverage = R.loadedRoute(context)
assert(loaded[2] == route[2] and position == 1 and coverage.status == "terminal" and coverage.lastLevel == 60)
guide.next = "59-60 Alternative;59-60 Last"
loaded, _, _, coverage = R.loadedRoute(context)
assert(#loaded == 1 and coverage.status == "ambiguous")
