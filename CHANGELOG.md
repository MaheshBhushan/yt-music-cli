# Changelog

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
