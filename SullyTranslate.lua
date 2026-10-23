-- SullyTranslate for OBS: live translated subtitles from your microphone.
--
-- This script is the OBS side only: settings UI, the two caption text sources, and starting/stopping the
-- speech engine (sullytranslate.py, installed next to this file). The two talk through files in ./state:
-- we write settings.json + heartbeat, the engine writes caption.json, style.json, meta.json and alive.

obs = obslua
local ffi = require("ffi")

local APP = "SullyTranslate"
local SRC_TR = "SullyTranslate: translation"
local SRC_ORIG = "SullyTranslate: original"
local WINDOWS = ffi.os == "Windows"

local dir = nil            -- install folder (this script's folder), native separators, trailing separator
local state_dir = nil
local kind = nil           -- text source type: GDI+ on Windows, FreeType 2 elsewhere
local current = nil        -- this script's settings (obs_data_t, owned by OBS)
local last_caption, last_style = nil, nil
local shown_tr, shown_orig = nil, nil
local status = "engine not started yet"
local last_launch = 0
local sources_ready = false

if WINDOWS then
  ffi.cdef [[ int WinExec(const char *lpCmdLine, unsigned int uCmdShow); ]]
end

-- ------------------------------------------------------------------ files

local function read_file(path)
  local f = io.open(path, "rb")
  if not f then return nil end
  local s = f:read("*a")
  f:close()
  return s
end

local function write_file(path, s)
  local f = io.open(path, "wb")
  if not f then return false end
  f:write(s)
  f:close()
  return true
end

local function exists(path)
  local f = io.open(path, "rb")
  if f then f:close() end
  return f ~= nil
end

-- all settings, defaults included (obs_data_get_json alone skips values left at their default)
local function settings_json(settings)
  local full = obs.obs_data_get_defaults(settings)
  obs.obs_data_apply(full, settings)
  local font = obs.obs_data_get_obj(settings, "font")   -- object defaults aren't copied by get_defaults
  obs.obs_data_set_obj(full, "font", font)
  obs.obs_data_release(font)
  local json = obs.obs_data_get_json(full)
  obs.obs_data_release(full)
  return json
end

local function write_settings()
  if current == nil then return end
  write_file(state_dir .. "settings.json", '{"kind":"' .. kind .. '","settings":' .. settings_json(current) .. '}')
end

-- ------------------------------------------------------------------ engine

local function engine_python()
  if WINDOWS then return dir .. ".venv\\Scripts\\pythonw.exe" end
  return dir .. ".venv/bin/python"
end

local function launch_engine()
  last_launch = os.time()
  local py, engine = engine_python(), dir .. "sullytranslate.py"
  if not exists(py) or not exists(engine) then
    status = "engine not found (looked in " .. dir .. " and the standard install folders); run the installer"
    print(APP .. ": " .. status)
    return
  end
  local state = state_dir:sub(1, -2)
  if WINDOWS then
    -- pythonw has no console window; WinExec returns > 31 on success
    local cmd = '"' .. py .. '" "' .. engine .. '" --serve "' .. state .. '"'
    if ffi.C.WinExec(cmd, 0) <= 31 then
      status = "could not start the engine"
      print(APP .. ": " .. status .. ": " .. cmd)
    end
  else
    os.execute("setsid -f '" .. py .. "' '" .. engine .. "' --serve '" .. state .. "' </dev/null >/dev/null 2>&1")
  end
  print(APP .. ": engine started")
end

-- ------------------------------------------------------------------ sources

local function text_kind()
  local id = obs.obs_get_latest_input_type_id(WINDOWS and "text_gdiplus" or "text_ft2_source")
  if id == nil or id == "" then id = WINDOWS and "text_gdiplus_v3" or "text_ft2_source_v2" end
  return id
end

-- Create the caption sources if they don't exist yet (bottom centre of the canvas); with
-- `into_current_scene`, also add existing ones to the current scene if they aren't in it.
local function ensure_sources(into_current_scene)
  local scene_source = obs.obs_frontend_get_current_scene()
  if scene_source == nil then return false end      -- OBS is still loading
  local scene = obs.obs_scene_from_source(scene_source)
  local video = obs.obs_video_info()
  obs.obs_get_video_info(video)
  for i, name in ipairs({ SRC_TR, SRC_ORIG }) do
    local source = obs.obs_get_source_by_name(name)
    local item = nil
    if source == nil then
      local empty = obs.obs_data_create()
      source = obs.obs_source_create(kind, name, empty, nil)
      obs.obs_data_release(empty)
      item = obs.obs_scene_add(scene, source)
      last_style = nil                               -- style the new source
    elseif into_current_scene and obs.obs_scene_find_source(scene, name) == nil then
      item = obs.obs_scene_add(scene, source)
    end
    if item ~= nil then
      local pos = obs.vec2()
      pos.x = video.base_width / 2
      pos.y = video.base_height - 60 - (i == 2 and 140 or 0)   -- the original sits above the translation
      obs.obs_sceneitem_set_alignment(item, 8)                  -- bottom edge, horizontally centred
      obs.obs_sceneitem_set_pos(item, pos)
    end
    obs.obs_source_release(source)
  end
  obs.obs_source_release(scene_source)
  return true
end

local function update_source(name, data)
  local source = obs.obs_get_source_by_name(name)
  if source ~= nil then
    obs.obs_source_update(source, data)
    obs.obs_source_release(source)
  end
end

local function set_text(name, text)
  local data = obs.obs_data_create()
  obs.obs_data_set_string(data, "text", text)
  update_source(name, data)
  obs.obs_data_release(data)
end

local function apply_style(json)
  local style = obs.obs_data_create_from_json(json)
  if style == nil then return false end              -- half-written; try again next tick
  for name, key in pairs({ [SRC_TR] = "tr", [SRC_ORIG] = "orig" }) do
    local s = obs.obs_data_get_obj(style, key)
    if s ~= nil then
      update_source(name, s)
      obs.obs_data_release(s)
    end
  end
  obs.obs_data_release(style)
  return true
end

-- ------------------------------------------------------------------ timers

local function tick()
  if not sources_ready then
    sources_ready = ensure_sources(false)
    if not sources_ready then return end
  end
  local style = read_file(state_dir .. "style.json")
  if style ~= nil and style ~= last_style and apply_style(style) then
    last_style = style
  end
  local caption = read_file(state_dir .. "caption.json")
  if caption ~= nil and caption ~= last_caption then
    local data = obs.obs_data_create_from_json(caption)
    if data ~= nil then
      last_caption = caption
      local tr, orig = obs.obs_data_get_string(data, "tr"), obs.obs_data_get_string(data, "orig")
      if tr ~= shown_tr then set_text(SRC_TR, tr); shown_tr = tr end
      if orig ~= shown_orig then set_text(SRC_ORIG, orig); shown_orig = orig end
      local new_status = obs.obs_data_get_string(data, "status")
      if new_status ~= status then
        status = new_status
        print(APP .. ": " .. status)
      end
      obs.obs_data_release(data)
    end
  end
end

local function heartbeat()
  write_file(state_dir .. "heartbeat", tostring(os.time()))
  -- restart the engine if it died (it refreshes `alive` every 2 s, even while loading models)
  local alive = tonumber(read_file(state_dir .. "alive") or "")
  if (alive == nil or os.time() - alive > 20) and os.time() - last_launch > 30 then
    launch_engine()
  end
end

-- ------------------------------------------------------------------ settings UI

local function load_meta()
  local json = read_file(state_dir .. "meta.json")
  return json and obs.obs_data_create_from_json(json) or nil
end

local function each_item(data, key, fn)
  local arr = obs.obs_data_get_array(data, key)
  if arr == nil then return end
  for i = 0, obs.obs_data_array_count(arr) - 1 do
    local item = obs.obs_data_array_item(arr, i)
    fn(item)
    obs.obs_data_release(item)
  end
  obs.obs_data_array_release(arr)
end

local function on_restart(props, prop)
  obs.obs_data_set_int(current, "restart", obs.obs_data_get_int(current, "restart") + 1)
  write_settings()
  launch_engine()                                    -- if no engine is running, this one takes over
  return true
end

local function on_add_sources(props, prop)
  ensure_sources(true)
  last_style = nil
  return false
end

local function on_refresh(props, prop)
  return true
end

local function on_play_test(props, prop)
  obs.obs_data_set_int(current, "test", obs.obs_data_get_int(current, "test") + 1)
  write_settings()                                   -- the engine plays the file instead of the microphone
  status = "test requested; click Refresh status to follow it"
  return true
end

function script_description()
  return "<h3>" .. APP .. "</h3>Live translated subtitles from your microphone, shown in two text sources " ..
         "(<i>" .. SRC_TR .. "</i> and <i>" .. SRC_ORIG .. "</i>). Move and resize them in your scene like any " ..
         "source; style them here. Speech recognition and translation run locally on this PC."
end

function script_properties()
  local props = obs.obs_properties_create()
  obs.obs_properties_add_text(props, "status_info", "Status: " .. status, obs.OBS_TEXT_INFO)
  obs.obs_properties_add_button(props, "refresh", "Refresh status / microphones", on_refresh)
  local meta = load_meta()

  -- test: plays an audio file through recognition + translation instead of the microphone
  local tp = obs.obs_properties_create()
  obs.obs_properties_add_path(tp, "test_file", "Audio file (empty = built-in Slovak sample)", obs.OBS_PATH_FILE,
    "Audio files (*.wav *.mp3 *.ogg *.opus *.m4a *.flac);;All files (*.*)", nil)
  obs.obs_properties_add_button(tp, "play_test", "Play test file", on_play_test)
  obs.obs_properties_add_group(props, "test_group", "Test captions", obs.OBS_GROUP_NORMAL, tp)

  -- speech
  local sp = obs.obs_properties_create()
  local mic = obs.obs_properties_add_list(sp, "mic", "Microphone", obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING)
  obs.obs_property_list_add_string(mic, "Default microphone", "")
  if meta ~= nil then
    each_item(meta, "inputs", function(i)
      obs.obs_property_list_add_string(mic, obs.obs_data_get_string(i, "name"), obs.obs_data_get_string(i, "id"))
    end)
  end
  obs.obs_properties_add_text(sp, "langs_info",
    "Languages you speak (none ticked = detect any language). Speech in other languages is ignored.",
    obs.OBS_TEXT_INFO)
  if meta ~= nil then
    each_item(meta, "common", function(l)
      obs.obs_properties_add_bool(sp, "lang_" .. obs.obs_data_get_string(l, "code"), obs.obs_data_get_string(l, "name"))
    end)
  end
  obs.obs_properties_add_text(sp, "other_langs", "Other languages (names or codes, comma-separated)", obs.OBS_TEXT_DEFAULT)
  local target = obs.obs_properties_add_list(sp, "target", "Translate into", obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING)
  if meta ~= nil then
    each_item(meta, "targets", function(l)
      obs.obs_property_list_add_string(target, obs.obs_data_get_string(l, "name"), obs.obs_data_get_string(l, "code"))
    end)
  else
    obs.obs_property_list_add_string(target, "English", "en")
  end
  obs.obs_properties_add_text(sp, "hotwords", "Words to prefer (names, game terms, slang)", obs.OBS_TEXT_DEFAULT)
  local model = obs.obs_properties_add_list(sp, "model", "Speech model", obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING)
  obs.obs_property_list_add_string(model, "Auto (best for this PC)", "auto")
  obs.obs_property_list_add_string(model, "large-v3 (most accurate)", "large-v3")
  obs.obs_property_list_add_string(model, "large-v3-turbo (2x faster)", "large-v3-turbo")
  obs.obs_property_list_add_string(model, "medium", "medium")
  obs.obs_property_list_add_string(model, "small (fastest, least accurate)", "small")
  local mt = obs.obs_properties_add_list(sp, "mt", "Translation model", obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING)
  obs.obs_property_list_add_string(mt, "Auto: M2M-100 (commercial use OK)", "auto")
  obs.obs_property_list_add_string(mt, "M2M-100 1.2B (commercial use OK)", "m2m")
  obs.obs_property_list_add_string(mt, "M2M-100 418M, faster (commercial use OK)", "m2m-small")
  obs.obs_property_list_add_string(mt, "NLLB-200 3.3B, best quality (NON-COMMERCIAL use only)", "nllb")
  obs.obs_property_list_add_string(mt, "NLLB-200 1.3B (NON-COMMERCIAL use only)", "nllb-fast")
  local device = obs.obs_properties_add_list(sp, "device", "Run on", obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING)
  obs.obs_property_list_add_string(device, "Auto (NVIDIA GPU if present)", "auto")
  obs.obs_property_list_add_string(device, "NVIDIA GPU", "cuda")
  obs.obs_property_list_add_string(device, "CPU", "cpu")
  obs.obs_properties_add_group(props, "speech", "Speech", obs.OBS_GROUP_NORMAL, sp)

  -- captions
  local cp = obs.obs_properties_create()
  obs.obs_properties_add_float_slider(cp, "hold", "Hold finished line (seconds, 0 = until the next one)", 0, 30, 0.5)
  obs.obs_properties_add_bool(cp, "partials", "Show words live while speaking")
  obs.obs_properties_add_bool(cp, "show_original", "Also show what was said (original source)")
  obs.obs_properties_add_int(cp, "width", "Wrap width in pixels (0 = one line)", 0, 7680, 10)
  obs.obs_properties_add_group(props, "captions", "Captions", obs.OBS_GROUP_NORMAL, cp)

  -- style
  local st = obs.obs_properties_create()
  obs.obs_properties_add_font(st, "font", "Font")
  obs.obs_properties_add_color(st, "color", "Text color")
  obs.obs_properties_add_bool(st, "gradient", "Gradient")
  obs.obs_properties_add_color(st, "color2", "Gradient color")
  obs.obs_properties_add_bool(st, "outline", "Outline")
  if WINDOWS then
    obs.obs_properties_add_int(st, "outline_size", "Outline size", 1, 20, 1)
    obs.obs_properties_add_color(st, "outline_color", "Outline color")
    obs.obs_properties_add_bool(st, "bg", "Background box")
    obs.obs_properties_add_color(st, "bg_color", "Background color")
    obs.obs_properties_add_int_slider(st, "bg_opacity", "Background opacity", 0, 100, 1)
    local align = obs.obs_properties_add_list(st, "align", "Alignment", obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING)
    obs.obs_property_list_add_string(align, "Center", "center")
    obs.obs_property_list_add_string(align, "Left", "left")
    obs.obs_property_list_add_string(align, "Right", "right")
  else
    obs.obs_properties_add_bool(st, "shadow", "Drop shadow")
  end
  obs.obs_properties_add_int(st, "orig_size", "Original text size", 8, 300, 1)
  obs.obs_properties_add_color(st, "orig_color", "Original text color")
  obs.obs_properties_add_group(props, "style", "Style", obs.OBS_GROUP_NORMAL, st)

  obs.obs_properties_add_button(props, "add_sources", "Add caption sources to the current scene", on_add_sources)
  obs.obs_properties_add_button(props, "restart", "Restart engine", on_restart)
  if meta ~= nil then obs.obs_data_release(meta) end
  return props
end

function script_defaults(settings)
  local lang = (obs.obs_get_locale() or "en"):match("^(%a+)") or "en"
  obs.obs_data_set_default_string(settings, "mic", "")
  obs.obs_data_set_default_string(settings, "other_langs", "")
  obs.obs_data_set_default_string(settings, "target", lang:lower())
  obs.obs_data_set_default_string(settings, "hotwords", "")
  obs.obs_data_set_default_string(settings, "model", "auto")
  obs.obs_data_set_default_string(settings, "mt", "auto")
  obs.obs_data_set_default_string(settings, "device", "auto")
  obs.obs_data_set_default_int(settings, "restart", 0)
  obs.obs_data_set_default_double(settings, "hold", 4.0)
  obs.obs_data_set_default_bool(settings, "partials", true)
  obs.obs_data_set_default_bool(settings, "show_original", false)
  obs.obs_data_set_default_int(settings, "width", 1600)
  local font = obs.obs_data_create()
  obs.obs_data_set_string(font, "face", WINDOWS and "Arial" or "Sans Serif")
  obs.obs_data_set_string(font, "style", "Bold")
  obs.obs_data_set_int(font, "size", 48)
  obs.obs_data_set_int(font, "flags", 1)
  obs.obs_data_set_default_obj(settings, "font", font)
  obs.obs_data_release(font)
  obs.obs_data_set_default_int(settings, "color", 0xFFFFFFFF)
  obs.obs_data_set_default_bool(settings, "gradient", false)
  obs.obs_data_set_default_int(settings, "color2", 0xFF66D9FF)
  obs.obs_data_set_default_bool(settings, "outline", true)
  obs.obs_data_set_default_int(settings, "outline_size", 4)
  obs.obs_data_set_default_int(settings, "outline_color", 0xFF000000)
  obs.obs_data_set_default_bool(settings, "shadow", true)
  obs.obs_data_set_default_bool(settings, "bg", false)
  obs.obs_data_set_default_int(settings, "bg_color", 0xFF000000)
  obs.obs_data_set_default_int(settings, "bg_opacity", 60)
  obs.obs_data_set_default_string(settings, "align", "center")
  obs.obs_data_set_default_int(settings, "orig_size", 30)
  obs.obs_data_set_default_int(settings, "orig_color", 0xFFD0D0D0)
  obs.obs_data_set_default_int(settings, "test", 0)
  obs.obs_data_set_default_string(settings, "test_file", "")
end

function script_update(settings)
  current = settings
  write_settings()
end

-- The engine lives next to this script after a normal install. If the script was copied (or linked) into
-- OBS's own scripts folder instead, fall back to the installers' default locations.
local function find_install(script_dir)
  local candidates = { script_dir }
  if WINDOWS then
    local appdata = os.getenv("LOCALAPPDATA")
    if appdata then table.insert(candidates, appdata .. "\\SullyTranslate\\") end
    table.insert(candidates, "C:\\SullyTranslate\\")
  else
    local home = os.getenv("HOME") or ""
    table.insert(candidates, home .. "/.local/share/SullyTranslate/")
    table.insert(candidates, home .. "/SullyTranslate/")
  end
  for _, d in ipairs(candidates) do
    if exists(d .. "sullytranslate.py") then return d end
  end
  return script_dir
end

function script_load(settings)
  local sep = WINDOWS and "\\" or "/"
  local here = script_path():gsub("[/\\]", sep)
  if here:sub(-1) ~= sep then here = here .. sep end
  dir = find_install(here)
  state_dir = dir .. "state" .. sep
  kind = text_kind()
  current = settings
  write_settings()
  write_file(state_dir .. "heartbeat", tostring(os.time()))
  launch_engine()
  obs.timer_add(tick, 100)
  obs.timer_add(heartbeat, 1000)
end

function script_unload()
  obs.timer_remove(tick)
  obs.timer_remove(heartbeat)
  write_file(state_dir .. "heartbeat", "stop")
end
