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
- `bgutil-ytdlp-pot-provider` — yt-dlp plugin that fetches PO tokens (BotGuard attestations) when YouTube demands one; see [PO token provider](#po-token-provider)
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

YouTube Music authentication is required. Choose the method below:

### Automated: Extract cookies from your browser

```bash
ytm auth --from-browser
```

Auto-detects a logged-in browser (tries Chrome, Chromium, Edge, Brave, Vivaldi, Opera, Firefox in order) and extracts YouTube cookies. To target a specific browser:

```bash
ytm auth --from-browser chrome
```

The command writes `~/.config/ytm/auth.json` at mode 0600, then validates it with a live API call. If validation fails the file is removed, so a set of credentials that does not work is never left behind to fail later.

**Requires:** You must be logged in to <https://music.youtube.com> in the target browser.

### Manual: Paste request headers from DevTools

If `--from-browser` does not find your browser, use the interactive fallback:

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

### Session expiry

Browser header auth expires when your YouTube session is revoked or the cookie ages out (typically after weeks). When it expires, the app will report it and you re-run `ytm auth` (or `ytm auth --from-browser`) with fresh credentials.

### OAuth (headless / SSH, no local browser required)

Browser-based auth needs a local logged-in browser, so it doesn't work over SSH or on a headless box, and cookie auth expires after a few weeks. `ytm auth --oauth` uses YouTube's device-code flow instead: you get a short code to enter on any other device, and the resulting token refreshes itself rather than expiring.

YouTube removed ytmusicapi's built-in OAuth client in November 2024, so you must provision your own Google Cloud OAuth client first:

1. Go to <https://console.cloud.google.com/> and create (or select) a project
2. Enable the **YouTube Data API v3** for the project (APIs & Services → Library)
3. Go to APIs & Services → Credentials → Create Credentials → OAuth client ID
4. If prompted, configure the OAuth consent screen first (External is fine; add your own Google account as a test user)
5. Application type: **TVs and Limited Input devices**
6. Give it a name and click Create
7. Copy the generated **Client ID** and **Client secret**

Then run:

```bash
ytm auth --oauth --client-id YOUR_CLIENT_ID --client-secret YOUR_CLIENT_SECRET
```

The command prints a verification URL and a short user code — open the URL on any device, sign in, and enter the code. `ytm` then polls in the background until you finish and stores the refreshable token.

`--client-id`/`--client-secret` can also come from the `YTM_OAUTH_CLIENT_ID`/`YTM_OAUTH_CLIENT_SECRET` environment variables, or you'll be prompted interactively if neither is set. Precedence: flags > environment variables > interactive prompt.

The token is stored in `~/.config/ytm/auth.json` at mode 0600, same as browser-header auth (the two are detected automatically and never confused). The client ID/secret are stored separately, in `~/.config/ytm/oauth_client.json` at mode 0600, since a token refresh needs them again and ytmusicapi rewrites the token file with only token fields on every refresh.

**Consequence for playback:** an OAuth auth file has no browser cookies, so stream resolution (see below) always proceeds unauthenticated for OAuth users — private/age-restricted tracks that need authenticated cookies to resolve will not resolve. Search and library access are unaffected.

If the refresh token is later revoked (e.g. you revoke access in your Google account, or the OAuth client is deleted), `ytm` reports it as an expired-auth error telling you to run `ytm auth --oauth` again, rather than a raw traceback.

### Playback and authentication

Search, library browsing, and playlist operations use your authenticated session normally. However, stream URL resolution (converting a video ID to a playable audio stream) proceeds unauthenticated as a fallback: when yt-dlp receives authenticated cookies, some YouTube accounts are placed in an experiment where all audio/video formats return no usable URLs. The resolver therefore tries authenticated first (necessary for private or age-restricted tracks) and falls back to the same request without cookies if needed. **Consequence:** tracks that are private, age-restricted, or otherwise account-gated may not resolve for playback, while search and library access remain fully authenticated.

### PO token provider

Part of what YouTube asks for on such requests is a *PO token* — a BotGuard attestation that cannot be computed in Python. `ytm` supplies one through the maintained [`bgutil-ytdlp-pot-provider`](https://github.com/Brainicism/bgutil-ytdlp-pot-provider) yt-dlp plugin, which is installed as a dependency and asks a small HTTP service for tokens. The service runs as a Docker container:

```bash
docker run --name bgutil-provider --detach --init --restart unless-stopped \
    --publish 4416:4416 brainicism/bgutil-ytdlp-pot-provider
```

You do not have to run that yourself, and you do not have to start anything after a reboot:

- `ytmd` calls `ytm.pot.ensure_provider()` at startup. It pings `pot.base_url`; only if that fails does it `docker start` the container, and only if there is no container does it create one with the command above.
- `--restart unless-stopped` means Docker itself brings the container back after a reboot (provided `docker.service` is enabled), so the daemon's check is normally a single local ping.

It degrades rather than blocking playback. If Docker is missing, the container cannot start, or the service never answers, `ytmd` prints one line to stderr and carries on; resolution falls back to the cookies-then-no-cookies chain exactly as before. Set `enabled = false` under `[pot]` to opt out entirely.

**What this does and does not buy you, measured on a SABR-forced account.** With the token, the authenticated request stops failing and returns a stream URL — without it, that request fails outright. But those authenticated URLs answer `403` to the unbounded `Range: bytes=0-` that ffmpeg and mpv always send, while accepting bounded ranges, so mpv cannot play them:

```
authenticated URL   bytes=0-100000  ->  206
authenticated URL   bytes=0-        ->  403
unauthenticated URL bytes=0-        ->  206
```

Those formats are SABR-delivered and are not plain progressive HTTP. So on such an account the unauthenticated fallback is still what produces the playable URL, and the token changes nothing you can hear. The provider is kept wired because it is free at playback time and starts paying off the moment either side of that changes — YouTube dropping the experiment for the account, or yt-dlp handing mpv a stream it can consume. Streaming SABR would mean piping yt-dlp's output into mpv instead of passing a URL, which costs seeking; that trade was considered and declined.

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

[pot]
enabled = true
base_url = "http://127.0.0.1:4416"

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
| `pot` | `enabled` | bool | `true` | Whether to use the PO token provider (start it, and point yt-dlp at it). |
| `pot` | `base_url` | string | `"http://127.0.0.1:4416"` | Where the PO token service listens. The port is also the one the container publishes. |
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
