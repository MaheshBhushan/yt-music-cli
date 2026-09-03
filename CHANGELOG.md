# Changelog

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
