"""Bounded, safe per-run diagnostics that survive a restart (M1).

The TUI used to truncate one fixed trace at every start, so the failed run
was gone exactly when it was needed. Each run now gets its own small file in
a bounded directory; the newest `KEEP_RUNS` files (and a total size cap) are
kept, and files another live run may still be writing are not deleted.

Only safe application-level lines are written here. `redact()` removes
credential-looking values before anything reaches disk: cookie headers,
signing cookies, OAuth tokens and signed media URLs. mpv's own log is
separate and deliberately not copied into this history, because it can
contain resolved signed URLs.

``YTM_TUI_LOG`` keeps its documented meaning: one explicit file, truncated
at each start, never rotated. Tests and bug reports use it to pin the
location. A logging failure never prevents playback or startup.
"""

import os
import re
import time
from pathlib import Path

from ytm.paths import application_path

#: how many per-run files to keep
KEEP_RUNS = 10

#: total size cap across kept per-run files
MAX_TOTAL_BYTES = 2 * 1024 * 1024

#: do not delete a file a live run may still be appending to
ACTIVE_SECONDS = 30

DEFAULT_DIR = application_path("state", "logs")

_RUN_PATH = None

_REDACTIONS = (
    (re.compile(r"(?i)\b(cookie|authorization|set-cookie)\b\s*[:=]\s*"
                r"(?:SAPISIDHASH\s+)?[^\s,;]+"), r"\1=<redacted>"),
    (re.compile(r"(?i)\b(SID|HSID|SSID|APISID|SAPISID|__Secure-[13]PAPISID)\s*=\s*[^;\s]+"),
     r"\1=<redacted>"),
    (re.compile(r"(?i)\b(access_token|refresh_token|id_token|client_secret|password)"
                r"\b\s*[:=]\s*[^\s,;]+"), r"\1=<redacted>"),
    (re.compile(r"(?i)https?://\S*[?&](?:token|sig|signature|expire|key|sparams)=[^\s&]+"),
     "<signed-url>"),
)


def redact(line):
    """Remove credential-looking values from one log line."""
    text = str(line)
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def start_run(now=None, pid=None):
    """Create a fresh per-run log and return its path, or None on failure.

    The filename carries the start time and pid, so two TUI instances
    starting in the same second do not share a file.
    """
    moment = time.time() if now is None else now
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(moment))
    path = DEFAULT_DIR / f"tui-{stamp}-{os.getpid() if pid is None else pid}.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
    except OSError:
        return None
    prune()
    return path


def run_path():
    """This process's run log: the explicit override or a per-run file.

    The per-run file is cached, so every trace line of one run lands in one
    file. An explicit YTM_TUI_LOG is re-read on every call: tests and
    scripts can redirect tracing without restarting the process. None means
    tracing is unavailable and callers must carry on without it.
    """
    explicit = os.environ.get("YTM_TUI_LOG")
    if explicit:
        return Path(explicit)
    global _RUN_PATH
    if _RUN_PATH is None:
        _RUN_PATH = start_run()
    return _RUN_PATH


def reset():
    """Forget the cached run path (tests, and after a fork)."""
    global _RUN_PATH
    _RUN_PATH = None


def write(line, mode="a", path=None):
    """Append one redacted line to the run log; never raise."""
    target = Path(path) if path is not None else run_path()
    if target is None:
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, mode, encoding="utf-8") as file:
            file.write(f"{time.strftime('%H:%M:%S')} {redact(line)}\n")
    except OSError:
        pass


def prune(now=None):
    """Keep the newest runs within both caps; never touch an explicit path."""
    if os.environ.get("YTM_TUI_LOG"):
        return
    now = time.time() if now is None else now
    try:
        files = [path for path in DEFAULT_DIR.glob("tui-*.log") if path.is_file()]
    except OSError:
        return
    files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    total = 0
    for index, path in enumerate(files):
        try:
            stat = path.stat()
        except OSError:
            continue
        total += stat.st_size
        if index < KEEP_RUNS and total <= MAX_TOTAL_BYTES:
            continue
        if stat.st_mtime > now - ACTIVE_SECONDS:
            continue  # a live run is still appending to this one
        try:
            path.unlink()
        except OSError:
            pass
