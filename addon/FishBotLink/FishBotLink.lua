-- FishBotLink: draws a strip of solid-colour squares at the very top-left of
-- the game view. Each square is exactly PX physical pixels so the bot can read
-- them from a screenshot regardless of UI scale.
--
--   cell 1  magenta  marker ("addon is here")
--   cell 2  green/red  mouse is over a Fishing Bobber
--   cell 3  green/red  player is channeling Fishing
--   cell 4  green/red  a loot window is open
--   cell 5  green/red  player is in combat
--   cell 6  green/red  player is flagged AFK
--
-- Addons cannot write files while you play, and cannot move the mouse or
-- click, so this is a one-way status display: the bot still does the input.

local PX = 16
local CELLS = 6

local root = CreateFrame("Frame", "FishBotLinkFrame", UIParent)
root:SetFrameStrata("TOOLTIP")
root:SetFrameLevel(10000)
root:SetPoint("TOPLEFT", UIParent, "TOPLEFT", 0, 0)

local cells = {}
for i = 1, CELLS do
    local t = root:CreateTexture(nil, "OVERLAY")
    t:SetColorTexture(1, 0, 0, 1)
    cells[i] = t
end

-- Physical height of the game view in pixels. Legion 7.3.5 has
-- GetPhysicalScreenSize; older or odd clients fall back to the resolution cvar.
local function physicalHeight()
    if GetPhysicalScreenSize then
        local _, h = GetPhysicalScreenSize()
        if h and h > 0 then return h end
    end
    for _, cvar in ipairs({ "gxWindowedResolution", "gxFullscreenResolution", "gxResolution" }) do
        local v = GetCVar and GetCVar(cvar)
        local h = v and tonumber(v:match("x(%d+)"))
        if h and h > 0 then return h end
    end
    return 1080
end

-- One UI unit is (768 / physical height) / effectiveScale physical pixels.
local function unitsPerPixel()
    return 768 / physicalHeight() / UIParent:GetEffectiveScale()
end

local function layout()
    local s = PX * unitsPerPixel()
    root:SetSize(CELLS * s, s)
    for i, t in ipairs(cells) do
        t:ClearAllPoints()
        t:SetPoint("TOPLEFT", root, "TOPLEFT", (i - 1) * s, 0)
        t:SetSize(s, s)
    end
    cells[1]:SetColorTexture(1, 0, 1, 1)
end

local function set(i, on)
    if on then
        cells[i]:SetColorTexture(0, 1, 0, 1)
    else
        cells[i]:SetColorTexture(1, 0, 0, 1)
    end
end

-- Localised name of the Fishing spell, so "channeling" means fishing and not
-- some other channel. 131474 is Fishing from MoP onward (so 7.3.5), 7620 is
-- the original rank. GetSpellInfo is the 7.x API; C_Spell is for new clients.
local fishingName
do
    if GetSpellInfo then
        fishingName = GetSpellInfo(131474) or GetSpellInfo(7620)
    end
    if not fishingName and C_Spell and C_Spell.GetSpellInfo then
        local info = C_Spell.GetSpellInfo(131474) or C_Spell.GetSpellInfo(7620)
        fishingName = info and info.name
    end
end

local BOBBER_NAMES = {
    ["Fishing Bobber"] = true,
    ["Boya de pesca"] = true,       -- esES / esMX
    ["Angelschwimmer"] = true,      -- deDE
    ["Flotteur de pêche"] = true,   -- frFR
    ["Boia de pesca"] = true,       -- ptBR
    ["Galleggiante da pesca"] = true, -- itIT
    ["Поплавок"] = true,            -- ruRU
}

local function isChanneling()
    local name = UnitChannelInfo("player")
    if not name then return false end
    if fishingName then return name == fishingName end
    return true
end

local function tooltipIsBobber(channeling)
    if not GameTooltip:IsShown() then return false end
    local line = _G["GameTooltipTextLeft1"]
    local text = line and line:GetText()
    if not text then return false end
    if BOBBER_NAMES[text] then return true end
    -- Unknown locale: a world-object tooltip with a single line while we are
    -- fishing is, in practice, the bobber.
    return channeling and GameTooltip:NumLines() == 1 and GameTooltip:GetOwner() == UIParent
end

-- With auto-loot on, the loot window opens and closes within one frame, so
-- the loot cell is held green for a moment after LOOT_OPENED.
local lootOpen, lootUntil, inCombat = false, 0, false
-- Every frame: the bot sweeps the cursor quickly and reads the bobber cell
-- right after each move, so the cell must not lag behind the tooltip.
root:SetScript("OnUpdate", function()
    local ch = isChanneling()
    set(2, tooltipIsBobber(ch))
    set(3, ch)
    set(4, lootOpen or GetTime() < lootUntil)
    set(5, inCombat)
    set(6, UnitIsAFK("player") and true or false)
end)

root:RegisterEvent("PLAYER_LOGIN")
root:RegisterEvent("LOOT_OPENED")
root:RegisterEvent("LOOT_CLOSED")
root:RegisterEvent("PLAYER_REGEN_DISABLED")
root:RegisterEvent("PLAYER_REGEN_ENABLED")
root:RegisterEvent("UI_SCALE_CHANGED")
root:RegisterEvent("DISPLAY_SIZE_CHANGED")
root:SetScript("OnEvent", function(_, event)
    if event == "LOOT_OPENED" then
        lootOpen = true
        lootUntil = GetTime() + 1.5
    elseif event == "LOOT_CLOSED" then
        lootOpen = false
    elseif event == "PLAYER_REGEN_DISABLED" then
        inCombat = true
    elseif event == "PLAYER_REGEN_ENABLED" then
        inCombat = false
    elseif event == "PLAYER_LOGIN" then
        layout()
        print("|cff00ff00FishBotLink|r loaded. Status pixels are in the top-left corner.")
    else
        layout()
    end
end)

layout()

-- /fbl toggles the strip. Hidden means the bot sees no marker and falls back
-- to the model, so show it again before fishing.
SLASH_FISHBOTLINK1 = "/fbl"
SLASH_FISHBOTLINK2 = "/fishbotlink"
SlashCmdList["FISHBOTLINK"] = function(msg)
    msg = (msg or ""):lower()
    if msg == "show" or (msg == "" and not root:IsShown()) then
        root:Show()
        print("|cff00ff00FishBotLink|r status pixels shown (the bot needs them visible).")
    else
        root:Hide()
        print("|cff00ff00FishBotLink|r status pixels hidden. Type /fbl before fishing to show them again.")
    end
end
