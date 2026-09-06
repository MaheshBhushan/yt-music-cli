# Changelog

## Unreleased

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
