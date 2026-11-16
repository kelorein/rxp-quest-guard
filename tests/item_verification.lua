GetBuildInfo = function() return "1.60.1", "70938", nil, 16001 end
local addon = {}
for _, file in ipairs({"Data", "Engine", "QuestLog"}) do
    assert(loadfile("addon/RXPQuestGuard/" .. file .. ".lua"))("RXPQuestGuard", addon)
end

local E, Query = addon.engine, addon.questLog
local function unavailable(id)
    return {active = false, completed = false}
end

local function scan(stateFor, inventory, metadata, current, earlier)
    return E.scan({
        quests = {[654] = metadata or {}}, player = {level = 44}, inventory = inventory,
        stateFor = stateFor, steps = {{chapter = "Quartz Basin", index = 4, current = current ~= false,
            actions = {{questId = 654, kind = "complete"}}}},
        prior = {[654] = earlier},
    })
end

local rawBag, rawQuest, active, completed, disabled, count, bagChecks = 8524, 654, false, false, false, 1, 0
local timer, elapsed
local function forbidden()
    error("Quest Guard must only observe WoW. Gameplay and network actions are out of scope.")
end
local api = {
    C_Container = {
        GetContainerNumSlots = function(bag) bagChecks = bagChecks + 1; return bag == 0 and 1 or 0 end,
        GetContainerItemInfo = function()
            if rawBag then return {itemID = rawBag, stackCount = 1, itemName = "Model 4711-FTZ Power Source"} end
        end,
        GetContainerItemQuestInfo = function() return {isQuestItem = false, questID = rawQuest, isActive = active} end,
        UseContainerItem = forbidden,
        PickupContainerItem = forbidden,
    },
    C_Item = {
        GetItemCount = function(id, bank, uses, reagent, account)
            assert(id == 8524 and bank == false and uses == false and reagent == false and account == false)
            return count
        end,
        IsEquippedItem = function() return false end,
        GetItemSpell = forbidden,
        RequestLoadItemDataByID = forbidden,
    },
    C_QuestLog = {
        IsOnQuest = function(id) return id == 654 and active end,
        IsQuestFlaggedCompleted = function(id) return id == 654 and completed end,
        IsQuestDisabledForSession = function(id) return id == 654 and disabled end,
        GetTimeAllowed = function(id) if id == 654 then return timer, elapsed end end,
    },
    AcceptQuest = forbidden,
    UseItemByName = forbidden,
}

-- The game itself supplies the exact item-to-quest mapping in this fixture.
local inventory = Query.inventory(api, true)
local state = Query.reader(api, true)
local before = bagChecks
local report = scan(state, inventory)
assert(#report.issues == 1 and report.issues[1].code == "starter-ready")
assert(report.issues[1].status == "Ready to activate from item" and report.issues[1].severity == "info")
assert(report.issues[1].item.itemId == 8524 and report.issues[1].item.source == "Live WoW bag API")
assert(report.counts.ready_to_activate == 1 and report.counts.unaccepted_objective == 0)
assert(E.timer(report.issues[1].meta, report.issues[1].state) == nil)
assert(bagChecks > before)
before = bagChecks
assert(scan(state, Query.inventory(api, true)).issues[1].code == "starter-ready" and bagChecks == before)

-- Active and completed game state win regardless of independently published data or a held item.
active, timer, elapsed = true, 7200, 60
Query.invalidateInventory()
report = scan(Query.reader(api, true), Query.inventory(api, true), {startItem = {itemId = 8524, verifiedForever = true}})
assert(#report.issues == 0 and report.current[1].status == "Active")
local total, remaining = E.timer(report.current[1].meta, report.current[1].state)
assert(total == 7200 and remaining == 7140)
active, completed = false, true
report = scan(Query.reader(api, true), Query.inventory(api, true))
assert(report.current[1].status == "Completed" and report.counts.ready_to_activate == 0)

-- A bag item plus an inherited Classic association or timer does not establish readiness.
completed, rawQuest = false, nil
Query.invalidateInventory()
local classic = {startItem = {itemId = 8524, name = "Model 4711-FTZ Power Source"}, timerSeconds = 7200,
    fieldEvidence = {
        startItem = {{value = {itemId = 8524}, inheritedClassic = true, provider = "Reference"}},
        timerSeconds = {{value = 7200, inheritedClassic = true, provider = "Reference"}},
    }}
report = scan(Query.reader(api, true), Query.inventory(api, true), classic, false)
assert(report.issues[1].severity == "review" and report.issues[1].item.present)
assert(not report.issues[1].item.verified and E.timer(classic, {}) == nil)

-- Future and unavailable states do not masquerade as verified authorization.
report = scan(function() return {} end, Query.inventory(api, true), classic)
assert(report.current[1].status == "Unknown" and report.counts.ready_to_activate == 0)
rawQuest, disabled = 654, true
Query.invalidateInventory()
report = scan(Query.reader(api, true), Query.inventory(api, true))
assert(report.current[1].status == "Not eligible by known restrictions")
assert(report.counts.ready_to_activate == 0)

-- Authorized, explicit Forever claims can pair with bag counts if the bag mapping API is absent.
disabled, rawBag, rawQuest = false, nil, nil
local restore = api.C_Container
api.C_Container = nil
Query.invalidateInventory()
local verified = {startItem = {itemId = 8524, name = "Model 4711-FTZ Power Source", verifiedForever = true},
    timerSeconds = 7200, source = "Fixture with Forever evidence"}
report = scan(Query.reader(api, true), Query.inventory(api, true), verified)
assert(report.issues[1].code == "starter-ready" and E.timer(verified, {}) == 7200)
assert(E.duration(7200) == "2 hr")
local outdated = {
    startItem = verified.startItem, timerSeconds = verified.timerSeconds,
    fieldEvidence = {
        startItem = {{value = {itemId = 8524}, confidence = "forever-specific", gameVersion = "1.60.1.69000"}},
        timerSeconds = {{value = 7200, confidence = "forever-specific", gameVersion = "WOW-69000patch1.60.1"}},
    },
}
report = scan(Query.reader(api, true), Query.inventory(api, true), outdated)
assert(report.issues[1].code ~= "starter-ready" and E.timer(outdated, {}) == nil)
count = 0
report = scan(Query.reader(api, true), Query.inventory(api, true), verified)
assert(report.issues[1].code ~= "starter-ready" and report.issues[1].item.present == false)
report = scan(Query.reader(api, true), nil, verified)
assert(report.issues[1].code == "starter-unknown" and report.issues[1].severity == "review")
api.C_Container = restore

-- Conflicting claims cannot establish blockers, usable starts, or waypoints.
local conflicting = {requirements = {{questId = 1}}, startLocation = {mapId = 7, x = 5, y = 5},
    conflicts = {{field = "requirements", claims = {}}, {field = "startLocation", claims = {}}}}
report = scan(unavailable, nil, conflicting)
assert(report.counts.confirmed_blocker == 0 and report.issues[1].verification == "Conflicting sources")
assert(report.issues[1].location == nil and report.issues[1].locationConflict)
assert(E.requirements(conflicting, unavailable, {}) == nil)
local weak = {requirements = {{questId = 1}}, fieldEvidence = {
    requirements = {{value = {{questId = 1}}, inheritedClassic = true}},
}}
assert(E.requirements(weak, unavailable, {}) == nil)
local hard = {requirements = {{questId = 1}}, source = "Authored test evidence"}
report = scan(unavailable, nil, hard)
assert(report.counts.confirmed_blocker == 1 and report.issues[1].dependencies[1].result == "Not completed")

local earlier = {chapter = "Earlier", index = 1, location = {mapId = 7, x = 50, y = 50}}
report = scan(unavailable, nil, {startLocation = {mapId = 7, x = 4, y = 4}}, false, earlier)
assert(report.counts.confirmed_missing_pickup == 1 and report.issues[1].locationConflict)
assert(report.issues[1].location == nil and report.issues[1].guideLocation.x == 50)

-- Live item data takes precedence over published disagreement when the exact quest ID is returned.
rawBag, rawQuest, count = 8524, 654, 1
Query.invalidateInventory()
report = scan(Query.reader(api, true), Query.inventory(api, true), {
    startItem = {itemId = 999, verifiedForever = true}, conflicts = {{field = "startItem", claims = {}}},
})
assert(report.issues[1].code == "starter-ready" and report.issues[1].item.itemId == 8524)
assert(report.issues[1].verification == "Conflicting sources")

-- Simulate the normal addon event entry points without launching WoW.
local scripts, messages = {}, {}
CreateFrame = function()
    return {RegisterEvent = function() end,
        SetScript = function(_, event, callback) scripts[event] = callback end}
end
SlashCmdList = {}
RXPQuestGuardSettings = {notify = "chat", authorizedGuides = true}
DEFAULT_CHAT_FRAME = {AddMessage = function(_, message) messages[#messages + 1] = message end}
C_QuestLog, C_Container, C_Item = api.C_QuestLog, api.C_Container, api.C_Item
local guide = {name = "Quartz Basin", group = "Test", key = "Test||Quartz Basin", steps = {{
    elements = {{tag = "complete", questId = 654, title = "Tanaris Field Sampling"}},
}}}
RXPCData = {currentGuideName = guide.name, currentGuideGroup = guide.group, currentStep = 1}
LibStub = function(name)
    if name == "AceAddon-3.0" then
        return {GetAddon = function() return {currentGuide = guide, release = "v4.11.36"} end}
    end
end
for _, file in ipairs({"RestedXP", "Snapshot", "Core"}) do
    assert(loadfile("addon/RXPQuestGuard/" .. file .. ".lua"))("RXPQuestGuard", addon)
end
scripts.OnEvent(nil, "ADDON_LOADED", "RXPQuestGuard")
scripts.OnEvent(nil, "PLAYER_ENTERING_WORLD", true, false)
assert(addon.report.issues[1].code == "starter-ready" and #messages == 1)
assert(messages[1]:find("Activate manually", 1, true) and not messages[1]:find("2 hr", 1, true))
scripts.OnEvent(nil, "BAG_UPDATE_DELAYED")
assert(#messages == 1)
active = true
scripts.OnEvent(nil, "QUEST_ACCEPTED", 654)
assert(#addon.report.issues == 0 and addon.report.current[1].status == "Active" and #messages == 1)
print("Lua 5.1 item and evidence regressions passed (fixture data, no live WoW actions).")
