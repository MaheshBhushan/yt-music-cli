"""The ytm command line.

Every command follows the same shape: parse, do one catalogue operation
and/or one mpv operation, print, exit. Local commands (pause, status,
volume, ...) touch only mpv over IPC and import nothing heavy; network
commands (search, play, lyrics, ...) import :mod:`ytm.music` lazily, so
``ytm pause`` never pays for ytmusicapi.

Output is plain lines by default and JSON with ``--json``. JSON bypasses
the formatting entirely, which is what makes the TUI a client of this CLI.
"""

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict

from ytm.paths import application_path

from ytm.player import (
    MPV_SITE,
    Player,
    PlayerError,
    find_mpv,
    install_succeeded,
    mpv_install_command,
)

_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

#: where the detached mpv writes its log
LOG_PATH = str(application_path("state", "mpv.log"))


class CliError(Exception):
    """A user-facing failure: printed to stderr, exit code 1."""


# -- wiring -----------------------------------------------------------------


def _ytdlp_path():
    """The yt-dlp next to this interpreter, else whatever is on PATH."""
    here = os.path.dirname(sys.executable)
    return shutil.which("yt-dlp", path=here) or shutil.which("yt-dlp")


AUTOPLAY_SCRIPT = os.path.join(os.path.dirname(__file__), "mpv", "autoplay.lua")


def _log_path():
    """LOG_PATH, with its directory made. mpv does not create it and does
    not complain when it cannot write there, so without this the log the
    README points at simply never appears -- and it is the only place a
    failed resolve or a dead audio device is ever reported."""
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    except OSError:
        pass
    return LOG_PATH


def player(spawn=True, **player_kwargs):
    """A connected Player, configured from config.toml and the stored auth."""
    from ytm import auth, config, js_runtime, volume

    cfg = config.load()
    pot = cfg["pot"]
    autoplay = cfg["behaviour"]["autoplay_radio"]
    mixer = volume.SystemVolume.detect() if cfg["audio"]["control"] == "system" else None
    if mixer is not None and player_kwargs.get("owner") is not None:
        mixer.owner = player_kwargs["owner"]
        mixer._run = mixer.owner.run
    return Player(
        spawn=spawn,
        mixer=mixer,
        **player_kwargs,
        # radio autoplay lives inside mpv: a Lua script asks `ytm radio` for
        # more when the last queued track starts (see ytm/mpv/autoplay.lua)
        scripts=[AUTOPLAY_SCRIPT] if autoplay else [],
        script_opts={
            "ytm_autoplay-python": sys.executable,
            "ytm_autoplay-limit": "10",
        } if autoplay else None,
        ytdlp_path=_ytdlp_path(),
        cookies_file=(
            auth.cookies_file() if cfg["behaviour"]["authenticated_streams"] else None
        ),
        extractor_args=(
            f"youtubepot-bgutilhttp:base_url={pot['base_url']}" if pot["enabled"] else None
        ),
        js_runtimes=js_runtime.find(),
        audio_device=cfg["audio"]["device"],
        extra_args=[
            # with a system mixer mpv's own volume stays at 100 (see
            # Player.volume); the configured level is for mpv-only control
            f"--volume={100 if mixer else cfg['audio']['volume']}",
            # mpv runs detached with no terminal, so this file is the only
            # place a failed resolve or a dead audio device is ever reported
            f"--log-file={_log_path()}",
            "--msg-level=all=warn,ytdl_hook=v",
        ],
    )


# -- selecting a track -------------------------------------------------------


def select(what, yt=None):
    """Turn the user's `what` into a Track.

    A small integer is an index into the last search, an 11-character id is
    a video id, and anything else is a search whose first hit wins.
    """
    from ytm import music, state

    if what.isdigit():
        results = state.last_search()
        index = int(what)
        if not 1 <= index <= len(results):
            raise CliError(
                f"no result {index}; the last search had {len(results)} results"
                if results
                else "no previous search to pick from; run 'ytm search' first"
            )
        return results[index - 1]
    if _VIDEO_ID.match(what):
        track = music.song(what, yt=yt)
        if track is None:
            raise CliError(f"no track with id {what}")
        return track
    results = music.search(what, limit=5, yt=yt)
    if not results:
        raise CliError(f"nothing found for '{what}'")
    # so "ytm play <query>" followed by "ytm add 2" picks from these hits
    state.remember_search(results)
    return results[0]


def current_track(p):
    """The Track mpv is on, from remembered metadata, or a stub from mpv."""
    from ytm import music, state

    for entry in p.playlist():
        if entry["current"]:
            known = state.track_for(entry["video_id"]) if entry["video_id"] else None
            if known:
                return known
            title = entry["title"] or entry["url"]
            return music.Track(entry["video_id"] or "", title, "", "", "", 0)
    return None


def _enqueue_all(p, tracks):
    """Append `tracks` to mpv's playlist in one batch."""
    from ytm import cache

    return p.enqueue_many((cache.playback_url(t.video_id), _label(t)) for t in tracks)


def _label(track):
    return f"{track.title} / {track.artist}" if track.artist else track.title


# -- formatting ---------------------------------------------------------------


def _clock(seconds):
    seconds = int(seconds or 0)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def fmt_track(track):
    return asdict(track)


def render_status(status, track):
    if status["idle"]:
        return f"Nothing playing (volume {int(status['volume'])})"
    lines = [track.title if track else status["title"] or "Unknown"]
    if track and track.artist:
        lines.append(track.artist)
    if track and track.album:
        lines.append(track.album)
    lines.append(f"{_clock(status['position'])} / {_clock(status['duration'])}")
    lines.append(
        f"{'Paused' if status['paused'] else 'Playing'}  "
        f"track {status['index'] + 1} of {status['count']}  "
        f"volume {int(status['volume'])}"
    )
    return "\n".join(lines)


def render_results(tracks):
    width = len(str(len(tracks)))
    return "\n".join(
        f"{i:>{width}}. {t.title} — {t.artist}" + (f"  ({t.duration})" if t.duration else "")
        for i, t in enumerate(tracks, 1)
    )


def render_queue(entries, tracks_by_id):
    if not entries:
        return "Queue is empty"
    width = len(str(len(entries)))
    lines = []
    for i, entry in enumerate(entries, 1):
        track = tracks_by_id.get(entry["video_id"])
        label = _label(track) if track else (entry["title"] or entry["url"])
        marker = "▶" if entry["current"] else " "
        lines.append(f"{marker} {i:>{width}}. {label}")
    return "\n".join(lines)


# -- commands ------------------------------------------------------------------
#
# Each returns (data, text): the JSON payload and the plain rendering.


def cmd_search(args):
    from ytm import music, state

    tracks = music.search(args.query, limit=args.limit)
    state.remember_search(tracks)
    if not tracks:
        return {"tracks": []}, f"nothing found for '{args.query}'"
    return {"tracks": [fmt_track(t) for t in tracks]}, render_results(tracks)


def _load(args, flags):
    from ytm import cache, state

    track = select(" ".join(args.what))
    state.remember_tracks([track])
    with player() as p:
        if flags == "play":
            p.play(cache.playback_url(track.video_id), title=_label(track))
        elif flags == "next":
            p.enqueue_next(cache.playback_url(track.video_id), title=_label(track))
        else:
            p.enqueue(cache.playback_url(track.video_id), title=_label(track))
    verb = {"play": "Playing", "next": "Up next"}.get(flags, "Queued")
    text = "\n".join([f"{verb}:", track.title, track.artist] + ([track.album] if track.album else []))
    return {"track": fmt_track(track), "action": flags}, text


def cmd_play(args):
    if not args.what:
        return cmd_resume(args)
    return _load(args, "play")


def cmd_add(args):
    return _load(args, "next" if args.next else "add")


def cmd_radio(args):
    from ytm import cache, music, state

    with player() as p:
        if args.what:
            seed = select(" ".join(args.what))
        else:
            seed = current_track(p)
            if seed is None:
                raise CliError("nothing is playing; give radio a song to start from")
        # ask for more than needed, then drop what is already queued: a
        # station keeps suggesting the same songs, and the queue must never
        # hold a track twice
        candidates = music.radio(seed.video_id, limit=args.limit * 2)
        if not candidates:
            raise CliError(f"no radio available for {seed.title}")
        if args.what:
            p.stop()
        queued = p.queued_ids() | {seed.video_id}
        tracks = []
        for track in candidates:
            if track.video_id not in queued:
                queued.add(track.video_id)
                tracks.append(track)
            if len(tracks) >= args.limit:
                break
        state.remember_tracks([seed] + tracks)
        if args.what:
            p.play(cache.playback_url(seed.video_id), title=_label(seed))
        _enqueue_all(p, tracks)
    return (
        {"seed": fmt_track(seed), "tracks": [fmt_track(t) for t in tracks]},
        f"Radio from {seed.title} — {seed.artist}: {len(tracks)} tracks queued",
    )


def cmd_mix(args):
    """List personal mixes, or replace the queue with one and play it."""
    from ytm import cache, music, state

    mixes = music.mixes()
    if not args.name:
        if not mixes:
            return {"mixes": []}, "no mixes available"
        return (
            {"mixes": [asdict(m) for m in mixes]},
            "\n".join(m.title for m in mixes),
        )
    match = args.name.lower()
    hits = [m for m in mixes if match in m.title.lower()]
    if not hits:
        raise CliError(f"no mix matching '{args.name}'")
    target = hits[0]
    playlist, tracks = music.get_playlist(target.playlist_id)
    if not tracks:
        raise CliError(f"{playlist.title} is empty")
    state.remember_tracks(tracks)
    with player() as p:
        p.stop()
        p.play(cache.playback_url(tracks[0].video_id), title=_label(tracks[0]))
        _enqueue_all(p, tracks[1:])
    return (
        {"playlist": asdict(playlist), "tracks": [fmt_track(t) for t in tracks]},
        f"Playing {playlist.title}: {len(tracks)} tracks queued",
    )


def _transport(method, text):
    def run(args):
        with player(spawn=False) as p:
            getattr(p, method)()
            status = p.status()
        return {"status": status}, text

    return run


cmd_pause = _transport("pause", "paused")
cmd_resume = _transport("resume", "resumed")
cmd_next = _transport("next", "next")
cmd_prev = _transport("prev", "previous")
cmd_stop = _transport("stop", "stopped")


def cmd_toggle(args):
    with player(spawn=False) as p:
        p.toggle()
        status = p.status()
    return {"status": status}, "paused" if status["paused"] else "resumed"


def cmd_seek(args):
    with player(spawn=False) as p:
        p.seek(args.seconds, absolute=args.to)
        status = p.status()
    return {"status": status}, _clock(status["position"])


def cmd_volume(args):
    with player(spawn=False) as p:
        level = p.volume(args.level)
    return {"volume": level}, f"volume {int(level)}"


def cmd_status(args):
    with player(spawn=False) as p:
        status = p.status()
        track = None if status["idle"] else current_track(p)
    data = dict(status, track=fmt_track(track) if track else None)
    return data, render_status(status, track)


def cmd_queue(args):
    from ytm import state

    with player(spawn=False) as p:
        entries = p.playlist()
    known = state.tracks_for(e["video_id"] for e in entries)
    data = [
        dict(entry, track=fmt_track(known[entry["video_id"]]) if entry["video_id"] in known else None)
        for entry in entries
    ]
    return {"queue": data}, render_queue(entries, known)


def cmd_clear(args):
    with player(spawn=False) as p:
        p.clear()
    return {"cleared": True}, "queue cleared (current track kept)"


def cmd_shuffle(args):
    with player(spawn=False) as p:
        p.shuffle()
    return {"shuffled": True}, "queue shuffled"


def cmd_lyrics(args):
    from ytm import music

    with player(spawn=False) as p:
        track = current_track(p)
    if track is None:
        raise CliError("nothing is playing")
    lyrics, source = music.get_lyrics(track.video_id)
    if not lyrics:
        return {"track": fmt_track(track), "lyrics": None, "source": None}, f"no lyrics for {track.title}"
    text = lyrics + (f"\n\n— {source}" if source else "")
    return {"track": fmt_track(track), "lyrics": lyrics, "source": source}, text


def cmd_like(args):
    from ytm import music

    with player(spawn=False) as p:
        track = current_track(p)
    if track is None:
        raise CliError("nothing is playing")
    music.like(track.video_id)
    return {"liked": fmt_track(track)}, f"liked {track.title} — {track.artist}"


def render_playlists(playlists):
    lines = []
    for playlist in playlists:
        counted = (
            f"{playlist.track_count} tracks"
            if playlist.track_count is not None
            else "track count unknown"
        )
        lines.append(f"{playlist.title} — {counted}")
    return "\n".join(lines)


def cmd_liked(args):
    from ytm import music

    tracks = music.liked_songs(limit=args.limit)
    if not tracks:
        return {"tracks": []}, "no liked songs"
    return {"tracks": [fmt_track(t) for t in tracks]}, render_results(tracks)


def cmd_library(args):
    from ytm import music

    tracks = music.library_songs(limit=args.limit)
    if not tracks:
        return {"tracks": []}, "your library has no songs"
    return {"tracks": [fmt_track(t) for t in tracks]}, render_results(tracks)


def cmd_playlists(args):
    from ytm import music, playlists_local

    if args.local:
        playlists = playlists_local.list_playlists()
        if not playlists:
            return {"playlists": []}, "no local playlists"
    else:
        playlists = music.library_playlists()
        if not playlists:
            return {"playlists": []}, "no playlists"
    return {"playlists": [asdict(p) for p in playlists]}, render_playlists(playlists)


def cmd_install_mpv(args):
    """Install mpv with whatever package manager this machine has.

    mpv is a C program and cannot come from PyPI (the `mpv` and `python-mpv`
    packages there are bindings to libmpv, not the player), so a fresh
    `pip install ytm` or `uv tool install ytm` leaves this one step to do.
    The command is printed before it runs, and it runs attached to this
    terminal so sudo and Homebrew can ask their own questions.
    """
    existing = find_mpv()
    if existing and not args.force:
        return {"installed": True, "path": existing, "ran": None}, f"mpv is already installed at {existing}"
    command = mpv_install_command()
    if command is None:
        raise CliError(
            f"no package manager ytm knows about was found. Install mpv from {MPV_SITE} "
            "(Homebrew: 'brew install mpv'; Debian/Ubuntu: 'sudo apt install mpv')."
        )
    printed = " ".join(command)
    if not args.yes:
        print(f"About to run: {printed}")
        try:
            answer = input("Continue? [Y/n] ").strip().lower()
        except EOFError:
            answer = "n"
        if answer not in ("", "y", "yes"):
            return {"installed": False, "ran": None}, "nothing installed"
    print(f"$ {printed}")
    try:
        # inherits this terminal: sudo prompts for a password, brew reports
        # its own progress, and neither works through a captured pipe
        code = subprocess.call(command)
    except OSError as exc:
        raise CliError(f"could not run {command[0]}: {exc}") from exc
    if not install_succeeded(command, code):
        raise CliError(f"{printed} failed (exit {code})")
    path = find_mpv()
    if path is None:
        raise CliError(
            f"{printed} reported success but mpv is still not on PATH. "
            "A package manager adds its directory to the PATH of terminals opened "
            "afterwards, so open a new one, or check where it put mpv."
        )
    return {"installed": True, "path": path, "ran": command}, f"mpv installed at {path}"


def cmd_quit(args):
    try:
        with player(spawn=False) as p:
            p.quit()
    except PlayerError:
        return {"stopped": False}, "mpv was not running"
    return {"stopped": True}, "mpv stopped"


def trace_auth(function):
    # Keep public/player commands free of authentication imports.
    from functools import wraps

    @wraps(function)
    def run(*args, **kwargs):
        from ytm.authentication.diagnostics import traced
        return traced(function)(*args, **kwargs)
    return run


@trace_auth
def cmd_auth(args):
    from ytm import auth

    if args.from_browser is not None:
        auth.import_from_browser(args.from_browser or None, profile=args.profile, authuser=args.authuser)
        auth.cookies_file()
        return {"saved": str(auth.SESSION_PATH)}, "Saved a verified browser session"

    record = auth.oauth_login(client_id=args.client_id, client_secret=args.client_secret,
                              client_file=args.client_file)
    _reset_account_clients()
    return _login_payload(record), "Login successful. Credentials stored locally."


def _confirm_account(name):
    """The first-release account check; --yes skips it, EOF is a no."""
    shown = name or "an unnamed account"
    try:
        print(f"Use this account: {shown}? [Y/n] ", end="", file=sys.stderr, flush=True)
        answer = input().strip().lower()
    except EOFError:
        return False
    return answer in ("", "y", "yes")


def _login_payload(record):
    return {"authenticated": True, "method": record.method, "status": "valid"}


def _reset_account_clients():
    """Retire a client built from the credentials this login replaced.

    One-shot CLI runs would notice the new revision anyway; a long-lived
    process (or a test) must not keep serving the old account.
    """
    from ytm import music

    music.reset_client()


def _login_interactive(args):
    from ytm import auth
    from ytm.authentication import browser_login

    print("Opening YouTube Music login...", file=sys.stderr)
    print("Sign in using the browser window.", file=sys.stderr)
    print("Waiting for YouTube Music authentication...", file=sys.stderr)

    def confirm(verified):
        print("YouTube Music account verified.", file=sys.stderr)
        if args.yes:
            return True
        return _confirm_account(verified.account_name)

    if args.method == "playwright":
        browser = browser_login.PlaywrightBrowser(channel=args.browser)
    else:
        from ytm.authentication.browser_profiles import NativeBrowser
        browser = NativeBrowser(browser=args.browser, profile=args.profile, authuser=args.authuser)
    record = browser_login.interactive_login(
        auth.auth_manager(), browser=browser, timeout=args.timeout, confirm=confirm
    )
    _reset_account_clients()
    return _login_payload(record), "Login successful. Credentials stored locally."


def _login_from_browser(args):
    from ytm import auth

    print("Importing the browser's YouTube Music session...", file=sys.stderr)

    def confirm(verified):
        if args.yes:
            return True
        return _confirm_account(verified.account_name)

    record = auth.import_from_browser(
        args.from_browser or None,
        profile=args.profile,
        authuser=args.authuser,
        confirm=confirm,
    )
    _reset_account_clients()
    return _login_payload(record), "Login successful. Credentials stored locally."


def _install_login_browser(args):
    if importlib.util.find_spec("playwright") is None:
        raise CliError(
            "The Playwright package is not installed. Install it with "
            "'pip install \"ytm[login]\"' (or 'pip install playwright'), then run "
            "'ytm login --install-browser' again."
        )
    browser = {"edge": "msedge"}.get(args.browser, args.browser) or "chromium"
    command = [sys.executable, "-m", "playwright", "install", browser]
    print(f"$ {' '.join(command)}", file=sys.stderr)
    try:
        code = subprocess.call(command)
    except OSError as exc:
        raise CliError(f"could not run {command[0]}: {exc}") from exc
    if code != 0:
        raise CliError(f"browser installation failed (exit {code})")
    return {"installed": True, "browser": browser}, f"{browser} installed for Playwright"


_METHOD_LABELS = {"browser": "Browser session", "oauth": "OAuth"}


def cmd_account(args):
    """Report the signed-in account: local facts first, validation second."""
    from ytm import auth

    status = auth.auth_manager().status(validate=not args.no_check)
    data = {
        "logged_in": status.logged_in,
        "method": status.method,
        "session_status": status.state,
    }
    if status.account_name:
        data["account"] = status.account_name
    method = _METHOD_LABELS.get(status.method, "None")
    if status.state == "valid":
        lines = ["Logged in: Yes", f"Authentication: {method}", "Session status: Valid"]
        if status.account_name:
            lines.append(f"Account: {status.account_name}")
        return data, "\n".join(lines)
    if status.state == "not_checked":
        stored = "Yes" if status.logged_in else "No"
        return data, f"Credentials stored: {stored}\nSession status: Not checked"
    if status.state == "unknown":
        return data, "Credentials stored: Yes\nSession status: Unknown (could not verify)"
    if status.state == "expired":
        return data, (
            f"Logged in: No\nAuthentication: {method}\nSession status: Expired\n"
            "Run `ytm login` to sign in."
        )
    if status.state == "invalid":
        return data, (
            "Credentials stored: Yes\nSession status: Invalid\n"
            "Run `ytm login` to replace them."
        )
    return data, "Logged in: No\nRun `ytm login` to sign in."


def cmd_logout(args):
    """Sign out on this computer: local tombstone plus file cleanup."""
    from ytm import auth

    result = auth.auth_manager().logout()
    _reset_account_clients()
    data = {
        "signed_out": True,
        "cleaned": list(result.removed),
        "cleanup_failures": [{"path": path, "reason": reason} for path, reason in result.failed],
    }
    if result.failed:
        details = "\n".join(f"could not remove {path}: {reason}" for path, reason in result.failed)
        raise CliError(
            "Signed out, but some credential files could not be removed and could "
            f"resurface if the session record is deleted:\n{details}"
        )
    return data, "Signed out of YTM on this computer."


@trace_auth
def cmd_login(args):
    """Sign in: interactive browser by default, or an explicit alternative."""
    from ytm import auth

    if args.install_browser:
        return _install_login_browser(args)
    if args.from_browser is not None:
        return _login_from_browser(args)
    if args.method == "oauth":
        record = auth.oauth_login(
            client_id=args.client_id,
            client_secret=args.client_secret,
            client_file=args.client_file,
        )
        _reset_account_clients()
        return _login_payload(record), "Login successful. Credentials stored locally."
    return _login_interactive(args)


def _login_usage_error(args):
    """Reject inconsistent browser modes before touching profiles or credentials."""
    from ytm.authentication.browser_profiles import NATIVE_BROWSERS

    managed = ("chromium", "chrome", "chrome-beta", "chrome-dev", "chrome-canary",
               "edge", "msedge", "msedge-beta", "msedge-dev", "msedge-canary", "firefox", "webkit")
    import math
    if args.timeout is not None and (not math.isfinite(args.timeout) or args.timeout <= 0):
        return "--timeout must be a positive number of seconds"
    if args.install_browser:
        if args.from_browser is not None or args.profile or args.authuser or args.method in ("browser", "oauth") or args.client_file or args.client_id or args.client_secret:
            return "--install-browser only installs a Playwright browser; do not combine it with a login method or profile"
        if args.browser and args.browser not in managed:
            return "unsupported Playwright browser; choose chromium, chrome, edge, firefox or webkit"
        return None
    if args.from_browser is not None and args.method in ("oauth", "playwright"):
        return "--from-browser cannot be combined with --method oauth or playwright"
    if args.browser and (args.from_browser is not None or args.method == "oauth"):
        return "--browser cannot be combined with --from-browser or --method oauth"
    if (args.profile is not None or args.authuser is not None) and args.method in ("oauth", "playwright"):
        return "--profile/--authuser select an existing browser session; omit them for OAuth or Playwright"
    if (args.client_file or args.client_id or args.client_secret) and args.method != "oauth":
        return "--client-file/--client-id/--client-secret require --method oauth"
    choices = managed if args.method == "playwright" else (*NATIVE_BROWSERS, "default", "msedge")
    if args.browser and args.browser not in choices:
        return "unsupported browser for this method; use --method playwright for isolated Firefox/WebKit/Chromium"
    if args.authuser is not None and (not args.authuser.isascii() or not args.authuser.isdecimal()):
        return "--authuser must be a nonnegative numeric account index"
    return None


def cmd_cache(args):
    from ytm import cache

    if args.cache_command == "add":
        path = cache.download(args.video_id)
        return {"cached": args.video_id, "path": str(path)}, f"cached {args.video_id} -> {path}"
    if args.cache_command == "rm":
        removed = cache.remove(args.video_id)
        if not removed:
            raise CliError(f"not cached: {args.video_id}")
        return {"removed": args.video_id}, f"removed {args.video_id}"
    entries = cache.list_cached()
    return {"cached": entries}, "\n".join(
        f"{e['video_id']}\t{e['size']}\t{e['path']}" for e in entries
    ) or "cache is empty"


def cmd_version(args):
    """The running ytm version, plus what the last PyPI check knew (no network)."""
    from ytm import update

    installed = update.installed_version()
    info = update.check(fetch=lambda: None)  # cached result only; `ytm update --check` asks PyPI
    latest = info["latest"]
    line = f"ytm {installed}"
    if info["newer"]:
        line += f" ({latest} is available: ytm update)"
    return {"version": installed, "latest": latest, "newer": info["newer"]}, line


def cmd_update(args):
    """Upgrade ytm and yt-dlp in place, or just report what is available."""
    from ytm import update

    info = update.check(force=True)
    if info["latest"] is None:
        line = f"ytm {info['installed']} installed; could not reach PyPI to check for updates"
    elif info["newer"]:
        line = f"ytm {info['installed']} installed; {info['latest']} is available"
    else:
        line = f"ytm {info['installed']} is up to date"
    if args.check:
        return info, line
    if not info["newer"] and not args.force:
        return dict(info, upgraded=False), line + " (use --force to reinstall and refresh yt-dlp)"
    kind = update.install_kind()
    if sys.platform == "win32" and kind != "editable" and not args.json and sys.stdin.isatty():
        try:
            answer = input(
                f"{line}\nytm must exit before Windows can replace its launcher.\n"
                "Close other ytm instances. Exit now and update in a new PowerShell window? [Y/n] "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer not in ("", "y", "yes"):
            _, manual = update.upgrade(kind=kind, target=info["latest"] if info["newer"] else None)
            return dict(info, upgraded=False, pending=False), f"Update cancelled.\n{manual}"
        ok, text = update.start_windows_upgrade(kind=kind, target=info["latest"] if info["newer"] else None)
        if not ok:
            raise CliError(text)
        return dict(info, upgraded=False, pending=True, kind=kind), text
    ok, text = update.upgrade(kind=kind, target=info["latest"] if info["newer"] else None)
    if not ok:
        raise CliError(text)
    return (
        dict(info, upgraded=True, kind=kind, output=text),
        f"{line}\nupgraded via {kind}; restart ytm to run the new version",
    )


def cmd_tui(args):
    """The Textual TUI, still on the old daemon until the new one lands."""
    from ytm.tui.app import run as run_tui

    run_tui()
    return None, None


# -- parser ---------------------------------------------------------------------


def cmd_migrate_paths(args):
    from ytm.paths import MigrationError, migrate
    if sys.platform != "win32":
        raise CliError("Path migration is for Windows installations only.")
    try:
        items = migrate(apply=args.apply)
    except MigrationError as exc:
        raise CliError(str(exc)) from exc
    except OSError as exc:
        raise CliError("Could not access migration files. Originals kept; close YTM and check directory permissions.") from exc
    action = "Migration completed; restart YTM. Legacy originals kept." if args.apply else "Preview only. Close other YTM instances, then use --apply to copy."
    return {"applied": args.apply, "files": items}, action + "\n" + "\n".join(
        f"{i['status']}: {i['source']} -> {i['target']}" for i in items
    )


def build_parser():
    parser = argparse.ArgumentParser(prog="ytm", description="YouTube Music from the terminal")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    from ytm.update import installed_version

    parser.add_argument("--version", action="version", version=f"ytm {installed_version()}")
    sub = parser.add_subparsers(dest="command", metavar="command")

    def add(name, func, help, **kwargs):
        p = sub.add_parser(name, help=help, **kwargs)
        p.set_defaults(func=func)
        return p

    p = add("migrate-paths", cmd_migrate_paths, "preview Windows data migration (credentials stay managed by login)")
    p.add_argument("--apply", action="store_true", help="copy to application data; retain originals, refuse conflicts")

    p = add("search", cmd_search, "search songs")
    p.add_argument("query")
    p.add_argument("-n", "--limit", type=int, default=10)

    p = add("liked", cmd_liked, "list your liked songs")
    p.add_argument("-n", "--limit", type=int, default=25)

    p = add("library", cmd_library, "list songs saved in your library")
    p.add_argument("-n", "--limit", type=int, default=25)

    p = add("playlists", cmd_playlists, "list your playlists (--local: the ones stored on this computer)")
    p.add_argument("--local", action="store_true", help="list local playlists only; no account needed")

    p = add("play", cmd_play, "play a song: a query, a result number, or a video id")
    p.add_argument("what", nargs="*")
    p = add("add", cmd_add, "queue a song at the end (or right after the current one)")
    p.add_argument("what", nargs="+")
    p.add_argument("--next", action="store_true", help="play it next instead of last")
    p = add("radio", cmd_radio, "queue a radio from a song (default: the one playing)")
    p.add_argument("what", nargs="*")
    p.add_argument("-n", "--limit", type=int, default=25)

    p = add("mix", cmd_mix, "list personal mixes, or play one by name")
    p.add_argument("name", nargs="?")

    add("pause", cmd_pause, "pause")
    add("resume", cmd_resume, "resume")
    add("toggle", cmd_toggle, "toggle pause")
    add("next", cmd_next, "next track")
    add("prev", cmd_prev, "previous track")
    add("stop", cmd_stop, "stop and empty the queue")
    p = add("seek", cmd_seek, "seek by seconds (negative to go back)")
    p.add_argument("seconds", type=float)
    p.add_argument("--to", action="store_true", help="seek to an absolute position")
    p = add("volume", cmd_volume, "show or set the volume (0-100)")
    p.add_argument("level", type=float, nargs="?")
    add("status", cmd_status, "what is playing")
    add("queue", cmd_queue, "list the queue")
    add("clear", cmd_clear, "clear the queue, keeping the current track")
    add("shuffle", cmd_shuffle, "shuffle the queue")
    add("lyrics", cmd_lyrics, "lyrics for the current track")
    add("like", cmd_like, "like the current track")
    add("quit", cmd_quit, "stop mpv entirely")

    p = add("install-mpv", cmd_install_mpv, "install mpv, which pip cannot")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask before running it")
    p.add_argument("--force", action="store_true", help="run it even if mpv is already on PATH")

    p = add("login", cmd_login, "sign in to YouTube Music (browser session by default)")
    p.add_argument(
        "--timeout", type=float, default=600, metavar="SECONDS",
        help="how long to wait for the browser sign-in (default: 600)",
    )
    p.add_argument(
        "--browser", default=None, metavar="CHANNEL",
        help='browser to open, e.g. chrome, edge, firefox, brave (default: system HTTPS browser)',
    )
    p.add_argument(
        "--from-browser", nargs="?", const="", default=None, metavar="BROWSER",
        help="import cookies from a logged-in browser instead: auto-detect, or chrome, chromium, "
             "edge, brave, vivaldi, opera, helium, firefox",
    )
    p.add_argument(
        "--profile", default=None, metavar="NAME",
        help='browser profile directory to read, e.g. "Default" or "Profile 1" (normal browser or --from-browser)',
    )
    p.add_argument(
        "--authuser", default=None, metavar="N",
        help="Google account index when the browser is signed in to several (normal browser or --from-browser)",
    )
    p.add_argument(
        "--method", choices=("browser", "playwright", "oauth"), default=None,
        help="browser: normal profile (default); playwright: isolated browser; oauth: advanced fallback",
    )
    p.add_argument("--client-file", default=None, help="Google 'Desktop app' OAuth client JSON (only with --method oauth)")
    p.add_argument("--client-id", default=None, help="OAuth client id (only with --method oauth)")
    p.add_argument("--client-secret", default=None, help="OAuth client secret (only with --method oauth)")
    p.add_argument(
        "--install-browser", action="store_true",
        help="install the browser binary Playwright drives, then exit",
    )
    p.add_argument("-y", "--yes", action="store_true", help="skip account confirmation (normal browser still waits for you to finish signing in)")

    add("logout", cmd_logout, "sign out on this computer (local only; never contacts Google)")
    p = add("account", cmd_account, "show the signed-in account and session status")
    p.add_argument(
        "--no-check", action="store_true",
        help="report stored credentials without asking YouTube Music whether they still work",
    )

    p = add("auth", cmd_auth, "sign in with Google (or --from-browser to import a browser's cookies)")
    p.add_argument(
        "--from-browser", nargs="?", const="", default=None, metavar="BROWSER",
        help="import cookies from a logged-in browser instead: auto-detect, or chrome, chromium, "
             "edge, brave, vivaldi, opera, helium, firefox",
    )
    p.add_argument(
        "--profile", default=None, metavar="NAME",
        help='browser profile directory to read, e.g. "Default" or "Profile 1" (default: the one with a login)',
    )
    p.add_argument(
        "--authuser", default=None, metavar="N",
        help="Google account index when the browser is signed in to several (default: auth.x-goog-authuser in config.toml, 0)",
    )
    p.add_argument("--client-file", default=None, help="Google 'Desktop app' OAuth client JSON; sign in via a browser on this machine")
    p.add_argument("--client-id", default=None)
    p.add_argument("--client-secret", default=None)

    p = add("cache", cmd_cache, "offline cache")
    cache_sub = p.add_subparsers(dest="cache_command")
    cache_sub.add_parser("add").add_argument("video_id")
    cache_sub.add_parser("rm").add_argument("video_id")
    cache_sub.add_parser("list")
    p.set_defaults(cache_command="list")

    add("version", cmd_version, "print the ytm version")
    p = add("update", cmd_update, "upgrade ytm and yt-dlp to the latest release")
    p.add_argument("--check", action="store_true", help="only report whether a newer version exists")
    p.add_argument("--force", action="store_true", help="reinstall even when already current")

    add("tui", cmd_tui, "open the full-screen interface")
    return parser


def main(argv=None, out=sys.stdout, err=sys.stderr):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "login":
        problem = _login_usage_error(args)
        if problem:
            parser.error(problem)
    if args.command is None:
        # plain `ytm` is the same command as `ytm tui`; route it through the
        # shared handler so startup failures and Ctrl-C behave identically
        args.func = cmd_tui
    try:
        data, text = args.func(args)
    except KeyboardInterrupt:
        print("cancelled", file=err)
        return 130
    except (CliError, PlayerError) as exc:
        print(exc, file=err)
        return 1
    except Exception as exc:  # auth and network failures included
        import requests
        from ytmusicapi.exceptions import YTMusicError

        from ytm import auth

        if isinstance(exc, auth.AuthError):
            print(exc, file=err)
            return 1
        if isinstance(exc, requests.exceptions.RequestException):
            # no route to YouTube is a fact to report, not a stack trace
            print("could not reach YouTube Music; check your connection and try again.", file=err)
            return 1
        from ytm import music

        if isinstance(exc, music.ProviderError):
            print(exc, file=err)
            return 1
        from ytm import playlists_local

        if isinstance(exc, playlists_local.PlaylistStorageError):
            # local storage refused the change and kept the original file
            print(exc, file=err)
            return 1
        if isinstance(exc, YTMusicError):
            # the raw message can carry a whole response body; keep it out
            print(music.provider_message(exc), file=err)
            return 1
        raise
    if data is None:
        return 0
    if args.json:
        json.dump(data, out)
        out.write("\n")
    elif text:
        print(text, file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
