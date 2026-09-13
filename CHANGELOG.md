# Changelog

## 0.8.0 — 2026-09-13

- **`ytm auth` now signs in with Google.** OAuth is the default: the first run takes a Google Desktop-app client JSON (`--client-file`) or a TV client id and secret and remembers them; every later `ytm auth` reuses them. Importing cookies from a browser is now explicit: `ytm auth --from-browser` auto-detects, `ytm auth --from-browser firefox` names one. `--oauth` is gone: there is nothing left for it to select.
- **The paste-your-headers mode is gone.** `ytm auth --manual`, which took request headers copied from DevTools, is removed along with its setup code; browser import or Google sign-in cover every case it did. Existing `auth.json` files written by it keep working until their cookies expire.
- `YTM_TUI_LOG=<file>` makes the TUI append the keys it receives, focus changes, backend requests with their timing, errors and terminal resizes to that file, so an input problem in one particular terminal can be diagnosed from the file instead of from memory.
- `ytm install-mpv` on Windows no longer reports a failure when mpv is already installed. winget answers "Found an existing package already installed... No available upgrade found" with exit code 2316632107 (`0x8A15002B`, update not applicable), and ytm read any non-zero exit as `winget install -e --id shinchiro.mpv failed`. That exit, and winget's "already installed" one, now count as success. The reason ytm tried to install at all is that winget and scoop add their directory to the PATH of terminals opened *afterwards*, so the shell that ran the install cannot see the new `mpv.exe`; ytm now looks in those directories itself (winget's Links folder and package directory, scoop's shims, Chocolatey's bin), both when deciding whether to install and when starting mpv, so a fresh Windows install plays without opening a new terminal.
- Picking a playlist right after opening ytm no longer needs a trick. The search box has focus at startup and takes every letter as text, so `l` typed the letter and Enter searched for it; the only way to the playlists was Escape, which nothing mentioned. `↓` now leaves the search box like Escape does, the shortcut bar opens with "Esc/↓ leave search, then:" while you are in it, and the placeholder says so too.
- `ytm update` no longer claims to have upgraded when nothing changed. Right after a release, pip's HTTP cache and the index CDN can still call the old version the newest, so `pip install -U` (and `pipx upgrade`) exited 0 having installed nothing, and ytm said "upgraded, restart ytm" until a second run some minutes later actually did it. Installers are now told to skip their caches, pip is asked for the exact version PyPI reported, and a fresh interpreter is asked what is installed afterwards; if it is still the old version the command fails and says the index has not caught up yet.
- The TUI no longer offers an update to a source checkout that is already ahead of PyPI. An editable install's recorded version is frozen at `pip install -e` time, so after a `git pull` ytm reported the old number, every PyPI release looked newer, and the "Update available" toast never went away. An editable install now reads its version from the checkout's `pyproject.toml`.

## 0.7.0 — 2026-09-13

- `ytm auth --oauth` can sign in through a browser on the same machine. Pass a Google **Desktop app** OAuth client JSON with `--client-file` (or `YTM_OAUTH_CLIENT_FILE`); ytm runs the PKCE flow against a loopback callback, stores a self-refreshing token, and remembers the client in `~/.config/ytm/oauth_desktop_client.json` so a plain `ytm auth --oauth` re-uses it. The TV/device-code flow is unchanged and is still what `--client-id`/`--client-secret` select; using it forgets a previously remembered desktop client so the two cannot fall out of step.
- The stored desktop token is reduced to exactly the fields ytmusicapi's `OAuthToken` defines, with integer expiry times and `token_type` filled in. The raw oauthlib token carries extra keys (`id_token`, a float `expires_at`) that ytmusicapi releases before 1.11 reject at every client start.
- A failed or incomplete Google sign-in, or a token without the YouTube scope, leaves existing credentials in place.
- New dependency: `google-auth-oauthlib`.

## 0.6.1 — 2026-09-13

- `ytm auth` on macOS no longer reports an installed browser as missing. macOS withholds one app's data from another until the asking app has Full Disk Access, and a blocked profile directory looks empty to yt-dlp, which reports the cookie database as missing — so `ytm auth` said "chrome: not installed or no profile found" on a Mac with Chrome open and logged in, and told the user to go and log in. It now tells a directory that is shut apart from one that is absent, says which it is per browser, and explains Full Disk Access (naming the terminal it is talking about) along with `--manual` and `--oauth`, which need none of it.
- ytm no longer gives up on an mpv that is still starting. It waited a fixed 10 s for mpv to open its IPC socket, and mpv's first start after installation is slower than that — 11 s on a Mac where Homebrew had just put it there, one second past the limit — so the first run after installing mpv failed with `mpv started but never opened its IPC endpoint` and the next one worked. The wait is now long enough for a cold start, and an mpv that has actually *died* is reported the moment it exits instead of at the end of the wait, so a real failure is quicker to hear about than it used to be.
- When mpv does fail to start, ytm says what mpv said. Its output went to `/dev/null`, so a missing library or a refused option left nothing to report but the absence of a socket; it is now kept beside the socket and quoted in the error, and where mpv stayed silent the error says where the rest is.
- `~/.local/state/ytm/` is created before mpv is told to write its log there. mpv does not create the directory and does not complain when it cannot write, so on a machine that had never had it the log the README points at — the only place a failed resolve or a dead audio device is ever reported — was never written at all.
- Cached downloads are now used for CLI, TUI, radio and playlist playback; cached filenames retain their YouTube video id so queue metadata and duplicate detection still work offline.
- Session updates are serialized across processes, preventing the CLI, TUI and autoplay helper from overwriting one another's remembered searches or tracks.
- State, local-playlist and audio-cache files now honor `XDG_STATE_HOME` and `XDG_CACHE_HOME` consistently.
- Slow OAuth client construction no longer holds the catalogue cache lock. Concurrent first callers still share one client, while reauthentication can invalidate an in-progress build safely.
- Visitor ids dated in the future are rejected, and failed visitor-file writes clean up their temporary file.
- Playlist fallback handling now catches expected authentication and network failures without hiding programmer errors; missing playlist counts remain uncached and keep the displayed zero.
- Restored `ytm.api` as a compatibility re-export for integrations written before 0.6.0.
- The fake mpv test server now treats a client disconnect as normal instead of emitting an unhandled thread warning.

## 0.6.0 — 2026-09-13

- Far fewer requests to YouTube. ytm built a new ytmusicapi client for every single catalogue call, and each one opened a fresh connection and downloaded the music.youtube.com home page just to read a visitor id out of it — so opening the playlists pane with ten playlists made about a dozen handshakes and a dozen page downloads to make a dozen API calls. One client now serves the whole process, retired only when `ytm auth` rewrites the credentials, and the visitor id is kept for a day in `~/.local/state/ytm/visitor.json` so one-shot commands do not have to fetch it again either.
- Adding a song to a playlist no longer re-lists the entire library behind it. The count in the pane was already updated in place; the refresh that followed fetched the playlist list plus a track count for every playlist in it, to learn a number the add itself had already reported.
- Track counts missing from the library listing (Liked Music, Episodes for Later) are looked up once per session instead of on every refresh of the playlists pane, and an empty mix list is taken as an answer rather than re-fetching the whole home feed each time.
- Lyrics and search results are remembered for the session, so coming back round to a song, or backspacing over a query, does not ask YouTube again.
- Loading a playlist or a radio station is much faster. Each track was appended with its own round trip to mpv that first re-read the whole playlist, so queueing 100 tracks meant 100 reads of a list that grew with every one — and the TUI redrew every row of the queue after each. Appends now share one read, and queue redraws are coalesced.
- The queue pane read and re-parsed the remembered-track file once per row, on every queue change. It is read once per redraw, and only when it has actually changed on disk.
- `ytm status` and every transport key ask mpv for all their properties in one batch rather than eight, and the play/pause key no longer shells out to `wpctl` twice.
- Plain `ytm` (the TUI) exited with status 1 and printed `(None, None)` when it closed.
- Network commands start about a quarter quicker (roughly 25 ms off each run): yt-dlp, which only `ytm auth` and the cookie refresh reach, is no longer imported just by importing ytm. mpv's autoplay script pays this on every radio top-up.
- A dropped connection during a command now says it could not reach YouTube Music, instead of printing a traceback, and auth errors in the TUI banner no longer arrive prefixed with the exception's class name.
- The session file is written through a temp file scoped to the writing process. The CLI, the TUI and the `ytm radio` that mpv's autoplay script spawns all write it, and they shared one temp name, so one could rename another's half-written copy into place.
- Removed `ytm/api.py`, a compatibility shim for the daemon and the old Textual TUI, both of which are gone. Import from `ytm.music` instead.
- A video id is read only from the real `v=` parameter of a URL, not from the tail of another parameter that happens to end in `v=`.
- `ytm install-mpv` installs mpv with whatever package manager the machine has (Homebrew, apt, dnf, pacman, zypper, apk, xbps, pkg, scoop, winget, Chocolatey), printing the command and asking before it runs it. mpv is a C program and cannot come from PyPI — the `mpv` and `python-mpv` packages there are bindings to libmpv, not the player — so `uv tool install ytm` leaves this one step, and a fresh install used to meet `error: could not start mpv (mpv): [Errno 2] No such file or directory: 'mpv'` with no hint that mpv is a separate program or how to get one. That error now says so, and names the exact command for the machine it is on.

## 0.5.15 — 2026-09-07

- Fixes a crash on startup or resize introduced in 0.5.11 (`TypeError: unsupported operand type(s) for -: 'NoneType' and 'int'` in the now-playing strip): the queue summary could be laid out before the strip had a width.

## 0.5.14 — 2026-09-07

- A song added to a remote playlist (or liked into Liked Music) shows up in that playlist right away, and playing the playlist straight after includes it. YouTube acknowledges an add immediately but takes a few seconds to list it, so opening the playlist at once used to show the old tracklist while the count had already moved. The TUI now remembers what it just added and merges it into the fetched list until YouTube shows it (likes at the top of Liked Music, adds at the end of a playlist). The count in the playlists pane can no longer drop below what it showed plus what was just added.

## 0.5.13 — 2026-09-06

- The volume label sits right after the UP NEXT column on the heading row of the player strip. In 0.5.11 it was pinned to the strip's far right edge, which on a wide terminal is a long way from anything else and was reported as missing.

## 0.5.12 — 2026-09-06

- Stale browser cookies fix themselves. Google rotates the browser's session tokens about daily, after which YouTube answers ytm's copy with the signed-out page: empty library, no playlists, mixes that fail to open. `ytm auth --from-browser` now records which browser and profile the cookies came from (`auth.source.json`), and the first request that comes back signed out (or 401/403) re-extracts the cookies from that browser and retries by itself. If the browser is signed out too, the error says the automatic re-extraction failed and why. Pasted headers and OAuth are left alone. Run `ytm auth --from-browser` once after upgrading so the source is recorded.

## 0.5.11 — 2026-09-06

- The volume is now the system's output volume. ytm used to change mpv's own software volume, which the desktop never saw: `vol 70` in ytm next to 55 % in the tray, and the keyboard's volume keys moved the tray while ytm's number stayed put. `+`/`-` and `ytm volume` now set the default output through `wpctl` (PipeWire) or `pactl` (PulseAudio), and changes made anywhere else (media keys, the tray slider) appear in the TUI within a moment. mpv's own volume is pinned to 100. `control = "player"` under `[audio]` restores the old behaviour, and it is also what you get when neither tool is installed.
- The volume indicator moved from the end of the progress row to the top-right corner of the player strip, in the space beside the PLAYED / UP NEXT columns.
- `=` raises the volume like `+`, so the key works unshifted.

## 0.5.10 — 2026-09-06

- Opening a mix after the browser cookies have gone stale now says the credentials are signed out and to run `ytm auth`, like the library and mix list already did. YouTube serves the anonymous page for a personal mix, ytmusicapi fails a field lookup, and the TUI banner used to show the whole raw response.

## 0.5.9 — 2026-09-06

- `ytm auth` can pick which Google account to use when the browser is signed in to several: `--authuser N` on the command line (0 is the first account), or `x-goog-authuser` under `[auth]` in `config.toml` as the default. Previously the first account was always used. Config option contributed by @paul-sx (#29).

## 0.5.8 — 2026-09-05

- The PLAYED / UP NEXT columns' maximum width is configurable: `queue_column_width` under `[tui]` in `config.toml`, default 40 cells, `0` for no cap. Negative values are rejected with the usual config warning. Contributed by @paul-sx (#31).

## 0.5.7 — 2026-09-05

- `ytm auth --from-browser` with several browser profiles: the profile that is actually logged in to YouTube is used, instead of whichever one the browser saved last. yt-dlp's "newest cookie file" rule picked the wrong profile and ytm then said "no YouTube login" for a browser that had one; System and Guest profiles are skipped. `--profile NAME` ("Default", "Profile 1", a Firefox profile folder) selects one explicitly. Reported by @nikbrunner (#27).
- When the extracted cookies could not be checked against YouTube Music because the connection failed (for example a broken IPv6 route, which hangs until the 30 s timeout), the error now says it is a network problem rather than a login problem.

## 0.5.6 — 2026-09-04

- `h` hides the search box and results so the queue, playlists and lyrics get the whole screen while you listen; `s`, `/` or `h` bring them back. Not remembered across runs. Suggested by @paul-sx (#30).
- The PLAYED / UP NEXT columns in the player strip size themselves to the terminal: up to four tracks per side with the artist when there is room, one per side in compact mode where they now stay visible in place of the artist line. Columns are capped at 40 cells so they stay side by side. Contributed by @paul-sx (#28).

## 0.5.5 — 2026-09-04

- `ytm auth --from-browser helium` reads cookies from the Helium browser (macOS, Linux, Windows), and auto-detection tries it after the mainstream browsers. Helium stores its profile under `net.imput.helium` and, on macOS, names its Keychain item "Helium Storage Key", neither of which yt-dlp knows; ytm supplies both and reuses yt-dlp's Chromium decryption. Fixes #27.

## 0.5.4 — 2026-09-04

- Expired or signed-out browser cookies are reported instead of silently hiding your playlists and mixes. YouTube answers stale cookies with the signed-out page (empty library, no mixes) rather than an error, so `ytm mix` said "no mixes available" and the TUI showed only local playlists. Both now say the credentials are signed out and to run `ytm auth` again; the TUI keeps the local playlists visible.
- Volume keys work again, from anywhere. `+` and `-` are handled before the search box, so they change the volume instead of being typed, and the search box's `-` in a query is no longer needed for hyphenated names (YouTube ignores it).
- The TUI no longer freezes while a YouTube call is in flight. One lock used to cover every backend request, so pressing a key during the startup playlist load or a search waited seconds for it; the lock now sits on the mpv connection only. The volume indicator is seeded in about 1 s instead of 5.

## 0.5.3 — 2026-09-04

- The progress line resets to 0:00 of the new song's length the moment the track changes, instead of showing the old song's position for the two seconds mpv needs to resolve the stream.
- Position updates are sent to the TUI once per second instead of a dozen times, so the strip re-renders 12x less often.

## 0.5.2 — 2026-09-04

- Mixes no longer re-roll on every play. YouTube generates a fresh tracklist each time a mix is fetched, so playing one twice queued different songs than the pane had shown. The TUI now keeps each mix's tracklist for the session; `r` refreshes all mixes at once.

## 0.5.1 — 2026-09-04

- The player strip's PLAYED / UP NEXT columns are uppercase and sit side by side instead of at opposite ends of the strip.
- README screenshot shows the 0.5 layout.

## 0.5.0 — 2026-09-04

- Mixes: `ytm mix` lists your daily mixes (Supermix, Discover Mix, Replay Mix, ...), `ytm mix <name>` plays one, and they appear below your playlists in the TUI.
- Compact layout: below 100 columns or 24 rows the TUI keeps only the search box, the queue and a 4-row player strip, so it fits a tmux pane. Restores itself when the terminal grows.
- The player strip shows the last two played and the next three queued songs beside the cover.
- Search columns take fixed proportions of the width and truncate with an ellipsis instead of scrolling sideways.

## 0.4.0 — 2026-09-03

- Play next: `u` in the TUI puts the highlighted song right after the one playing; `ytm add --next <song>` does the same from the shell. A song already in the queue is moved up instead of duplicated.

## 0.3.3 — 2026-09-03

- Documentation release: the Authentication section covers all three sign-in methods, the manual-header steps, and the Google Cloud OAuth client walkthrough.

## 0.3.2 — 2026-09-03

- `ytm auth --from-browser` explains why each browser failed (not installed, cookies could not be decrypted, database locked, no YouTube login) instead of a blanket "not logged in".
- Windows: Chromium browsers (Chrome 127+, Edge, Brave, Vivaldi, Opera) use App-Bound Encryption and cannot be read. Firefox is tried first there and the error says to use Firefox, `--manual` or `--oauth`.

## 0.3.1 — 2026-09-03

- `ytm update` works in environments without pip (for example `uv venv` + `uv pip install ytm`): it upgrades through `uv pip` aimed at the running interpreter, and says what to run if neither pip nor uv exists.

## 0.3.0 — 2026-09-03

- `ytm update`: upgrades ytm and yt-dlp through whatever installed them (pipx, `uv tool`, pip). `--check` only reports, `--force` reinstalls.
- The TUI checks PyPI once a day and shows a toast when a newer release exists. `[update] auto = true` installs it automatically.
- `ytm --version`.
- Search results appear as you type; Enter still plays the first one.
- Add to playlist is a two-step flow: `a` on a song, pick the list, `a` or Enter. Adding to Liked Music works (it likes the song). Counts refresh at once.
- All shortcuts are lowercase: `a` add, `q` enqueue, `l` playlists, `x` exit and stop.
- The queue cursor follows the playing track until you move it.

## 0.2.0 — 2026-09-03

- First PyPI release. mpv is the only background process, driven over JSON IPC.
- Textual TUI with cover art, lyrics, queue, playlists, clickable everything.
- Radio autoplay from inside mpv; no duplicate tracks in the queue.
