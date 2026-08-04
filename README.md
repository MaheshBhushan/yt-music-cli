# ytm

A full-screen terminal music player for YouTube Music. Two-process design: a background daemon (`ytmd`) owns the queue, playback state, and drives a headless `mpv --no-video`, while a Textual TUI (`ytm`) talks to it over a Unix domain socket. Closing the terminal does not stop the music; one-shot commands like `ytm next` talk to the same daemon.

No browser, no Electron, no window — audio plays while the terminal stays the only thing on screen.

## Requirements

- **Python 3.11+**
- **`mpv`** (audio playback; must be installed separately as an external binary, not via pip)
  - On Linux: `apt install mpv`, `pacman -S mpv`, etc.
  - On macOS: `brew install mpv`
  - On other platforms: <https://mpv.io>

Run `pip install -e .` to install Python dependencies from `pyproject.toml`:
- `ytmusicapi` — YouTube Music API
- `yt-dlp` — stream URL resolution and offline cache downloads
- `textual` — terminal UI framework
- `dbus-next` — MPRIS support (optional; degrades gracefully if missing or no session bus)

## Install

```bash
# Clone and enter the repo
git clone <url>
cd ytm

# Create a virtualenv and activate it
python3 -m venv .venv
source .venv/bin/activate

# Install in editable mode
pip install -e .

# Verify installation
ytm --help
```

## Authentication

YouTube Music authentication is required and must be set up once by hand:

```bash
ytm auth
```

The command will prompt you to:

1. Open <https://music.youtube.com> in your browser, logged in to your account
2. Open DevTools (F12 or Cmd+Shift+I)
3. Go to the Network tab
4. Filter by `/browse`
5. Click anywhere in the app to trigger a request (e.g., click Library)
6. Find the POST request to a `/browse` endpoint with a 200 response
7. Right-click it → Copy → Copy request headers
8. Paste the headers into the `ytm auth` prompt
9. Press Ctrl-D to finish

The headers are stored in `~/.config/ytm/auth.json` with mode 0600. They must include `cookie` and `x-goog-authuser` to work.

**Important:** Browser header auth expires when your YouTube session is revoked or the cookie ages out (typically after weeks). When it expires, the app will report it and you re-run `ytm auth` with fresh headers.

**Why not OAuth?** ytmusicapi 1.12.1's OAuth path requires you to provision your own Google Cloud OAuth client (credentials, redirect URIs, etc.), which is impractical for a CLI tool. Browser headers are the only credentials a human can supply interactively.

## Usage

### Launch the TUI

```bash
ytm
```

The TUI will auto-spawn the daemon (`ytmd`) if it is not already running. Close with `q` (quit) or `Q` (quit + stop daemon).

To explicitly manage the daemon:

```bash
ytmd                  # Start the daemon in the foreground
# (Ctrl-C to stop)
```

### One-shot commands

Each command below talks to the daemon (spawning it if needed) and returns immediately:

```bash
ytm search "song name"        # Search YouTube Music; outputs title, artist, album, duration
ytm next                       # Skip to the next track
ytm prev                       # Go to the previous track
ytm pause                      # Pause playback
ytm resume                     # Resume playback
ytm toggle                     # Toggle play/pause
ytm status                     # Show current track and state
ytm volume <0-100>             # Set volume (0-100)

ytm cache add <video_id>       # Download a track's audio into the offline cache
ytm cache rm <video_id>        # Remove a cached track
ytm cache list                 # List all cached tracks (video_id, size, path)
```

## Keybindings

### TUI keybindings (full list)

| Key | Action | Notes |
|-----|--------|-------|
| `/` | Search | Customizable via config (`keys.search`) |
| `a` | Enqueue selected | Hardcoded |
| `P` | Focus Playlists pane | Hardcoded |
| `A` | Add selected track to playlist | Hardcoded |
| `space` | Play/Pause | Customizable via config (`keys.toggle`) |
| `n` | Next | Customizable via config (`keys.next`) |
| `p` | Previous | Customizable via config (`keys.prev`) |
| `←` | Seek back 5s | Hardcoded |
| `→` | Seek forward 5s | Hardcoded |
| `+` | Volume up 5% | Hardcoded |
| `-` | Volume down 5% | Hardcoded |
| `tab` | Cycle panes | Hardcoded |
| `q` | Quit TUI (daemon keeps running) | Customizable via config (`keys.quit`) |
| `Q` | Quit TUI and stop daemon | Hardcoded |

Five keys are customizable:
- `keys.search` (default: `/`)
- `keys.toggle` (default: `space`)
- `keys.next` (default: `n`)
- `keys.prev` (default: `p`)
- `keys.quit` (default: `q`)

All other keys are hardcoded and cannot be changed.

## Config

Configuration file: `~/.config/ytm/config.toml`

Example with all defaults:

```toml
[audio]
volume = 70
device = "auto"

[behaviour]
autoplay_radio = true
confirm_remote_delete = true

[ui]
theme = "dark"

[keys]
toggle = "space"
next = "n"
prev = "p"
search = "/"
quit = "q"
```

### Schema and defaults

| Section | Key | Type | Default | Description |
|---------|-----|------|---------|-------------|
| `audio` | `volume` | int | 70 | Initial volume (0-100). Overridden by persisted state if it exists. |
| `audio` | `device` | string | `"auto"` | Audio device for mpv. Use `"auto"` or a device string (e.g., `"pulse"`). |
| `behaviour` | `autoplay_radio` | bool | `true` | Auto-fill queue with radio recommendations when it empties. |
| `behaviour` | `confirm_remote_delete` | bool | `true` | Require explicit `confirm=true` for deleting/removing tracks from remote playlists. |
| `ui` | `theme` | string | `"dark"` | Textual theme: `"dark"` or `"light"`. Unknown themes fall back to dark with a warning. |
| `keys` | `toggle` | string | `"space"` | Key to toggle play/pause. |
| `keys` | `next` | string | `"n"` | Key to skip to next. |
| `keys` | `prev` | string | `"p"` | Key to go to previous. |
| `keys` | `search` | string | `"/"` | Key to focus search. |
| `keys` | `quit` | string | `"q"` | Key to quit the TUI. |

### Behavior

- **Missing file:** If `~/.config/ytm/config.toml` does not exist, all defaults apply.
- **Partial file:** Any keys you omit use their defaults.
- **Volume precedence:** If a persisted state file exists (from a previous session), its volume wins over the config default. The config default only applies on first run when no state file exists.
- **Malformed TOML or bad values:** The app prints a readable warning to stderr, ignores the bad key/section, and uses the default. It never refuses to start due to a config error.

## Offline cache

Downloaded track audio is cached locally at `~/.cache/ytm/tracks/{video_id}.{ext}`. Cached tracks play instantly without network or stream URL resolution.

```bash
ytm cache add <video_id>       # Download and cache a track
ytm cache rm <video_id>        # Remove a cached track
ytm cache list                 # List: video_id, size, path for all cached tracks
```

The cache enforces a size cap (2 GB by default) by evicting least-recently-*used* entries (tracked by mtime). Partial/interrupted downloads never pollute the cache — they land in a private temp directory and only move into the cache directory after a successful download.

## Local playlists

In addition to remote YouTube Music playlists, you can create local (on-disk) playlists stored at `~/.local/state/ytm/playlists.json`. They live entirely offline and require no network:

- **Local playlists** are prefixed `local-` and live on disk. No confirmation needed for any operation.
- **Remote playlists** are stored on your YouTube Music account. Destructive operations (delete, remove tracks) require explicit `confirm=true` by default (can be disabled with `confirm_remote_delete = false` in config).

The TUI shows both types side-by-side; you can add tracks to or manage either kind.

## MPRIS

The daemon exposes itself on the session D-Bus as `org.mpris.MediaPlayer2.ytm`. This allows system media keys and tools like `playerctl` to control playback:

```bash
playerctl -p ytm play-pause
playerctl -p ytm next
playerctl -p ytm previous
```

Media keys (if configured by your desktop environment) should just work.

**Graceful degradation:** If D-Bus is unavailable (headless/CI environment, no session bus, `dbus-next` not installed), MPRIS registration is skipped with a warning, but the daemon and TUI function normally.

## Reliability notes

### yt-dlp updates

YouTube periodically changes its signature/throttling logic, and yt-dlp patches within days. A stale yt-dlp may fail to resolve streams or download cached tracks.

**Keep yt-dlp updated:**

```bash
pip install -U yt-dlp
```

There is no version pin; ytm works with any recent yt-dlp. A warning is printed if the installed version is more than 4 weeks old.

### Personal uploads

Personal uploads from your YouTube account are explicitly excluded from search results. Only official YouTube Music content is supported.
