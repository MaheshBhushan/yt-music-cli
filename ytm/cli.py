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
import json
from dataclasses import asdict
import os
import re
import shutil
import sys

from ytm.player import Player, PlayerError, watch_url

_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

#: where the detached mpv writes its log
LOG_PATH = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "ytm", "mpv.log"
)


class CliError(Exception):
    """A user-facing failure: printed to stderr, exit code 1."""


# -- wiring -----------------------------------------------------------------


def _ytdlp_path():
    """The yt-dlp next to this interpreter, else whatever is on PATH."""
    here = os.path.dirname(sys.executable)
    return shutil.which("yt-dlp", path=here) or shutil.which("yt-dlp")


def _js_runtime():
    for name in ("deno", "node"):
        if shutil.which(name):
            return name
    return None


AUTOPLAY_SCRIPT = os.path.join(os.path.dirname(__file__), "mpv", "autoplay.lua")


def player(spawn=True, **player_kwargs):
    """A connected Player, configured from config.toml and the stored auth."""
    from ytm import auth, config

    cfg = config.load()
    pot = cfg["pot"]
    autoplay = cfg["behaviour"]["autoplay_radio"]
    return Player(
        spawn=spawn,
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
        js_runtimes=_js_runtime(),
        audio_device=cfg["audio"]["device"],
        extra_args=[
            f"--volume={cfg['audio']['volume']}",
            # mpv runs detached with no terminal, so this file is the only
            # place a failed resolve or a dead audio device is ever reported
            f"--log-file={LOG_PATH}",
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
    from ytm import state

    track = select(" ".join(args.what))
    state.remember_tracks([track])
    with player() as p:
        if flags == "play":
            p.play(watch_url(track.video_id), title=_label(track))
        elif flags == "next":
            p.enqueue_next(watch_url(track.video_id), title=_label(track))
        else:
            p.enqueue(watch_url(track.video_id), title=_label(track))
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
    from ytm import music, state

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
            p.play(watch_url(seed.video_id), title=_label(seed))
        for track in tracks:
            p.enqueue(watch_url(track.video_id), title=_label(track))
    return (
        {"seed": fmt_track(seed), "tracks": [fmt_track(t) for t in tracks]},
        f"Radio from {seed.title} — {seed.artist}: {len(tracks)} tracks queued",
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
    known = {e["video_id"]: state.track_for(e["video_id"]) for e in entries if e["video_id"]}
    known = {k: v for k, v in known.items() if v}
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


def cmd_quit(args):
    try:
        with player(spawn=False) as p:
            p.quit()
    except PlayerError:
        return {"stopped": False}, "mpv was not running"
    return {"stopped": True}, "mpv stopped"


def cmd_auth(args):
    from ytm import auth

    if args.oauth:
        path = auth.oauth_setup(client_id=args.client_id, client_secret=args.client_secret)
    elif args.manual:
        path = auth.setup()
    else:
        path = auth.from_browser(args.from_browser or None)
    # regenerate the cookie file yt-dlp reads, so mpv's next resolve is authenticated
    auth.cookies_file()
    return {"saved": str(path)}, f"Saved credentials to {path}"


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
    ok, text = update.upgrade(kind=kind)
    if not ok:
        raise CliError(text)
    after = update.installed_version()
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

    p = add("search", cmd_search, "search songs")
    p.add_argument("query")
    p.add_argument("-n", "--limit", type=int, default=10)

    p = add("play", cmd_play, "play a song: a query, a result number, or a video id")
    p.add_argument("what", nargs="*")
    p = add("add", cmd_add, "queue a song at the end (or right after the current one)")
    p.add_argument("what", nargs="+")
    p.add_argument("--next", action="store_true", help="play it next instead of last")
    p = add("radio", cmd_radio, "queue a radio from a song (default: the one playing)")
    p.add_argument("what", nargs="*")
    p.add_argument("-n", "--limit", type=int, default=25)

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

    p = add("auth", cmd_auth, "sign in (default: cookies from a logged-in browser)")
    p.add_argument("--from-browser", nargs="?", const="", default=None, metavar="BROWSER")
    p.add_argument("--manual", action="store_true", help="paste request headers instead")
    p.add_argument("--oauth", action="store_true", help="OAuth device flow, for SSH")
    p.add_argument("--client-id", default=None)
    p.add_argument("--client-secret", default=None)

    p = add("cache", cmd_cache, "offline cache")
    cache_sub = p.add_subparsers(dest="cache_command")
    cache_sub.add_parser("add").add_argument("video_id")
    cache_sub.add_parser("rm").add_argument("video_id")
    cache_sub.add_parser("list")
    p.set_defaults(cache_command="list")

    p = add("update", cmd_update, "upgrade ytm and yt-dlp to the latest release")
    p.add_argument("--check", action="store_true", help="only report whether a newer version exists")
    p.add_argument("--force", action="store_true", help="reinstall even when already current")

    add("tui", cmd_tui, "open the full-screen interface")
    return parser


def main(argv=None, out=sys.stdout, err=sys.stderr):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        return cmd_tui(args) or 0
    try:
        data, text = args.func(args)
    except (CliError, PlayerError) as exc:
        print(exc, file=err)
        return 1
    except Exception as exc:  # auth and network failures included
        from ytm import auth

        if isinstance(exc, auth.AuthError):
            print(exc, file=err)
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
