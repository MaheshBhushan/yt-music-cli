# ytm — Project Handout

**Version:** 0.1.0  
**Platform:** Linux (Python 3.11+, mpv)  
**Repository:** [github.com/MaheshBhushan/yt-music-cli](https://github.com/MaheshBhushan/yt-music-cli)  
**Status:** Working daily-use build with an automated test suite; no packaged release yet

## Project at a Glance

ytm is a terminal music player for YouTube Music. It offers a full-screen TUI with search, a queue, lyrics and playlist management, plus one-shot commands such as `ytm next` and `ytm status`, all driven against the user's real YouTube Music account. No browser and no Electron window is ever opened.

Playback lives in a background daemon, so music keeps playing when the TUI is closed, and system media keys work through MPRIS.

## Problem Statement

Listening to YouTube Music on a Linux desktop normally means a browser tab or an Electron wrapper. Both are heavy, neither integrates with media keys reliably, and neither fits a keyboard-driven terminal workflow.

Getting audio out of YouTube from the command line has its own obstacles:

- stream URLs expire within hours and must be resolved fresh each time;
- YouTube's bot detection blocks unauthenticated or unattested requests, and may put logged-in sessions into a mode that returns no usable formats;
- the official API surface is undocumented and shifts, so anything hand-rolled breaks quickly.

ytm solves this by composing maintained components rather than reimplementing them: `ytmusicapi` for the catalogue, `yt-dlp` for stream resolution, a PO token provider for attestation, and `mpv` for audio.

## Main Features

- Full-screen Textual TUI: search, results, now-playing, queue, lyrics and playlists panes.
- One-shot CLI commands for scripting and keybinding: search, next, prev, pause, resume, toggle, status, volume.
- Background daemon holding all playback state; the TUI and CLI are both thin clients.
- Radio autoplay: when the queue runs out, it refills from YouTube Music's radio for the last track.
- Gapless-oriented prefetch of the next track's stream URL shortly before it is needed.
- Three authentication paths: cookies pulled straight from a logged-in browser profile, manual header paste, and an OAuth device-code flow for headless machines.
- Offline cache with a 2 GB LRU cap; cached tracks play without network.
- Local on-disk playlists alongside remote account playlists, with confirmation gating on destructive remote operations.
- MPRIS integration as `org.mpris.MediaPlayer2.ytm`, so media keys and `playerctl` work.
- TOML config for volume, audio device, theme, radio autoplay and five customisable keys.
- Persisted session: queue, cursor and volume survive a daemon restart and come back paused.

## How It Works

Every client speaks newline-delimited JSON to the daemon over a Unix socket at `$XDG_RUNTIME_DIR/ytm/ytmd.sock`. Requests carry an id and a command; responses echo the id. Events such as `track_changed`, `position` and `state_changed` are pushed to every connected client, so nothing polls.

Starting `ytm` with no arguments launches the TUI, which spawns the daemon if the socket is not there. The daemon starts a headless `mpv --no-video --idle` and drives it over mpv's JSON IPC, observing `time-pos` and `pause` and listening for `end-file`.

Playing a track goes through three stages:

1. **Catalogue lookup** via `ytmusicapi` (search, radio, lyrics, playlists) using the stored credentials.
2. **Stream resolution** via `yt-dlp` at play time only. Cookies are tried first, because they make private and age-gated tracks resolvable, and dropped as a fallback if YouTube answers with the URL-less SABR format set. A PO token from the `bgutil` provider accompanies authenticated requests. Resolved URLs are held in memory and never written to disk.
3. **Playback** by handing the URL to mpv. If the track is in the offline cache, the local file is played and resolution is skipped.

Concurrency is deliberately narrow. Command handlers run in worker threads under a single daemon lock. Player observers fired by the mpv reader thread are scheduled onto the event loop and run under that same lock, so the queue has exactly one writer at any time and the reader thread never blocks on a network resolve.

## Architecture

```text
ytm (Textual TUI) ──┐
ytm one-shot CLI  ──┼── unix socket, JSON lines ──> ytmd daemon
playerctl / keys  ──┘ (via MPRIS on session D-Bus)      │
                                                        ├── Queue (cursor, prefetch, radio refill,
                                                        │          circuit breaker on failures)
                                                        ├── Player ── JSON IPC ──> mpv (audio)
                                                        ├── state.json (queue, volume, last played)
                                                        └── playlists.json (local playlists)
                          ┌──────────────────────────────┤
                    ytmusicapi                        yt-dlp + bgutil PO token plugin
                  (search, radio,                     (stream URL resolution)
                   lyrics, playlists)                        │
                                               bgutil-provider (Docker, port 4416)
```

Module layout:

| Module | Role |
|---|---|
| `ytm/cli.py` | argparse front end; no subcommand launches the TUI |
| `ytm/client.py` | socket client with a single reader thread routing responses and events |
| `ytm/daemon/server.py` | asyncio Unix server, command routing, event broadcast, lifecycle |
| `ytm/daemon/queue.py` | queue state machine, prefetch, radio autoplay, failure breaker |
| `ytm/daemon/player.py` | mpv process and JSON IPC wrapper |
| `ytm/daemon/mpris.py` | MPRIS interface over `dbus-next` |
| `ytm/daemon/state.py` | persisted session state |
| `ytm/api.py` | thin normalising layer over `ytmusicapi` |
| `ytm/auth.py` | browser cookie extraction, header paste, OAuth device flow |
| `ytm/resolve.py`, `ytm/pot.py` | yt-dlp options, cookie fallback chain, PO token provider |
| `ytm/cache.py`, `ytm/playlists_local.py` | offline cache and on-disk playlists |
| `ytm/config.py` | TOML config with defaults and tolerant parsing |
| `ytm/tui/` | Textual app and its panes |

## Technology Stack

- **Python 3.11+** with `asyncio` for the daemon and `tomllib` for config.
- **Textual** for the TUI.
- **ytmusicapi** for YouTube Music's InnerTube API.
- **yt-dlp** for stream resolution, with the **bgutil-ytdlp-pot-provider** plugin and its Docker-hosted token service.
- **mpv** as the audio engine, controlled over JSON IPC.
- **dbus-next** for MPRIS.
- **pytest** for the test suite.

## Safety and Reliability

- Credentials are stored with mode 0600 under `~/.config/ytm/` and are gitignored. Cookie extraction runs with a silenced logger so cookies never reach a log.
- Browser-extracted credentials are validated with a live call and deleted again if they fail, so a dead auth file is never left behind.
- Stream URLs are never persisted.
- Deleting or removing tracks from a remote playlist requires an explicit confirm flag by default.
- Malformed JSON, unknown commands and bad argument types are answered with an error and never take down the daemon.
- A track that fails to load is skipped and reported; three consecutive failures trip a breaker so a run of dead tracks cannot skip through the whole queue.
- A second daemon refuses to start if a live one owns the socket; stale socket files are cleaned up.
- SIGTERM and SIGINT run the same teardown as the `shutdown` command, so mpv is never orphaned.
- Missing D-Bus, a missing PO token provider or a stale config all degrade with a warning rather than refusing to start.
- The mpv reader thread survives any exception in an observer, logging it instead of going deaf to mpv.

## Installation and Development

```bash
git clone https://github.com/MaheshBhushan/yt-music-cli.git ytm
cd ytm
python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev]"
ln -sf "$PWD/.venv/bin/ytm"  ~/.local/bin/ytm
ln -sf "$PWD/.venv/bin/ytmd" ~/.local/bin/ytmd
ln -sf "$PWD/.venv/bin/ytm"  ~/.local/bin/y      # optional short alias

ytm auth --from-browser     # pull cookies from a logged-in browser
ytm                         # launch the TUI
```

Requirements: `mpv` on the PATH and, for authenticated stream resolution, Docker running so the daemon can start the `bgutil-provider` container. Without Docker the daemon still works and falls back to unauthenticated resolution.

Run the tests with:

```bash
.venv/bin/python -m pytest -q
```

Keep `yt-dlp` current. YouTube changes break it periodically and the fix ships within days:

```bash
.venv/bin/pip install -U yt-dlp bgutil-ytdlp-pot-provider
```

## Verification Snapshot

As of 2026-09-02:

| Check | Result |
|---|---|
| Test suite | 258 tests passing |
| Commits on main | 37 |
| Source / test lines | about 4,400 / 5,200 |
| Live stream resolution | working |
| Daemon spawn, session restore, live play | working |
| Open pull requests | 0 |
| Open issues | 1 (issue 10) |

All tests run against fake players and fake API clients. Live checks against YouTube are done sparingly and by hand, because heavy automated live testing from one IP address triggered a temporary bot-detection block during the initial build.

## Current Limitations

- Search supports only the `songs` filter; albums, artists and videos are not searchable.
- Personal uploads are excluded from results.
- The PO token provider depends on Docker; a lighter Node script mode exists but is not wired up.
- Dependencies are unpinned and there is no lockfile or CI workflow yet.
- When auto-spawned by the TUI, the daemon's output is discarded and there is no log file, which makes field debugging harder than it should be.
- Issue 10: `status` can report playing when mpv is merely unpaused with nothing loaded.
- Network calls inside command handlers hold the daemon lock, so a slow search or resolve delays other commands briefly.

## Planned Improvements

- Fix issue 10 and add a daemon log file under `~/.local/state/ytm/`.
- Resolve stream URLs outside the daemon lock so the UI never waits on the network.
- Pin dependencies with a lockfile and add a CI job running the test suite.
- Replace the Docker token service with the provider's script mode.
- Add an `ytm upgrade` command that updates yt-dlp and the token plugin.
- Widen search to albums, artists and playlists.

## Conclusion

ytm is a working terminal client for YouTube Music built by composing maintained tools rather than reimplementing them. Its daemon-and-thin-client design keeps playback independent of the UI, its fallbacks keep it playing when YouTube changes course, and a large mocked test suite keeps development off YouTube's rate limiter. The remaining work is operational hardening rather than missing features.
