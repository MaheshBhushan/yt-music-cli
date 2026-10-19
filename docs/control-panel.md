# Local playback control panel

The React and TypeScript panel controls a Python `ytm.tui.backend.Backend`
session. The backend owns a separate mpv player. The server stops that player
when you press Ctrl+C. Closing a browser tab keeps the server and audio running.
One-shot CLI and Textual sessions keep their own players.

## Install and run

Use Python 3.11+, Node 22.12+ or 24+, and mpv on your PATH. Run these commands
from the repository root:

```sh
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
npm --prefix control-panel ci
npm --prefix control-panel run build
python -m ytm.panel
```

On Windows, activate the environment with `.venv\Scripts\Activate.ps1`.
Open <http://127.0.0.1:8765>. Use `--port 9000` to change the port or
`--assets /absolute/path/to/dist` to serve a different panel build.
The server checks the asset directory before starting mpv. Rebuild after
changing the frontend. Node is needed to build the panel; Python serves the
compiled files at runtime.

Search for songs or artists, play a result, append it to the queue, or insert
it after the current song. Queue rows support play, move up/down, and remove.
“Clear upcoming” keeps the current song. Playback controls support previous,
next, pause/resume, relative seek, and volume. Volume follows the existing
Python audio configuration. The panel refreshes player state once per second.
Errors appear above the search and queue sections. Reconnect reloads the panel.

Search uses the public catalogue. Streaming needs network access and the same
mpv/yt-dlp prerequisites as the CLI. The panel adds no account or sign-in flow.

## HTTP interface

The server binds to `127.0.0.1` only. It accepts local Host headers and
same-origin browser requests, sends no CORS headers, and requires JSON for
commands. Serve the panel through Python; opening `index.html` directly does
not provide the API. Do not expose this local control server through a tunnel
or reverse proxy.

- `GET /api/status` returns `{ "status": ..., "queue": ... }` from the backend.
- `POST /api/command` accepts `{ "cmd": "search", "args": { "query": "..." } }`.
- Supported commands are `search`, `play`, `enqueue`, `enqueue_next`, `pause`,
  `resume`, `toggle`, `next`, `prev`, `seek`, `volume`, `queue_play`,
  `queue_move`, `queue_remove`, and `queue_clear`.
- Track commands accept the existing `Track` fields. Queue indices start at
  zero. Seek uses relative `seconds`; volume uses `level` from 0 to 100.
- Errors return `{ "error": "..." }` with HTTP 400 for invalid input, 403 for
  nonlocal origins/hosts, 415 for non-JSON commands, or 502 for backend failures.

## Deterministic checks

Install the Python dev dependencies and frontend packages first. Install
Playwright's test browser once:

```sh
npm --prefix control-panel exec -- playwright install chromium
```

Run the checks from the repository root:

```sh
.venv/bin/python -m pytest -q tests/test_panel.py tests/test_tui_backend.py
npm --prefix control-panel run build
npm --prefix control-panel test
```

The browser check serves the production build through the actual Python HTTP
adapter on port 8766. It uses the real backend with a deterministic catalogue
and an in-memory mpv fixture. It covers search, duplicate queue entries,
reordering, removal, clearing, transport controls, empty results, provider
failures, reconnection, and a narrow viewport. It does not contact YouTube,
read credentials, or start audio. `YTM_TEST_PYTHON` can select another Python
interpreter with the project's dev dependencies installed. HTTP tests also
exercise malformed input, local origin checks, and static asset confinement.

The fixtures verify control behavior. They do not verify live YouTube stream
resolution or physical audio output.
