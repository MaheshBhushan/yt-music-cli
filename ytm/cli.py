"""Command-line interface entry point."""
import argparse
import sys

from ytm import api, auth, cache
from ytm.client import Client, ClientError


def cmd_auth(args):
    """Run the authentication setup: interactive, or --from-browser to extract cookies."""
    if args.from_browser is not None:
        path = auth.from_browser(args.from_browser or None)
    else:
        path = auth.setup()
    print(f"Saved credentials to {path}")


def cmd_search(args):
    """Print normalised search results as plain text."""
    for track in api.search(args.query):
        print(f"{track.title}\t{track.artist}\t{track.album or '-'}\t{track.duration}")


def _describe_status(data):
    """A short human-readable status line from `status` command data."""
    current = data.get("current")
    state = "paused" if data.get("paused") else "playing"
    if current is None:
        return f"[{state}] nothing playing (volume {data.get('volume')})"
    return (
        f"[{state}] {current.get('title')} - {current.get('artist')} "
        f"(volume {data.get('volume')})"
    )


def _one_shot(cmd, args=None, describe=None):
    """Send one command to the daemon and print a short result line."""
    try:
        client = Client()
        try:
            data = client.request(cmd, args)
        finally:
            client.close()
    except ClientError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(describe(data) if describe else f"ok: {cmd}")
    return 0


def cmd_next(args):
    return _one_shot("next", describe=lambda data: "next")


def cmd_prev(args):
    return _one_shot("prev", describe=lambda data: "prev")


def cmd_pause(args):
    return _one_shot("pause", describe=lambda data: "paused")


def cmd_resume(args):
    return _one_shot("resume", describe=lambda data: "resumed")


def cmd_toggle(args):
    return _one_shot(
        "toggle", describe=lambda data: "paused" if data.get("paused") else "resumed"
    )


def cmd_status(args):
    return _one_shot("status", describe=_describe_status)


def cmd_volume(args):
    return _one_shot(
        "volume",
        {"level": args.level},
        describe=lambda data: f"volume: {data.get('volume')}",
    )


def cmd_cache_add(args):
    """Download a videoId's audio into the offline cache."""
    path = cache.download(args.video_id)
    print(f"cached {args.video_id} -> {path}")
    return 0


def cmd_cache_rm(args):
    """Remove a videoId's entry from the offline cache."""
    if cache.remove(args.video_id):
        print(f"removed {args.video_id}")
        return 0
    print(f"not cached: {args.video_id}", file=sys.stderr)
    return 1


def cmd_cache_list(args):
    """List cached entries."""
    for entry in cache.list_cached():
        print(f"{entry['video_id']}\t{entry['size']}\t{entry['path']}")
    return 0


def main():
    """Parse arguments and dispatch commands."""
    parser = argparse.ArgumentParser(description="YouTube Music CLI")
    subparsers = parser.add_subparsers(dest="command")

    auth_parser = subparsers.add_parser("auth", help="set up YouTube Music authentication")
    auth_parser.add_argument(
        "--from-browser",
        nargs="?",
        const="",
        default=None,
        metavar="BROWSER",
        help="extract cookies from a local browser profile instead of pasting headers "
        "(auto-detects a logged-in browser if BROWSER is omitted)",
    )
    auth_parser.set_defaults(func=cmd_auth)

    search_parser = subparsers.add_parser("search", help="search YouTube Music for songs")
    search_parser.add_argument("query", help="search query")
    search_parser.set_defaults(func=cmd_search)

    next_parser = subparsers.add_parser("next", help="skip to the next track")
    next_parser.set_defaults(func=cmd_next)

    prev_parser = subparsers.add_parser("prev", help="go to the previous track")
    prev_parser.set_defaults(func=cmd_prev)

    pause_parser = subparsers.add_parser("pause", help="pause playback")
    pause_parser.set_defaults(func=cmd_pause)

    resume_parser = subparsers.add_parser("resume", help="resume playback")
    resume_parser.set_defaults(func=cmd_resume)

    toggle_parser = subparsers.add_parser("toggle", help="toggle play/pause")
    toggle_parser.set_defaults(func=cmd_toggle)

    status_parser = subparsers.add_parser("status", help="show the current track and state")
    status_parser.set_defaults(func=cmd_status)

    volume_parser = subparsers.add_parser("volume", help="set the playback volume")
    volume_parser.add_argument("level", type=float, help="volume level (0-100)")
    volume_parser.set_defaults(func=cmd_volume)

    cache_parser = subparsers.add_parser("cache", help="manage the offline track cache")
    cache_subparsers = cache_parser.add_subparsers(dest="cache_command")

    cache_add_parser = cache_subparsers.add_parser("add", help="download a track into the cache")
    cache_add_parser.add_argument("video_id", help="YouTube videoId")
    cache_add_parser.set_defaults(func=cmd_cache_add)

    cache_rm_parser = cache_subparsers.add_parser("rm", help="remove a track from the cache")
    cache_rm_parser.add_argument("video_id", help="YouTube videoId")
    cache_rm_parser.set_defaults(func=cmd_cache_rm)

    cache_list_parser = cache_subparsers.add_parser("list", help="list cached tracks")
    cache_list_parser.set_defaults(func=cmd_cache_list)

    args = parser.parse_args()
    if getattr(args, "command", None) == "cache" and not getattr(args, "func", None):
        cache_parser.print_help()
        return 0
    if not getattr(args, "func", None):
        from ytm.tui.app import run as run_tui

        run_tui()
        return 0
    try:
        return args.func(args) or 0
    except auth.AuthError as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
