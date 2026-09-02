-- ytm autoplay: keep the music going when the queue runs out.
--
-- Loaded into the persistent mpv by ytm (see ytm.cli.player). Whenever the
-- *last* playlist entry starts playing, it asks `ytm radio` to append
-- YouTube Music's radio for that track, so there is always something after
-- the current song. Everything else -- resolving, advancing, playing -- is
-- mpv's own; this script only runs one subprocess at the right moment.
--
-- Options (script-opts, prefix `ytm_autoplay-`):
--   enabled  yes/no   (default yes)
--   python   path to the interpreter that has ytm installed (default python3)
--   limit    how many radio tracks to append each time (default 10)

local msg = require("mp.msg")
local options = require("mp.options")

local opts = {
    enabled = true,
    python = "python3",
    limit = 10,
}
options.read_options(opts, "ytm_autoplay")

-- the entry we last extended from; guards against asking twice for the
-- same track (file-loaded fires again after a seek past EOF, for instance)
local extended_from = nil
local in_flight = false

local function on_file_loaded()
    if not opts.enabled or in_flight then
        return
    end
    local pos = mp.get_property_number("playlist-pos", -1)
    local count = mp.get_property_number("playlist-count", 0)
    if pos < 0 or pos < count - 1 then
        return -- something is already queued after this track
    end
    local path = mp.get_property("path")
    if path == nil or path == extended_from then
        return
    end
    extended_from = path
    in_flight = true
    msg.info("last queued track started; fetching radio to follow it")
    mp.command_native_async({
        name = "subprocess",
        args = { opts.python, "-m", "ytm.cli", "--json", "radio", "-n", tostring(opts.limit) },
        playback_only = false,
        capture_stdout = true,
        capture_stderr = true,
    }, function(success, result, error)
        in_flight = false
        if not success or result == nil or result.status ~= 0 then
            local detail = error or (result and result.stderr) or "unknown error"
            msg.warn("could not fetch radio: " .. tostring(detail))
            -- allow a retry from this same track if the user comes back to it
            extended_from = nil
            return
        end
        msg.info("radio appended: " .. tostring(mp.get_property_number("playlist-count", 0)) .. " entries queued")
    end)
end

mp.register_event("file-loaded", on_file_loaded)
