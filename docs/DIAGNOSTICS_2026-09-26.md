# YTM diagnostic audit — 2026-09-26

## Summary

The existing suite passes, but targeted fault injection reproduced six defects and identified one playback/download configuration mismatch. Three findings deserve high priority: a stuck authenticated-client initialization flag, local-playlist lost updates, and cookie-export races with logout.

This audit is diagnostic only. It does not change application behavior, authenticate to Google, read actual cookie values, mutate the user's playlists, or control the running player. The previous duplicate-click fix remains in the working tree. Temporary test logs use `/tmp/ytm-diagnostics-*` so the normal TUI log is preserved.

## Checks performed

| Check | Result |
|---|---|
| Full pytest suite | **729 passed**, 2 dependency deprecation warnings, 94.95 seconds |
| `code-health check --full` | **0 errors, 119 warnings** |
| `uv pip check --python .venv/bin/python` | All 41 installed packages compatible |
| `ytm --help`, `ytm login --help` | Successful |
| Account-client failure injection | Reproduced stuck initialization flag |
| Concurrent local-playlist creation | Requested 2 creates, persisted 1 |
| Corrupt local-playlist mutation | Original corrupt content silently replaced |
| Logout during cookie export | Logged-out record plus recreated cookie file |
| Default TUI startup failure injection | Plain `ytm` propagates exception; `ytm tui` returns status 1 |
| mpv event injection | `end-file` error ignored; next property update returned |
| Download JavaScript runtime inspection | Only Deno enabled despite Node being available and Deno absent |

Installed environment: YTM 0.10.0, ytmusicapi 1.12.2, yt-dlp 2026.8.19, Textual 8.2.8, Playwright 1.63.0. mpv and Node are available; Deno is absent.

The tests and compatibility check are not a dependency vulnerability audit. They also do not certify live Google login, live YouTube extraction or other operating systems.

## Findings ordered by priority

### D1 — High: account-client initialization can become permanently stuck

**Location:** `ytm/music.py:234–242`, especially the second `_auth_stamp(path)` call at line 237.

**Trigger:** Client construction succeeds, then reading the credential stamp raises—for example, the file becomes unreadable or malformed between the initial and final stamp reads.

**Root cause:** The final stamp comparison is outside the exception handler that resets `_CLIENT['building']`. An exception bypasses both resetting that flag and notifying waiting threads. Future calls see `building=True` and wait on the condition indefinitely. `reset_client()` also does not clear that flag.

**Reproduction:** Replace `_auth_stamp` with a fake returning one valid revision and then raising `AuthStorageError`; fake client construction succeeds. After the exception, `_CLIENT['building']` is still `True`.

**Impact:** Account-backed TUI operations can appear frozen until the process is restarted. This is a plausible restart-requiring failure mode, but there is no evidence tying this injection scenario to the user's earlier real incident.

**Suggested fix:** Put construction, final revision validation and publication inside a structure that always releases the builder state and notifies waiters. Keep the generation check so a stale builder cannot publish after logout. Add a test that a second request recovers after the final stamp read fails; testing only constructor failures misses this case.

### D2 — High: concurrent local-playlist mutations lose data

**Location:** `ytm/playlists_local.py:130–144` and other load/modify/save operations; `save()` at lines 78–90.

**Trigger:** Two requests read the same local-playlist store before either writes its change. These can come from background TUI work or separate application instances.

**Root cause:** Atomic rename makes each individual file replacement atomic, but the complete read/modify/write operation has no transaction lock. Both callers change independent copies; the later write replaces the earlier caller's result. Within one process, the PID-only temporary filename is also shared across threads and can cause competing writers to interfere.

**Reproduction:** Two worker threads call `create()` against a temporary store. A barrier makes both finish loading before either saves. Saves are deliberately serialized in the diagnostic to isolate lost updates from temporary-file collisions. Both calls complete, but only one playlist remains.

**Impact:** Successfully acknowledged local playlist creation, additions or edits can disappear.

**Suggested fix:** Lock the whole read/modify/write transaction across threads and processes, use unique temporary filenames, and run mutation tests under concurrent access. A lock around only `save()` is insufficient: the reproduction already serializes saves and still loses data.

### D3 — High: concurrent cookie export can recreate credentials after logout

**Location:** `ytm/auth.py:1025–1044`, `cookies_file()`.

**Trigger:** Cookie export reads browser headers, logout then commits its tombstone and deletes managed credentials, and the original exporter subsequently writes the previously read headers to `cookies.txt`.

**Root cause:** Export operates outside the session-store transaction and does not verify the captured revision before writing. Its later `active_record()` check treats a logout tombstone as an existing record when choosing the timestamp source; it does not reject the stale export.

**Reproduction:** In a temporary configuration directory, save a synthetic browser session. Inject logout immediately after the export function reads the header. At completion, `session.json` has `method: none`, but `cookies.txt` exists again.

**Impact:** Sensitive local credential material can remain after a reported logout. This does **not** reactivate the authenticated ytmusicapi record. A stale export may also be handed to an external playback consumer. The path is relevant when cookie export is requested, including authenticated streaming; ordinary anonymous playback does not export account cookies by default.

**Suggested fix:** Coordinate cookie generation and logout with the same store lock and revision check. Write the export atomically, refuse a tombstone, and reject a snapshot that changed while preparing the export. Add a two-thread export/logout test.

### D4 — Medium: playback failures are invisible to the TUI

**Location:** `ytm/player.py:532–543` and the property-only consumption loop in `ytm/tui/backend.py`.

**Trigger:** mpv reports an asynchronous stream failure using an `end-file` event with `reason: error`.

**Root cause:** `Player.observe()` yields only `property-change` messages. It ignores playback failure events. A successful `loadfile` command reply acknowledges the command; it does not prove that stream resolution or audio playback succeeded.

**Reproduction:** Feed the observer a synthetic `end-file` error followed by a pause-property event. The caller receives only `('pause', False)`; no playback error is surfaced.

**Impact:** Failed extraction, inaccessible media or another asynchronous playback failure can leave the UI looking idle/silent without an actionable error. This contributes directly to users trying restarts without knowing what failed.

**Suggested fix:** Forward lifecycle/error events through a typed event path while preserving property observers. Display a sanitized error linked to the affected track. Treat deliberate stop/replacement differently from an actual stream failure. Avoid automatically retrying every event, which could create loops.

### D5 — Medium: corrupt local playlists can be silently overwritten

**Location:** `ytm/playlists_local.py:51–58` and mutation callers such as `create()` at line 132.

**Trigger:** The playlist file contains invalid JSON or has the wrong top-level shape, and the user subsequently performs a mutation.

**Root cause:** `load()` converts unreadable/corrupt storage into an empty store. Writers cannot distinguish that recovery view from a genuinely new empty store, so the next successful mutation overwrites the existing file.

**Reproduction:** Write `{broken` into a temporary playlist file, call `create()`, then inspect it. The damaged original is replaced with a fresh one-playlist store without an error or backup.

**Impact:** Data that might have been recoverable is lost, and the user has no explanation for missing playlists.

**Suggested fix:** Tolerant reads may keep the UI usable, but mutation must distinguish missing files from corruption or permission failure. Refuse destructive replacement or preserve a recoverable backup before explicit repair.

### D6 — Medium: plain `ytm` bypasses startup error handling

**Location:** `ytm/cli.py:969–977`.

**Trigger:** The default TUI command raises a normal startup `PlayerError`, such as unavailable mpv or failed player launch.

**Root cause:** `cmd_tui(args)` for a missing subcommand executes before the main `try` block. The explicit `ytm tui` command executes inside it.

**Reproduction:** Mock `cmd_tui` to raise `PlayerError`. `main([])` leaks the exception; `main(['tui'])` returns exit status 1 with a clean error message.

**Impact:** The most common entry point can produce a traceback for an expected environment failure, while its equivalent explicit command behaves correctly. Keyboard interruption during that startup path is also outside the shared handler.

**Suggested fix:** Route default and explicit TUI invocation through the same exception-handling boundary while retaining the correct success exit code.

### D7 — Medium, confirmed configuration mismatch: downloads omit Node runtime selection

**Location:** `ytm/cache.py:178–195`, compared with `_js_runtime()` and player construction in `ytm/cli.py:53–57,104`.

**Trigger:** A system has Node but no Deno, and a YouTube download requires JavaScript challenge evaluation.

**Root cause:** Playback explicitly selects an available JavaScript runtime. The direct Python yt-dlp download path does not supply `js_runtimes`, so it retains the installed yt-dlp default configuration.

**Local evidence:** Node is available, Deno is absent, playback's selector returns Node, and constructing `YoutubeDL(cache._ydl_opts(...))` yields only `deno` in the enabled runtime configuration.

**Impact:** Playback and offline download can behave differently on the same installation. A track needing JavaScript evaluation may download unreliably despite playing through mpv. No live network download was performed, so this report does not claim a measured extraction failure for a specific video.

**Suggested fix:** Share runtime discovery between playback and downloads and test a Node-only environment. Keep cookie/PO-token policy separate from runtime selection.

## Minor issues and limitations

1. **Diagnostics disappear on restart.** TUI logs are truncated at startup; the mpv log uses a fixed path. Retaining a small rotated history would help distinguish the failed attempt from the eventual successful run. Do not retain raw authentication headers or resolved signed media URLs unnecessarily.
2. **Dependency deprecation warning.** Two tests exercise `textual_image` code calling Pillow's deprecated `Image.getdata()`. This is an upstream compatibility warning, not a current test failure.
3. **119 static warnings.** Findings include unused variables/imports, import ordering, broad exception handlers and style suggestions. The tool classifies some stylistic rules as security findings; that label alone is not evidence of an exploitable defect. Review relevant findings individually.
4. **Browser compatibility remains conditional.** Unit tests cover routing/discovery with mocks. Real macOS/Windows login, browser cookie encryption, Google acceptance of isolated Playwright login, and portable/sandboxed browser distributions remain unverified. Native login requires terminal confirmation; it is not automatic page observation.
5. **Project memory is not initialized.** The configured recall command returned `PROJECT_NOT_INITIALIZED`. This affects historical agent context, not YTM runtime behavior.

## Existing duplicate-click fix

The previously reproduced duplicate dispatch is fixed in the current working tree. Exact-count tests now require one playback request per click. Repeated clicks, Enter, changing columns within a row and header selection are covered. The complete suite above includes that fix. These new findings should not be confused with a recurrence of the same click bug.

## Recommended implementation order

1. Fix D1's stuck initialization state and D4's missing playback errors to improve reliability and make future playback failures observable.
2. Fix D2 and D5 together as a local-playlist storage transaction/recovery change.
3. Fix D3 before describing logout as reliably removing every managed credential copy under concurrency.
4. Unify default TUI error handling (D6) and JavaScript runtime selection (D7).
5. Add safe log rotation and address remaining warnings according to their actual impact.

## Reproduction artifacts

During this audit, `/tmp/ytm_diagnostic_repros.py` used only temporary credential/playlist files and synthetic data. Its output was:

```text
client_building_flag_left_set: True
local_playlist_creates_requested: 2; persisted: 1
corrupt_playlist_file_silently_replaced: True
logged_out_but_cookie_export_recreated: True
startup [] leaks_exception: True
startup ['tui'] returned: 1
```

The separate synthetic mpv-event probe returned:

```text
event returned after injected playback failure: ('pause', False)
```

Temporary files are not durable fixtures. When implementing fixes, convert these scenarios into regression tests and show each failure before the change and success afterward.
