local _, QG = ...
local pane, toast, minimap, reportBody, settingsBody, sourceCopy
local mode, selected, page = "report", nil, 1
local selectionKey, lastScope, lastGuide
local backdrop = BackdropTemplateMixin and "BackdropTemplate" or nil
local colors = {gold = {0.98, 0.80, 0.48}, text = {0.93, 0.92, 0.85},
    muted = {0.72, 0.71, 0.61}, warning = {1, 0.55, 0.38}, info = {0.62, 0.82, 0.92}}
local pageSize = 5
local categoryLabels = {
    confirmed_blocker = "Confirmed blocker", confirmed_missing_pickup = "Missed pickup",
    unaccepted_objective = "Unaccepted objective", optional_objective = "Optional objective",
    needs_review = "Review", future_planned = "Future pickup planned", already_satisfied = "Satisfied",
    ready_to_activate = "Ready to activate", conflicting_sources = "Conflicting sources",
}
local fields = {
    {"startItem", "Starting item"}, {"timerSeconds", "Timer"}, {"requirements", "Prerequisites"},
    {"requirementGroups", "Prerequisite alternatives"}, {"startLocation", "Pickup location"},
    {"endLocation", "Turn-in location"}, {"chain", "Quest chain"}, {"name", "Name"},
    {"classification", "Route requirement"}, {"minLevel", "Required level"}, {"faction", "Faction"},
    {"classes", "Classes"},
}

local function paint(frame)
    if frame.SetBackdrop then
        frame:SetBackdrop({
            bgFile = "Interface\\Buttons\\WHITE8X8",
            edgeFile = "Interface\\DialogFrame\\UI-DialogBox-Border", edgeSize = 24,
            insets = {left = 7, right = 7, top = 7, bottom = 7},
        })
        frame:SetBackdropColor(0.075, 0.065, 0.06, 0.96)
        frame:SetBackdropBorderColor(0.76, 0.62, 0.43, 1)
    else
        local bg = frame:CreateTexture(nil, "BACKGROUND")
        bg:SetAllPoints()
        bg:SetColorTexture(0.075, 0.065, 0.06, 0.96)
    end
end

local function font(parent, style, color)
    local text = parent:CreateFontString(nil, "OVERLAY", style)
    text:SetJustifyH("LEFT")
    text:SetJustifyV("TOP")
    text:SetTextColor(unpack(colors[color]))
    return text
end

local function button(parent, label, width, onClick)
    local widget = CreateFrame("Button", nil, parent, "UIPanelButtonTemplate")
    widget:SetSize(width, 22)
    widget:SetText(label)
    widget:SetScript("OnClick", onClick)
    return widget
end

local function enable(widget, on)
    if on then widget:Enable() else widget:Disable() end
end

local function section(parent)
    local line = parent:CreateTexture(nil, "ARTWORK")
    line:SetHeight(1)
    line:SetColorTexture(0.65, 0.49, 0.26, 0.45)
    return line
end

local function safeText(value)
    return tostring(value):gsub("[%c]", " "):gsub("|", "||")
end

local function known(value)
    return value and safeText(value) or "Unknown"
end

local function position(row)
    local point = row and not row.locationConflict and row.code ~= "starter-ready" and row.location
    local x, y = point and tonumber(point.x), point and tonumber(point.y)
    if not (x and y and x >= 0 and y >= 0 and x <= 100 and y <= 100) then return nil end
    return point, x, y, QG.engine.questId(point.mapId)
end

local function pickupString(row)
    local location = row.location
    local pickup = row.pickup
    local parts = {}
    if pickup then
        local step = pickup.index and (" step " .. pickup.index) or ""
        parts[#parts + 1] = safeText(pickup.chapter) .. step
        if pickup.flagged then parts[#parts + 1] = "Pickup has a RestedXP step flag" end
    end
    if location and location.name then parts[#parts + 1] = safeText(location.name) end
    if location and location.zone then parts[#parts + 1] = safeText(location.zone) end
    local _, x, y = position(row)
    if x then parts[#parts + 1] = string.format("%.1f, %.1f", x, y) end
    local marker = pickup and pickup.location
    if marker and marker ~= location and marker.name and (not location or marker.name ~= location.name) then
        parts[#parts + 1] = "Guide marker " .. safeText(marker.name)
    end
    local raw = marker and marker.worldMapId and marker or location
    if raw and type(raw.worldX) == "number" and type(raw.worldY) == "number" then
        parts[#parts + 1] = string.format("World coordinates %.1f, %.1f (map %s, floor %s)",
            raw.worldX, raw.worldY, known(raw.worldMapId), known(raw.worldFloor))
    end
    return #parts > 0 and table.concat(parts, "  •  ") or "Unknown"
end

local function publicUrl(value)
    if type(value) == "string" and value:match("^https://[%w%.%-]+[/?:#]?[^%s%c]*$") then return value end
end

local function copySource(url)
    if not sourceCopy or not url then return end
    sourceCopy:SetText(url)
    sourceCopy:Show()
    sourceCopy:SetFocus()
    sourceCopy:HighlightText()
    QG.note("Copy the selected data source URL with Ctrl+C.")
end

local function rowSource(row)
    if not row then return nil end
    local evidence = QG.engine.list(row.meta.fieldEvidence)
    for _, field in ipairs(fields) do
        for _, claim in ipairs(QG.engine.list(evidence[field[1]])) do
            local url = type(claim) == "table" and publicUrl(claim.sourceUrl)
            if url then return url end
        end
    end
    for _, conflict in ipairs(QG.engine.list(row.meta.conflicts)) do
        if type(conflict) == "table" then
            for _, claim in ipairs(QG.engine.list(conflict.claims)) do
                local url = type(claim) == "table" and publicUrl(claim.sourceUrl)
                if url then return url end
            end
        end
    end
    return publicUrl(row.meta.sourceUrl)
end

local function dropWaypoint(row)
    local _, x, y, mapId = position(row)
    if mapId and TomTom and type(TomTom.AddWaypoint) == "function" then
        local ok, point = pcall(TomTom.AddWaypoint, TomTom, mapId, x / 100, y / 100, {
            title = "Quest pickup for " .. row.name, source = "RXP Quest Guard", persistent = false,
        })
        if ok and point then
            QG.note("TomTom waypoint added for " .. row.name .. ".")
            return
        end
    end
    QG.note("TomTom could not add a waypoint for this pickup.")
end

local function requirementsText(row)
    local meta = row.meta
    if QG.engine.conflict(meta, "requirements") or QG.engine.conflict(meta, "requirementGroups") then
        return "Disputed. Review the source claims below."
    end
    if not QG.engine.actionable(meta, "requirements") or not QG.engine.actionable(meta, "requirementGroups") then
        return "Unverified for Forever. See the reference evidence below."
    end
    local function list(group)
        local entries = {}
        for _, req in ipairs(QG.engine.list(group)) do
            entries[#entries + 1] = tostring(req.questId) .. " " .. (req.state or "completed")
        end
        return table.concat(entries, " + ")
    end
    local fixed = list(meta.requirements)
    local groups = {}
    for _, group in ipairs(QG.engine.list(meta.requirementGroups)) do groups[#groups + 1] = list(group) end
    if #groups > 0 then
        local alternatives = "(" .. table.concat(groups, ") or (") .. ")"
        return fixed ~= "" and fixed .. " + " .. alternatives or alternatives
    end
    return fixed ~= "" and fixed or (meta.requirementsKnown and "None listed by the source" or "Unknown")
end

local function claimValue(value, depth)
    if type(value) ~= "table" then return known(value) end
    local id = value.itemId or value.questId
    if id then
        local name = value.name and (" " .. safeText(value.name)) or ""
        return "#" .. tostring(id) .. name .. (value.state and (" " .. safeText(value.state)) or "")
    end
    if type(value.x) == "number" and type(value.y) == "number" then
        return (value.zone and (safeText(value.zone) .. " ") or "") ..
            string.format("%.1f, %.1f", value.x, value.y) .. (value.name and (" " .. safeText(value.name)) or "")
    end
    if value.name then return safeText(value.name) end
    if depth < 2 then
        local items = {}
        for index, entry in ipairs(value) do
            if index > 5 then items[#items + 1] = "…"; break end
            items[#items + 1] = claimValue(entry, depth + 1)
        end
        if #items > 0 then return table.concat(items, ", ") end
    end
    return next(value) and "See source for details" or "No entries"
end

local function claimText(claim, key)
    local label
    if claim.inheritedClassic then label = "Inherited Classic"
    elseif claim.guideDerived then label = "Guide-derived inference"
    elseif claim.observedForever then label = "Forever-specific evidence"
    else label = claim.confidence and safeText(claim.confidence) or "Unverified" end
    local value = claimValue(claim.value, 0)
    if key == "timerSeconds" and type(claim.value) == "number" and claim.value > 0 then
        value = QG.engine.duration(claim.value)
    end
    local source = known(claim.provider) .. " · " .. label
    local dateValue = claim.verifiedAt or claim.retrievedAt
    if dateValue then source = source .. " · " .. safeText(dateValue) end
    if claim.gameVersion then source = source .. " · Build " .. safeText(claim.gameVersion) end
    if claim.method then source = source .. " · " .. safeText(claim.method) end
    if publicUrl(claim.sourceUrl) then source = source .. " · " .. safeText(claim.sourceUrl) end
    return value .. "  —  " .. source
end

local function showEvidence(words, meta)
    local conflicted = {}
    for _, conflict in ipairs(QG.engine.list(meta.conflicts)) do
        if type(conflict) == "table" and type(conflict.field) == "string" then
            conflicted[conflict.field] = true
            words[#words + 1] = (conflict.unresolved == false and "Different source reports" or "Unresolved source conflict") ..
                "    " .. safeText(conflict.field)
            for _, claim in ipairs(QG.engine.list(conflict.claims)) do
                if type(claim) == "table" then words[#words + 1] = claimText(claim, conflict.field) end
            end
        end
    end
    local evidence = QG.engine.list(meta.fieldEvidence)
    for _, field in ipairs(fields) do
        if not conflicted[field[1]] then
            for index, claim in ipairs(QG.engine.list(evidence[field[1]])) do
                if type(claim) == "table" then
                    if index == 1 then words[#words + 1] = field[2] .. " evidence" end
                    words[#words + 1] = claimText(claim, field[1])
                end
            end
        end
    end
end

local function progressText(row)
    local all = row.state.objectives
    if not all or not row.objectiveIndex then return nil end
    local obj = all[row.objectiveIndex]
    if type(obj) ~= "table" then return nil end
    if type(obj.numFulfilled) == "number" and type(obj.numRequired) == "number" then
        return string.format("Objective %d progress    %d / %d", row.objectiveIndex, obj.numFulfilled, obj.numRequired)
    end
    if obj.finished then return "Objective complete" end
end

local function detail(row)
    if not row then
        return "No quest objective to display. Move to a quest step, choose a local chapter, or use /qg check <quest ID>."
    end
    local title = safeText(row.name) .. "  #" .. row.id
    local words = {
        title,
        row.status .. "  •  " .. row.kind .. "  •  Route requirement " .. row.classification:lower(),
    }
    if categoryLabels[row.category] then words[#words + 1] = categoryLabels[row.category] end
    if row.verification then words[#words + 1] = row.verification end
    if row.message then words[#words + 1] = row.message end
    if row.chapter then words[#words + 1] = "Guide step    " .. safeText(row.chapter) .. " / " .. tostring(row.index or "?") end
    local pickup = pickupString(row)
    words[#words + 1] = "Pickup    " .. pickup
    if row.locationConflict then
        words[#words + 1] = "Pickup location is disputed. Check the source before traveling."
        if row.publishedLocation and row.guideLocation then
            words[#words + 1] = "Published location    " .. claimValue(row.publishedLocation, 0)
            words[#words + 1] = "Guide location    " .. claimValue(row.guideLocation, 0)
        end
    end
    words[#words + 1] = "Required quests    " .. requirementsText(row)
    for _, dependency in ipairs(QG.engine.list(row.dependencies)) do
        local name = dependency.name and (safeText(dependency.name) .. " ") or ""
        local option = dependency.option and (" • Option " .. dependency.option) or ""
        words[#words + 1] = name .. "#" .. dependency.id .. "  •  Needs " .. dependency.requirement ..
            "  •  " .. dependency.result .. option
    end
    if row.item then
        local itemName = row.item.name and safeText(row.item.name) or "Item"
        local presence = row.item.present == true and "In your bags" or row.item.present == false and
            "Absent from your bags" or "Bag presence unknown"
        words[#words + 1] = itemName .. " #" .. row.item.itemId .. "  •  " .. presence
        words[#words + 1] = row.item.verified and ("Item evidence    " .. known(row.item.source)) or
            "Quest-start mapping unverified for this Forever client."
        if row.item.disagrees then words[#words + 1] = "The live bag and published item mapping differ." end
        if row.item.present == false then
            words[#words + 1] = "Verify how to obtain the item before traveling for it."
        end
    end
    local duration, remaining = QG.engine.timer(row.meta, row.state)
    if remaining then
        words[#words + 1] = "Time remaining at last check    " .. QG.engine.duration(remaining) .. "  •  Live WoW quest API"
    elseif duration then
        words[#words + 1] = "Published time limit    " .. QG.engine.duration(duration)
    end
    local progress = progressText(row)
    if progress then words[#words + 1] = progress end
    if row.code == "unaccepted" then
        if row.severity == "review" then
            words[#words + 1] = "Review the pickup and branch before departing."
        elseif row.classification == "Skippable" then
            words[#words + 1] = "Accept the quest before proceeding, or skip the optional objective."
        else
            words[#words + 1] = "Accept the quest before traveling to the objective."
        end
        if not row.guarded then words[#words + 1] = "No supported quest-state guard was found on this step." end
    elseif row.code == "prerequisite" then
        words[#words + 1] = "Complete the documented prerequisites before continuing."
    end
    local provenance = "Evidence    " .. known(row.source) .. " / live quest APIs"
    if row.meta.source then provenance = provenance .. " / " .. safeText(row.meta.source) end
    if row.meta.confidence then provenance = provenance .. " / " .. safeText(row.meta.confidence) end
    words[#words + 1] = provenance
    showEvidence(words, row.meta)
    return table.concat(words, "\n")
end

local function setDetail(value)
    pane.detail.text:SetText(value)
    pane.detail.content:SetHeight(math.max(80, pane.detail.text:GetStringHeight() + 5))
    pane.detail:SetVerticalScroll(0)
end

local function setMinimap()
    if minimap then minimap:SetShown(QG.settings.minimap == true) end
end

local function debugText(report)
    local current, data = report.context, report.data
    local build, patch, interface
    if GetBuildInfo then
        local v, b, _, i = GetBuildInfo()
        build, patch, interface = b, v, i
    end
    local skipped = 0
    for _, value in pairs(current.skipped or {}) do if value then skipped = skipped + 1 end end
    local log = C_QuestLog or {}
    local capture = type(RXPQuestGuardSnapshot) == "table" and RXPQuestGuardSnapshot.capturedAt
    local capturedAt
    if type(capture) == "number" and type(date) == "function" then
        local ok, formatted = pcall(date, "%Y-%m-%d %H:%M:%S", capture)
        if ok then capturedAt = formatted end
    end
    local coverage, counts = report.coverage or {}, report.counts or {}
    local automatic = QG.lastAutomaticReport
    local text = {
        "RestedXP " .. known(current.version) .. "  •  " .. known(current.status),
        "Client " .. known(patch) .. " / build " .. known(build) .. " / interface " .. known(interface),
        "Level " .. known(report.player.level) .. "  •  " .. known(report.player.faction) .. "  •  " ..
            known(report.player.class) .. "  •  " .. known(GetZoneText and GetZoneText()),
        "Guide group    " .. known(current.group),
        "Recorded step flags    " .. (current.skipped and tostring(skipped) or "Unknown"),
        "Live quest API    " .. (type(log.IsOnQuest) == "function" and "C_QuestLog" or "Compatibility fallback"),
        "Recorded quest data source    " .. known(data.source),
        "Source version    " .. known(data.sourceVersion),
        "Data updated    " .. known(data.updatedAt),
        "Last successful capture    " .. known(capturedAt),
        known(QG.snapshot.status or "Waiting to capture character progress"),
        "Local route chapters    " .. tostring(#QG.engine.list(data.route)),
        "Published route    " .. known(coverage.route),
        "Automatic preflight    " .. (QG.settings.autoPreflight and "On" or "Off") ..
            (automatic and ("  •  Last scan " .. automatic.scope) or ""),
        "Plan results    " .. (counts.future_planned or 0) .. " future pickups  •  " ..
            (counts.already_satisfied or 0) .. " satisfied  •  " .. (counts.needs_review or 0) .. " to review",
        "Use /reload or logout to save quest progress. Use /reload to read desktop updates.",
    }
    return table.concat(text, "\n")
end

local function currentSummary(report)
    if #report.current == 0 then return "Current objective  •  Unknown or no quest directive" end
    local first = report.current[1]
    local additional = #report.current > 1 and ("  +" .. (#report.current - 1)) or ""
    return "Current  •  " .. safeText(first.name) .. "  •  " .. first.status .. additional
end

function QG.render(report)
    if not pane or not pane:IsShown() or not report then return end
    local context = report.context
    local name = context.name and safeText(context.name) or "No active chapter"
    local step = context.index and ("  •  Step " .. context.index) or ""
    local tail = context.manual and "Manual planning" or known(context.status)
    pane.sub:SetText(name .. step .. "\n" .. tail .. "  •  RestedXP " .. known(context.version))
    local attribution = report.data.sourceUrl
    pane.attribution.url = attribution
    pane.attribution.label:SetText(attribution and ("ForeverDB.net  •  " .. safeText(attribution)) or "")
    pane.attribution:SetShown(attribution ~= nil)
    reportBody:SetShown(mode == "report")
    settingsBody:SetShown(mode == "settings")
    pane.debug:SetShown(mode == "debug")
    if mode == "debug" then pane.debug:SetText(debugText(report)); return end
    if mode == "settings" then
        for key, control in pairs(pane.settingsToggles) do
            control:SetText(control.label .. "  •  " .. (QG.settings[key] and "On" or "Off"))
        end
        pane.lookahead:SetText("Lookahead  •  " .. QG.settings.lookahead .. " steps")
        pane.scope:SetText("Manual scan  •  " .. QG.scopes[QG.settings.scope])
        pane.notify:SetText("Alert style  •  " .. (QG.settings.notify == "chat" and "Chat" or "Compact"))
        return
    end
    if lastScope ~= report.scope or lastGuide ~= context.name then
        page, selectionKey = 1, nil
        lastScope, lastGuide = report.scope, context.name
    end
    local rows, listed = {}, {}
    for _, row in ipairs(report.issues) do rows[#rows + 1], listed[row.key] = row, true end
    for _, row in ipairs(report.current) do
        if not listed[row.key] then rows[#rows + 1], listed[row.key] = row, true end
    end
    local count = math.max(1, math.ceil(#rows / pageSize))
    page = math.min(page, count)
    pane.current:SetText(currentSummary(report))
    local prefix = report.scope .. "  •  " .. report.warnings .. " warnings  •  " .. #report.issues .. " findings"
    if report.coverage and report.coverage.steps > 0 then
        prefix = prefix .. "  •  " .. report.coverage.steps .. " steps"
    end
    if context.nextName then prefix = prefix .. "  •  Next " .. safeText(context.nextName) end
    pane.summary:SetText(prefix)
    local message = report.notice
    if message == "" and report.unknown > 0 then
        message = report.unknown .. " quest states unknown across the scanned steps."
    end
    pane.notice:SetText(message)
    local pageFirst = (page - 1) * pageSize + 1
    selected = nil
    for _, row in ipairs(rows) do
        if row.key == selectionKey then selected = row; break end
    end
    selected = selected or rows[pageFirst]
    selectionKey = selected and selected.key
    for index, widget in ipairs(pane.rows) do
        local row = rows[pageFirst + index - 1]
        widget.row = row
        widget:SetShown(row ~= nil)
        if row then
            local label = row.current and "Now" or (row.index and "Step " .. row.index or "Planned")
            local color = (row.severity == "warning" or row.severity == "blocker") and "warning" or
                row.severity == "info" and "info" or "text"
            local state = categoryLabels[row.category] or row.status
            widget.label:SetText(label .. "  •  " .. safeText(row.name) .. "  #" .. row.id .. "  •  " .. state)
            widget.label:SetTextColor(unpack(colors[color]))
            widget.background:SetShown(row.key == selectionKey)
        end
    end
    pane.page:SetText(page .. " / " .. count)
    enable(pane.prev, page > 1)
    enable(pane.next, page < count)
    setDetail(detail(selected))
    enable(pane.dismiss, selected ~= nil and selected.code ~= nil)
    enable(pane.ignore, selected ~= nil and selected.classification == "Skippable")
    enable(pane.copySource, rowSource(selected) ~= nil)
    local _, _, _, mapId = position(selected)
    enable(pane.waypoint, mapId ~= nil and TomTom ~= nil and type(TomTom.AddWaypoint) == "function")
end

function QG.open(tab)
    if not pane then return end
    mode = tab or "report"
    pane:Show()
    if not QG.report then QG.refresh(nil, true) else QG.render(QG.report) end
end

function QG.toggle()
    if not pane then return end
    if pane:IsShown() then pane:Hide() else QG.open("report") end
end

function QG.toast(message, tone)
    if not toast then return end
    toast.text:SetText((message:gsub("|", "||")))
    toast.text:SetTextColor(unpack(colors[tone == "info" and "info" or "gold"]))
    toast.untilTime = (GetTime and GetTime() or 0) + 8
    toast:Show()
    toast:SetHeight(math.max(48, toast.text:GetStringHeight() + 25))
end

local function buildReport()
    reportBody = CreateFrame("Frame", nil, pane)
    reportBody:SetPoint("TOPLEFT", 18, -107)
    reportBody:SetPoint("BOTTOMRIGHT", -18, 31)
    pane.current = font(reportBody, "GameFontNormal", "text")
    pane.current:SetPoint("TOPLEFT")
    pane.current:SetWidth(459)
    pane.current:SetMaxLines(1)
    pane.summary = font(reportBody, "GameFontHighlightSmall", "gold")
    pane.summary:SetPoint("TOPLEFT", 0, -18)
    pane.summary:SetWidth(459)
    pane.summary:SetMaxLines(1)
    pane.notice = font(reportBody, "GameFontHighlightSmall", "muted")
    pane.notice:SetPoint("TOPLEFT", 0, -35)
    pane.notice:SetWidth(459)
    pane.notice:SetMaxLines(2)
    pane.rows = {}
    for index = 1, pageSize do
        local widget = CreateFrame("Button", nil, reportBody)
        widget:SetPoint("TOPLEFT", 0, -62 - (index - 1) * 19)
        widget:SetSize(459, 18)
        widget.background = widget:CreateTexture(nil, "BACKGROUND")
        widget.background:SetAllPoints()
        widget.background:SetColorTexture(0.33, 0.25, 0.15, 0.7)
        widget:SetHighlightTexture("Interface\\QuestFrame\\UI-QuestTitleHighlight", "ADD")
        widget.label = font(widget, "GameFontHighlightSmall", "text")
        widget.label:SetPoint("LEFT", 4, 0)
        widget.label:SetPoint("RIGHT", -4, 0)
        widget.label:SetMaxLines(1)
        widget:SetScript("OnClick", function(self)
            selectionKey = self.row and self.row.key
            QG.render(QG.report)
        end)
        pane.rows[index] = widget
    end
    pane.prev = button(reportBody, "‹", 30, function() page, selectionKey = page - 1, nil; QG.render(QG.report) end)
    pane.prev:SetPoint("TOPLEFT", 0, -164)
    pane.next = button(reportBody, "›", 30, function() page, selectionKey = page + 1, nil; QG.render(QG.report) end)
    pane.next:SetPoint("LEFT", pane.prev, "RIGHT", 46, 0)
    pane.page = font(reportBody, "GameFontHighlightSmall", "muted")
    pane.page:SetPoint("CENTER", pane.prev, "RIGHT", 23, 0)
    local line = section(reportBody)
    line:SetPoint("TOPLEFT", 0, -193)
    line:SetWidth(459)

    pane.detail = CreateFrame("ScrollFrame", nil, reportBody)
    pane.detail:SetPoint("TOPLEFT", 0, -202)
    pane.detail:SetPoint("BOTTOMRIGHT", 0, 32)
    pane.detail:EnableMouseWheel(true)
    pane.detail.content = CreateFrame("Frame", nil, pane.detail)
    pane.detail.content:SetSize(458, 110)
    pane.detail:SetScrollChild(pane.detail.content)
    pane.detail.text = font(pane.detail.content, "GameFontHighlightSmall", "text")
    pane.detail.text:SetPoint("TOPLEFT")
    pane.detail.text:SetWidth(450)
    pane.detail.text:SetSpacing(3)
    pane.detail:SetScript("OnMouseWheel", function(self, direction)
        local scroll = math.max(0, math.min(self:GetVerticalScrollRange(), self:GetVerticalScroll() - direction * 18))
        self:SetVerticalScroll(scroll)
    end)
    pane.waypoint = button(reportBody, "TomTom waypoint", 126, function() dropWaypoint(selected) end)
    pane.waypoint:SetPoint("BOTTOMLEFT")
    pane.dismiss = button(reportBody, "Dismiss", 82, function() QG.dismiss(selected) end)
    pane.dismiss:SetPoint("LEFT", pane.waypoint, "RIGHT", 5, 0)
    pane.ignore = button(reportBody, "Ignore optional", 113, function() QG.ignore(selected) end)
    pane.ignore:SetPoint("LEFT", pane.dismiss, "RIGHT", 5, 0)
    pane.copySource = button(reportBody, "Copy source", 111, function() copySource(rowSource(selected)) end)
    pane.copySource:SetPoint("LEFT", pane.ignore, "RIGHT", 5, 0)
end

local function buildSettings()
    settingsBody = CreateFrame("Frame", nil, pane)
    settingsBody:SetPoint("TOPLEFT", 18, -107)
    settingsBody:SetPoint("BOTTOMRIGHT", -18, 30)
    pane.settingsToggles = {}
    local function put(label, column, row, onClick)
        local widget = button(settingsBody, label, 220, onClick)
        widget:SetPoint("TOPLEFT", column * 231, -row * 29)
        return widget
    end
    local function toggle(key, label, column, row, changed)
        local widget = put(label, column, row, function()
            QG.settings[key] = not QG.settings[key]
            if changed then changed() end
            QG.render(QG.report)
        end)
        widget.label = label
        pane.settingsToggles[key] = widget
    end
    toggle("autoPreflight", "Automatic preflight", 0, 0, QG.autoSettingsChanged)
    toggle("scanZoneChange", "Scan on zone change", 0, 1)
    toggle("scanGuideChange", "Scan on guide/chapter change", 0, 2)
    toggle("scanUpcomingSteps", "Upcoming-step scan", 0, 3, QG.autoSettingsChanged)
    pane.lookahead = put("", 0, 4, function()
        local choices, selected = QG.lookaheadChoices, 1
        for index, value in ipairs(choices) do
            if value == QG.settings.lookahead then
                selected = index % #choices + 1
                break
            end
        end
        QG.settings.lookahead = choices[selected]
        QG.autoSettingsChanged()
        QG.render(QG.report)
    end)
    toggle("warningNotifications", "Warning notifications", 1, 0, function()
        if not QG.settings.warningNotifications and toast then toast:Hide() end
    end)
    toggle("suppressRepeated", "Suppress repeated warnings", 1, 1)
    pane.notify = put("", 1, 2, function()
        QG.settings.notify = QG.settings.notify == "chat" and "compact" or "chat"
        QG.render(QG.report)
    end)
    toggle("minimap", "Minimap button", 1, 3, setMinimap)
    pane.scope = put("", 1, 4, function()
        for index, key in ipairs(QG.scopeOrder) do
            if key == QG.settings.scope then
                QG.settings.scope = QG.scopeOrder[index % #QG.scopeOrder + 1]
                break
            end
        end
        QG.render(QG.report)
    end)
    put("Show ignored optional quests", 0, 5, function()
        QG.settings.ignored = {}
        QG.refresh(QG.report and QG.report.requestedScope, true)
    end)
    local guideAccess = put("", 1, 5, function()
        if QG.settings.authorizedGuides then
            QG.setGuideAccess(false)
        else
            QG.guideAccessHelp()
        end
    end)
    guideAccess.label = "Authorized guide analysis"
    pane.settingsToggles.authorizedGuides = guideAccess
    local info = font(settingsBody, "GameFontHighlight", "muted")
    info:SetPoint("TOPLEFT", 1, -192)
    info:SetWidth(456)
    info:SetSpacing(6)
    info:SetText("Use /qg guides for self-authored or licensed guide setup.\n" ..
        "/qg next previews the next chapter.\n/qg check <quest ID> reads the live quest state.\n/qg chapters lists imported chapters for manual planning.")
end

function QG.setupWindow()
    if pane then return end
    pane = CreateFrame("Frame", "RXPQuestGuardWindow", UIParent, backdrop)
    pane:SetSize(495, 475)
    pane:SetFrameStrata("DIALOG")
    pane:SetClampedToScreen(true)
    pane:SetMovable(true)
    pane:EnableMouse(true)
    pane:RegisterForDrag("LeftButton")
    local anchor = QG.settings.anchor
    if type(anchor) == "table" and type(anchor[1]) == "string" and type(anchor[2]) == "string" and
       type(anchor[3]) == "number" and type(anchor[4]) == "number" then
        pane:SetPoint(anchor[1], UIParent, anchor[2], anchor[3], anchor[4])
    else
        pane:SetPoint("CENTER", UIParent, "CENTER", 0, 12)
    end
    pane:SetScript("OnDragStart", pane.StartMoving)
    pane:SetScript("OnDragStop", function(self)
        self:StopMovingOrSizing()
        local a, _, b, x, y = self:GetPoint()
        QG.settings.anchor = {a, b, x, y}
    end)
    paint(pane)
    local close = CreateFrame("Button", nil, pane, "UIPanelCloseButton")
    close:SetPoint("TOPRIGHT", -2, -2)
    close:SetScript("OnClick", function() pane:Hide() end)
    local title = font(pane, "GameFontNormalLarge", "gold")
    title:SetPoint("TOPLEFT", 19, -16)
    title:SetText("RXP Quest Guard")
    pane.sub = font(pane, "GameFontHighlightSmall", "muted")
    pane.sub:SetPoint("TOPLEFT", 19, -40)
    pane.sub:SetWidth(442)
    pane.sub:SetMaxLines(2)
    pane.sub:SetSpacing(2)
    local report = button(pane, "Report", 90, function() QG.open("report") end)
    report:SetPoint("TOPLEFT", 17, -71)
    local recheck = button(pane, "Recheck", 90, function() QG.refresh(nil, true); QG.open("report") end)
    recheck:SetPoint("LEFT", report, "RIGHT", 5, 0)
    local settings = button(pane, "Settings", 90, function() QG.open("settings") end)
    settings:SetPoint("LEFT", recheck, "RIGHT", 5, 0)
    local debug = button(pane, "Diagnostics", 95, function() QG.open("debug") end)
    debug:SetPoint("LEFT", settings, "RIGHT", 5, 0)
    pane.attribution = CreateFrame("Button", nil, pane)
    pane.attribution:SetSize(286, 18)
    pane.attribution:SetPoint("BOTTOMLEFT", 18, 13)
    pane.attribution.label = font(pane.attribution, "GameFontHighlightSmall", "muted")
    pane.attribution.label:SetPoint("LEFT")
    sourceCopy = CreateFrame("EditBox", nil, pane, "InputBoxTemplate")
    sourceCopy:SetSize(165, 20)
    sourceCopy:SetPoint("BOTTOMRIGHT", -16, 11)
    sourceCopy:SetAutoFocus(false)
    sourceCopy:SetScript("OnEscapePressed", function(self) self:Hide() end)
    sourceCopy:SetScript("OnEnterPressed", function(self) self:Hide() end)
    sourceCopy:Hide()
    pane.attribution:SetScript("OnClick", function(self)
        copySource(publicUrl(self.url))
    end)
    pane.debug = font(pane, "GameFontHighlight", "text")
    pane.debug:SetPoint("TOPLEFT", 18, -109)
    pane.debug:SetWidth(459)
    pane.debug:SetSpacing(6)
    buildReport()
    buildSettings()
    if UISpecialFrames then table.insert(UISpecialFrames, "RXPQuestGuardWindow") end
    pane:Hide()

    toast = CreateFrame("Frame", nil, UIParent, backdrop)
    toast:SetWidth(395)
    toast:SetPoint("TOP", UIParent, "TOP", 0, -88)
    toast:SetFrameStrata("HIGH")
    toast:EnableMouse(false)
    paint(toast)
    toast.text = font(toast, "GameFontNormal", "gold")
    toast.text:SetPoint("TOPLEFT", 15, -12)
    toast.text:SetWidth(365)
    toast.text:SetJustifyH("CENTER")
    toast.text:SetSpacing(2)
    toast:SetScript("OnUpdate", function(self)
        if GetTime and GetTime() > self.untilTime then self:Hide() end
    end)
    toast:Hide()

    if Minimap then
        -- Minimap button collectors discover named children of the minimap.
        minimap = CreateFrame("Button", "RXPQuestGuardMinimapButton", Minimap)
        minimap:SetSize(30, 30)
        minimap:SetPoint("CENTER", Minimap, "CENTER", -52, -52)
        minimap:SetFrameLevel(Minimap:GetFrameLevel() + 5)
        minimap:SetNormalTexture("Interface\\Minimap\\MiniMap-TrackingBorder")
        minimap:SetHighlightTexture("Interface\\Minimap\\UI-Minimap-ZoomButton-Highlight", "ADD")
        local icon = minimap:CreateTexture(nil, "BACKGROUND")
        icon:SetPoint("CENTER", -1, 1)
        icon:SetSize(18, 18)
        icon:SetTexture("Interface\\Icons\\INV_Misc_Book_09")
        minimap:RegisterForClicks("LeftButtonUp")
        minimap:SetScript("OnClick", QG.toggle)
        if GameTooltip then
            minimap:SetScript("OnEnter", function(self)
                GameTooltip:SetOwner(self, "ANCHOR_LEFT")
                GameTooltip:SetText("RXP Quest Guard")
                GameTooltip:Show()
            end)
            minimap:SetScript("OnLeave", function() GameTooltip:Hide() end)
        end
        setMinimap()
    end
end
