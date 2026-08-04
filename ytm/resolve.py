"""Stream URL resolution via yt-dlp.

Resolves a YouTube ``videoId`` to a direct audio stream URL using yt-dlp's
Python API, reusing the cookies from ``ytm.auth`` (if any) to mitigate bot
detection. Resolved URLs expire within hours and must never be persisted to
disk -- callers should resolve fresh at playback time and keep the URL in
memory only.
"""

import sys
from datetime import datetime, timedelta

import yt_dlp

from ytm import auth

STALE_WARNING_DAYS = 28

_WATCH_URL = "https://www.youtube.com/watch?v={video_id}"


def check_ytdlp_freshness(now=None):
    """Warn on stderr if the installed yt-dlp release is more than 4 weeks old.

    YouTube periodically breaks signature/throttling logic and yt-dlp ships
    fixes within days, so a stale install is worth flagging. This never
    raises -- it is a warning, not a failure.
    """
    version = yt_dlp.version.__version__
    try:
        release_date = datetime.strptime(version, "%Y.%m.%d")
    except ValueError:
        return
    now = now or datetime.now()
    if now - release_date > timedelta(days=STALE_WARNING_DAYS):
        print(
            f"warning: installed yt-dlp {version} is more than "
            f"{STALE_WARNING_DAYS} days old; YouTube may have broken it since. "
            "Consider upgrading yt-dlp.",
            file=sys.stderr,
        )


def _cookie_header():
    """Best-effort fetch of the stored Cookie header; None if unavailable."""
    try:
        return auth.load_cookies()
    except auth.AuthError:
        return None


def resolve_stream_url(video_id, ydl_class=yt_dlp.YoutubeDL):
    """Resolve a videoId to a direct bestaudio stream URL.

    Cookies from ytm.auth are attached when available; resolution still
    proceeds without them, since unauthenticated resolution often works.
    The returned URL is not written anywhere -- callers must keep it in
    memory only, as it expires within hours.
    """
    ydl_opts = {
        "format": "bestaudio",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    cookie_header = _cookie_header()
    if cookie_header:
        ydl_opts["http_headers"] = {"Cookie": cookie_header}

    with ydl_class(ydl_opts) as ydl:
        info = ydl.extract_info(_WATCH_URL.format(video_id=video_id), download=False)
    return info["url"]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python -m ytm.resolve <videoId>", file=sys.stderr)
        return 2
    check_ytdlp_freshness()
    url = resolve_stream_url(argv[0])
    print(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
