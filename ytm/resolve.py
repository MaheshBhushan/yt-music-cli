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


def cookie_attempts():
    """The cookie headers to try, in order: authenticated first, then without.

    Sending real account cookies puts some sessions into YouTube's SABR-only
    experiment, in which every audio and video format comes back without a
    URL and yt-dlp is left with nothing but storyboards -- so any format
    selector fails with "Requested format is not available". The very same
    request without cookies gets ordinary progressive formats back. Cookies
    are therefore tried first (they are what makes private and age-gated
    tracks resolvable), and dropped only as a fallback.
    """
    cookie_header = _cookie_header()
    return [cookie_header, None] if cookie_header else [None]


class _SilentLogger:
    """Swallows yt-dlp's output for an attempt whose failure is expected."""

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


def ydl_opts(cookie_header, silent=False, **extra):
    """Base yt-dlp options for audio extraction, with `cookie_header` if set.

    `silent` suppresses yt-dlp's own error reporting, for an attempt that
    has a further fallback behind it and so must not look like a failure.
    """
    opts = {
        "format": "bestaudio",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    if silent:
        opts["logger"] = _SilentLogger()
    opts.update(extra)
    if cookie_header:
        opts["http_headers"] = {"Cookie": cookie_header}
    return opts


def resolve_stream_url(video_id, ydl_class=yt_dlp.YoutubeDL):
    """Resolve a videoId to a direct bestaudio stream URL.

    Cookies from ytm.auth are attached when available; resolution still
    proceeds without them, since unauthenticated resolution often works.
    The returned URL is not written anywhere -- callers must keep it in
    memory only, as it expires within hours.
    """
    url = _WATCH_URL.format(video_id=video_id)
    attempts = cookie_attempts()
    for attempt, cookie_header in enumerate(attempts):
        last = attempt == len(attempts) - 1
        try:
            with ydl_class(ydl_opts(cookie_header, silent=not last)) as ydl:
                info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError:
            if last:
                raise
            continue
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
