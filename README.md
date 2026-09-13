<h1 align="center">ytm</h1>
<p align="center">YouTube Music in the terminal: search, queue, radio and lyrics, with mpv doing the playing.</p>

<p align="center">
  <img alt="PyPI" src="https://img.shields.io/pypi/v/ytm">
  <img alt="Tests" src="https://github.com/MaheshBhushan/yt-music-cli/actions/workflows/tests.yml/badge.svg">
  <img alt="License" src="https://img.shields.io/github/license/MaheshBhushan/yt-music-cli">
  <img alt="Last commit" src="https://img.shields.io/github/last-commit/MaheshBhushan/yt-music-cli">
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-blue">
</p>

![ytm's TUI: search results on top, queue, playlists with your daily mixes and lyrics in the middle, the current track with its cover, what just played and what is up next at the bottom](docs/screenshot.png)

## Overview

YouTube Music has no desktop client that is not a browser. `ytm` is a small Python CLI and a Textual TUI over three tools that already do the hard parts: [ytmusicapi](https://github.com/sigma67/ytmusicapi) for the catalogue, [yt-dlp](https://github.com/yt-dlp/yt-dlp) for stream resolution and [mpv](https://mpv.io) for audio.

mpv is the only long-running process. `ytm` starts it once, idle, with a JSON IPC socket, and every command after that is a stateless message to it. Close the terminal and the music keeps playing. A Lua script inside mpv keeps the queue fed with the station for whatever is playing, so it never runs dry.

## Quickstart

```bash
pipx install ytm              # or: uv tool install ytm   /   pip install ytm

ytm install-mpv               # mpv plays the audio and pip cannot install it
ytm auth                      # sign in with Google, see Authentication
ytm play "daft punk"          # search, play the first hit, radio follows
ytm                           # the TUI
ytm update                    # later: newest ytm and yt-dlp, whatever installed it
```

To hack on it instead:

```bash
git clone https://github.com/MaheshBhushan/yt-music-cli.git && cd yt-music-cli
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
```

> [!IMPORTANT]
> `mpv` must be on your `PATH`. It is a separate program and pip cannot install it, so `ytm install-mpv` does: it runs your own package manager (`brew install mpv`, `sudo apt-get install -y mpv`, `sudo pacman -S mpv`, `scoop install mpv`, ...), showing the command first. Or install it yourself from <https://mpv.io>. Node is optional but recommended: yt-dlp uses it to solve YouTube's JavaScript challenges.

## Usage

The TUI is `ytm` with no arguments. Results appear as you type; Enter plays the first one. Every key is listed in the bar at the bottom, and everything is clickable: results, queue rows, playlists, the progress bar, the shortcuts. Your daily mixes (Supermix, Discover Mix, ...) sit below your playlists. In a terminal under 100 columns or 24 rows, such as a tmux pane, the layout collapses to the search box, the queue and the player strip.

| Key | Action |
|---|---|
| `/` or `s` | Focus search. While the search box has focus every letter is text, so the letter keys below wait for `Esc` or `↓` |
| `Esc` `↓` | Leave the search box for the lists |
| `h` | Hide the search box and results while listening; `s`, `/` or `h` bring them back |
| `Enter` | Play the selected result, queue entry or playlist |
| `q` `u` | Enqueue the selected song at the end / play it next |
| `space` | Play / pause |
| `n` `p` | Next / previous |
| `←` `→` | Seek 5 s |
| `+` `-` | Volume (`=` also raises it). This is the system output volume, so it matches the tray and the media keys; see `[audio] control` |
| `a` | Add the selected song to a playlist: `a`, pick the list with `↑` `↓`, `a` or `Enter` |
| `l` | Focus playlists |
| `r` | Refresh your mixes (a mix keeps the same tracklist until you do) |
| `Tab` | Cycle panes |
| `e` | Exit, music keeps playing |
| `x` | Exit and stop mpv |

One-shot commands talk to the same mpv. Add `--json` to any of them for machine-readable output.

```bash
ytm search "song name" -n 10   # results are numbered
ytm play 3                     # a number from the last search, an 11-char video id, or a query
ytm add 4                      # enqueue; add --next 4 puts it right after the current song
ytm radio                      # replace the queue with a station for the current track
ytm mix                        # list your daily mixes (Supermix, Discover Mix, ...)
ytm mix discover               # replace the queue with a mix, matched by substring
ytm status | queue | lyrics | like
ytm pause | resume | toggle | next | prev | stop
ytm seek -10 | seek --to 90 | volume 60 | clear | shuffle
ytm quit                       # stop mpv entirely
ytm version                    # the running version (and a newer one, if the daily check saw it)
ytm update                     # upgrade ytm and yt-dlp; --check only reports
ytm install-mpv                # install mpv with this machine's package manager
```

The queue never holds a track twice: playing something already queued jumps to it, and radio skips what is there.

## Authentication

Search and playback work signed out. Library, playlists, likes and lyrics need your account. Credentials live in `~/.config/ytm/auth.json` (mode 0600) and are checked with a live call before being kept. Two ways in:

```bash
ytm auth                          # 1. sign in with Google (OAuth): a desktop browser, or a device code for SSH and headless boxes
ytm auth --from-browser           # 2. cookies from a browser you are logged in to (auto-detects)
ytm auth --from-browser firefox   #    or name one: chrome, chromium, edge, brave, vivaldi, opera, helium, firefox
ytm auth --from-browser helium --profile "Profile 1"   # pick a browser profile (default: the one that is logged in)
```

OAuth tokens refresh themselves. Browser cookies expire after a few weeks; re-run `ytm auth --from-browser` when the app says so. OAuth has no browser cookies, so streams resolve anonymously for OAuth users: search, library and the normal catalogue play fine, private or age-gated tracks do not; import cookies from a browser if you need those.

### From a browser

Log in at <https://music.youtube.com>, then run `ytm auth --from-browser`. It tries each browser in turn and, within a browser, every profile (Chromium's `Default`, `Profile 1`, ...; System and Guest profiles are skipped), taking the first with a YouTube session. To read one profile only, name its directory: `ytm auth --from-browser helium --profile "Profile 1"` (for Firefox, the profile folder name). If none works, the error says why for each browser: not installed, no such profile, cookies could not be decrypted, database locked, no YouTube login, or a network failure while checking the cookies against YouTube Music.

Browser cookies go stale on their own: Google rotates the session tokens in the browser about once a day, and YouTube then treats ytm's copy as signed out. ytm remembers which browser and profile the cookies came from (`auth.source.json` next to `auth.json`) and, the first time a request comes back signed out, re-extracts them from that browser and retries, so the TUI recovers without a visit to the terminal. If the browser itself is signed out, the error says so and that the re-extraction failed. Pasted headers and OAuth are never refreshed this way.

If the browser is signed in to more than one Google account, pass `ytm auth --from-browser --authuser 1` (0 is the first account, 1 the second, ...) or set `auth.x-goog-authuser` in `config.toml` to make it the default.

> [!WARNING]
> **Windows:** Chrome, Edge, Brave, Vivaldi and Opera encrypt their cookies with App-Bound Encryption (Chrome 127 and newer), which no other program can read, so `ytm auth --from-browser` cannot import from them. Plain `ytm auth` (Google sign-in) needs no cookies; otherwise log in with **Firefox** and run `ytm auth --from-browser firefox`.

> [!WARNING]
> **macOS:** a program may not read another app's data until it has Full Disk Access, so `ytm auth --from-browser` sees nothing in any browser until your terminal has it: **System Settings → Privacy & Security → Full Disk Access**, switch your terminal on (add it with **+** if it is not listed), then quit it completely and reopen it. `ytm auth --from-browser` says so when this is what stopped it. Plain `ytm auth` (Google sign-in) needs none of this.

### OAuth

Plain `ytm auth` stores a token that refreshes itself, so there are no cookies to go stale. YouTube removed ytmusicapi's shared OAuth client in November 2024, so you need your own from Google Cloud once:

1. Go to <https://console.cloud.google.com/> and create or pick a project.
2. **APIs & Services → Library**: enable **YouTube Data API v3**.
3. **APIs & Services → OAuth consent screen**: External is fine. Add your own Google account under **Test users**.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**. Pick one of the two application types below, name it and create.

**Desktop app** (a browser on the machine running ytm): download the client JSON, then

```bash
ytm auth --client-file ~/Downloads/client_secret_....json
ytm auth                          # later: the client is remembered
```

Open the printed link in a browser on the same computer and approve YouTube access. The callback listens only on loopback, uses PKCE, and gives up after 15 minutes. `YTM_OAUTH_CLIENT_FILE` can name the JSON instead of the flag. The client is remembered in `~/.config/ytm/oauth_desktop_client.json`; a failed or incomplete sign-in leaves your existing credentials untouched.

**TVs and Limited Input devices** (SSH and headless boxes: the link can be opened on any device): copy the **Client ID** and **Client secret**, then

```bash
ytm auth --client-id YOUR_CLIENT_ID --client-secret YOUR_CLIENT_SECRET
```

ytm prints a URL and a short code; open the URL anywhere, sign in and enter the code. The flags can also come from `YTM_OAUTH_CLIENT_ID` / `YTM_OAUTH_CLIENT_SECRET`, and with neither set `ytm` prompts for them.

Either way the client ID and secret are kept in `~/.config/ytm/oauth_client.json` (mode 0600) because every token refresh needs them again. Revoking access in your Google account is reported as expired auth; run `ytm auth` again.

> [!NOTE]
> Streams resolve **anonymously by default** for everyone. With account cookies, YouTube hands out URLs that require an account-bound proof-of-origin token and then answers 403. Anonymous resolution plays the same catalogue. Set `behaviour.authenticated_streams = true` only if you need private or age-gated tracks.

## Configuration

`~/.config/ytm/config.toml`. A missing file means these defaults; a partial file overrides only what it names; a bad value is warned about and ignored.

```toml
[audio]
control = "system"              # "system": the desktop's output volume; "player": mpv's own
volume = 70                     # mpv's starting volume, only with control = "player"
device = "auto"                 # an mpv --audio-device name

[behaviour]
autoplay_radio = true           # keep the queue fed with radio
confirm_remote_delete = true
authenticated_streams = false   # see the note above

[auth]
x-goog-authuser = "0"           # Google account index for browser auth cookies

[ui]
theme = "dark"                  # or "light"
art = "blocks"                  # blocks | kitty | sixel | auto | ascii | off

[tui]
queue_column_width = 40         # max PLAYED / UP NEXT column width; 0 = no max

[pot]
enabled = true                  # proof-of-origin tokens via bgutil-ytdlp-pot-provider
base_url = "http://127.0.0.1:4416"

[keys]
toggle = "space"
next = "n"
prev = "p"
search = "/"
quit = "e"

[update]
check = true                    # ask PyPI once a day, toast in the TUI when newer
auto = false                    # true: install it (and fresh yt-dlp) automatically
```

`control = "system"` makes the volume in ytm the same one the desktop shows: `+`/`-` and `ytm volume` move the default output through `wpctl` (PipeWire) or `pactl` (PulseAudio), and a media key or the tray slider shows up in the TUI. mpv's own volume is held at 100 so the stream is not attenuated twice. Without either tool, or with `control = "player"`, ytm uses mpv's software volume, which only ytm sees.

`art = "blocks"` draws the cover with coloured half-cell glyphs and works in every terminal, tmux included. `kitty` and `sixel` use the terminal's pixel protocol; Sixel is known to freeze the pane in Konsole, which is why it is opt-in.

The proof-of-origin token provider is a yt-dlp plugin installed with `ytm`. It asks an HTTP service for tokens when YouTube demands one; run `docker run -d --name bgutil-provider -p 4416:4416 brainicism/bgutil-ytdlp-pot-provider` if you want it, or set `enabled = false`. Playback works without it for most accounts.

## More

- **Offline cache.** `ytm cache add <video_id>` downloads a track into `~/.cache/ytm/tracks/`; `cache rm` and `cache list` manage it. 2 GB cap, least-recently-played evicted first.
- **Local playlists** live in `~/.local/state/ytm/playlists.json` and show up next to your YouTube Music playlists in the TUI.
- **Media keys.** `ytm` has no MPRIS of its own; install the [mpv-mpris](https://github.com/hoyon/mpv-mpris) plugin and mpv announces itself to your desktop.
- **Updating.** `ytm update` upgrades ytm and yt-dlp through whatever installed them (pipx, `uv tool`, or pip), so the new version lands where the `ytm` command runs from. The TUI checks PyPI once a day and shows a toast when there is a newer release; set `auto = true` under `[update]` to have it install without asking. yt-dlp is why this matters: YouTube changes things and yt-dlp follows within days, so a stale copy is the usual cause of sudden "could not resolve" failures.
- **Windows** works over a named pipe to mpv. Cookie import needs Firefox there, see Authentication.
- **Logs.** mpv writes to `~/.local/state/ytm/mpv.log`. The TUI writes a trace of its last run to `~/.local/state/ytm/tui.log`: every key it received, each focus move, each backend request with its timing, each error and resize. It starts over on every launch; `YTM_TUI_LOG=<file>` puts it elsewhere. This is the file to attach for a key or focus problem in a particular terminal.

## Repository structure

```
ytm/
  cli.py            commands and the mpv launch configuration
  player.py         Player: mpv over JSON IPC
  music.py          ytmusicapi wrappers, Track
  state.py          remembered searches and track metadata
  auth.py           browser cookies, DevTools headers, OAuth
  cache.py          offline downloads
  update.py         version check against PyPI, in-place upgrade
  mpv/autoplay.lua  radio autoplay inside mpv
  tui/              Textual app, panes, backend over Player
tests/              pytest; no network and no mpv needed
.github/workflows/  tests on 3.11-3.13; publish to PyPI on a v* tag
```

```bash
pip install -e '.[dev]' && pytest -q
```

## License

MIT, see [LICENSE](LICENSE).
