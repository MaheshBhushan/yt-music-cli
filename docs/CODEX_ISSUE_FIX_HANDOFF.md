# YTM CLI: complete issue-fix handoff for Codex

**Prepared:** 2026-09-27 (from diagnostics run 2026-09-26)  
**Repository:** https://github.com/MaheshBhushan/yt-music-cli  
**Purpose:** Give this single file to another Codex session or developer to implement the diagnostic fixes, including the minor findings.  
**Status:** Implementation instructions, not a claim that the listed defects have been fixed. The earlier duplicate-click fix already exists in the reviewed working tree and must be preserved.

## Navigation

- [1. Copy-and-paste assignment for the receiving agent](#1-copy-and-paste-assignment-for-the-receiving-agent)
- [2. Scope, baseline and how to use this file](#2-scope-baseline-and-how-to-use-this-file)
- [3. First actions in a fresh checkout](#3-first-actions-in-a-fresh-checkout)
- [4. Invariants that every change must preserve](#4-invariants-that-every-change-must-preserve)
- [5. Source and test map](#5-source-and-test-map)
- [6. Recommended implementation order and boundaries](#6-recommended-implementation-order-and-boundaries)
- [7. D1: recover from every account-client builder failure](#7-d1-recover-from-every-account-client-builder-failure)
- [8. D2 and D5: transactional, corruption-aware local playlists](#8-d2-and-d5-transactional-corruption-aware-local-playlists)
- [9. D3: make cookie export participate in logout ordering](#9-d3-make-cookie-export-participate-in-logout-ordering)
- [10. D4: surface asynchronous playback errors without false alarms](#10-d4-surface-asynchronous-playback-errors-without-false-alarms)
- [11. D6: one exception boundary for both TUI entry points](#11-d6-one-exception-boundary-for-both-tui-entry-points)
- [12. D7: unify playback and download JavaScript runtime selection](#12-d7-unify-playback-and-download-javascript-runtime-selection)
- [13. M1: retain useful diagnostics across restarts](#13-m1-retain-useful-diagnostics-across-restarts)
- [14. M2: resolve or explicitly track the Pillow/textual-image warning](#14-m2-resolve-or-explicitly-track-the-pillowtextual-image-warning)
- [15. M3: triage static warnings by actual impact](#15-m3-triage-static-warnings-by-actual-impact)
- [16. M4: browser/OS compatibility validation and honest documentation](#16-m4-browseros-compatibility-validation-and-honest-documentation)
- [17. M5: project-memory setup is optional tooling work](#17-m5-project-memory-setup-is-optional-tooling-work)
- [18. Test isolation and reproducibility rules](#18-test-isolation-and-reproducibility-rules)
- [19. Suggested regression names and expected evidence](#19-suggested-regression-names-and-expected-evidence)
- [20. Validation commands and completion gates](#20-validation-commands-and-completion-gates)
- [21. Reviewer checklist: actively look for these failed fixes](#21-reviewer-checklist-actively-look-for-these-failed-fixes)
- [22. Expected final report from the receiving Codex session](#22-expected-final-report-from-the-receiving-codex-session)
- [Appendix A. Original diagnostic evidence](#appendix-a-original-diagnostic-evidence)

## 1. Copy-and-paste assignment for the receiving agent

> Work in the YTM CLI repository. Read this entire handoff, inspect the actual current source and local instructions, and implement the confirmed diagnostic fixes D1–D7. Address the minor findings M1–M5 as described, distinguishing application bugs from upstream warnings, compatibility limitations and optional development-tool setup. Preserve the existing authentication redesign, anonymous public commands, OAuth compatibility, and duplicate-click fix. For each behavioral defect, reproduce it with an isolated regression test before changing the implementation, then demonstrate that the test passes after the fix. Use temporary files and fake credentials; never inspect or overwrite a real user's browser session, playlists or authentication files. Do not publish, push, create external issues, or change machine configuration as part of this task. Finish with a reviewed patch, appropriate passing tests, a disposition for every finding, and updated documentation. Do not claim real Google sign-in or cross-platform support was tested unless it actually was.

The receiving agent should proceed through authorized repository fixes without repeatedly asking for approval. Ask only when a real missing requirement blocks a decision. Ordinary testing, reversible source edits and local documentation are part of this assignment.

## 2. Scope, baseline and how to use this file

This handoff is self-contained. It includes the diagnostic evidence, source map, implementation constraints, regression designs, suggested work order and completion checklist. The original diagnostic report is reproduced later as an evidence appendix; there is no dependency on having access to the author's `/tmp` directory or earlier chat messages.

### Baseline observed when preparing this handoff

- Repository HEAD: `5bdcaa19082f0d62fd42a7a84fd5104196b41f8e`.
- Package version: 0.10.0.
- Python minimum declared by the repository: 3.11.
- Last full diagnostic run: **729 passed**, two existing dependency deprecation warnings, 94.95 seconds.
- Full static scan: **0 errors, 119 warnings**.
- Installed-package compatibility check: all 41 installed packages compatible.
- Runtime versions observed: ytmusicapi 1.12.2, yt-dlp 2026.8.19, Textual 8.2.8, Playwright 1.63.0.
- The audited machine has mpv and Node; Deno is absent.
- These are historical baseline facts. Recheck the receiving checkout; do not assume it is identical.

### Existing working-tree changes to preserve

At preparation time, these files were modified or untracked:

```text
 M CHANGELOG.md
 M tests/test_tui.py
 M ytm/tui/widgets.py
?? tests/test_tui_widgets.py
?? docs/DIAGNOSTICS_2026-09-26.md
?? dist/
```

The three tracked changes and widget test include the previous duplicate-click fix. Do not reset them, overwrite them with upstream versions, or treat them as disposable generated changes. Do not modify or delete `dist/` merely because it is untracked.

If the receiving checkout lacks that fix, inspect `SelectOnClickTable._on_click`: it needs to prevent a second automatic inherited Textual handler invocation when explicitly invoking the parent. Preserve header selection, same-row/different-column selection, repeated clicks and Enter behavior. Existing tests must assert exactly one playback request, not merely membership in a request list.

### No claim of exhaustive coverage

Passing the existing suite did not catch the defects below. The audit reproduced six behavioral bugs and confirmed one runtime-configuration mismatch. It did not prove that any specific earlier user restart was caused by D1, and it did not perform a live download proving D7 on a particular song.

## 3. First actions in a fresh checkout

1. Read applicable `AGENTS.md` and other repository instructions.
2. Inspect `git status --short`, the current commit and recent relevant changes.
3. Identify pre-existing user edits and retain them.
4. Read the implementation files listed in section 5 before changing them.
5. Check the project's existing environment and test commands. Prefer the existing virtual environment when available.
6. Run a baseline test pass with an isolated TUI trace path. Save the result so new failures are distinguishable from old ones.
7. Reproduce each issue in its own test or coherent test group.
8. Work in the dependency order below. Do not start with a broad formatting sweep.

Example commands from the original checkout:

```sh
git status --short
git rev-parse HEAD
YTM_TUI_LOG=/tmp/ytm-handoff-baseline.log .venv/bin/python -m pytest -q
uv pip check --python .venv/bin/python
```

On another machine, adapt the interpreter and temporary path. `python -m ytm` is not the supported entry point in this checkout because there is no `ytm.__main__`; use the installed `ytm` console script or import `ytm.cli.main` in tests.

If the optional `code-health` tool is installed, follow its skill instructions and run changed-file checks after code edits. A full scan is appropriate once for this audit baseline. If it is unavailable, record that fact and use the repository's available lint tools; do not silently claim the check passed.

## 4. Invariants that every change must preserve

### Authentication and privacy

- Search, public metadata, public playlists, radio discovery and lyrics must remain usable anonymously.
- Public client creation must not read credential storage or initialize OAuth.
- Never print real or synthetic secret values as part of diagnostic failure output. Use sentinel-presence assertions instead.
- Do not ask for or automate a Google password.
- Failed/cancelled login must preserve the previous active session.
- Logout must remain local: no global Google logout, token revocation or browser-profile deletion.
- A session tombstone must continue to block legacy credential fallback.
- Account mutations must not be blindly retried after ambiguous network errors.
- Preserve OAuth users and managed OAuth generation storage.

### Playback and TUI

- One click must result in one selection/playback request.
- Loading a track and confirming audible playback are different events.
- Explicit stop, skip and playlist replacement must not generate false error banners.
- UI updates must occur through the existing message/event path, not by modifying widgets from background threads.
- Closing the TUI must still terminate only its owned player/processes.
- Do not change the system audio device, volume, power policy or background services during tests.

### Storage and concurrency

- No acknowledged mutation should be silently lost.
- Do not replace corrupt user data with an empty store without an explicit recovery policy.
- Atomic rename is not a substitute for a lock around read/modify/write.
- Every lock has a documented scope and consistent acquisition order.
- Every claimed builder/lock state is released on failure.
- New code must not hold a global account-client lock while performing HTTP operations.
- Synthetic concurrency tests use events, barriers and bounded joins, not arbitrary sleeps.

## 5. Source and test map

| Area | Source to inspect | Existing tests to extend |
|---|---|---|
| Authenticated client caching | `ytm/music.py`: `shared_client`, `reset_client`, `_auth_stamp`, `_CLIENT_READY` | `tests/test_caching.py`, `tests/test_auth_review.py` |
| Local playlist persistence | `ytm/playlists_local.py`: `load`, `save`, `create`, `add_items`, `remove_items`, `delete` | `tests/test_playlists.py`, `tests/test_tui_backend.py` |
| Existing storage locking patterns | `ytm/state.py`, `ytm/authentication/storage.py` | `tests/test_caching.py`, `tests/test_auth_storage.py` |
| Browser cookie export/logout | `ytm/auth.py`: `cookies_file`, `load_cookies`, `_write_text_0600`; `SessionStore.logout_and_cleanup` | `tests/test_core_state_music_auth.py`, `tests/test_auth_storage.py`, `tests/test_auth_review.py` |
| mpv IPC and event stream | `ytm/player.py`: `observe`, command/reply handling | `tests/test_mpv_player.py` |
| Backend/UI event delivery | `ytm/tui/backend.py`: `listen`, `_emit`; `ytm/tui/app.py`: daemon-event handling | `tests/test_tui_backend.py`, `tests/test_tui.py`, `tests/test_lifecycle.py` |
| CLI default dispatch | `ytm/cli.py`: `main`, `cmd_tui` | `tests/test_cli_core.py` |
| Runtime selection/downloads | `ytm/cli.py`: `_js_runtime`; `ytm/cache.py`: `_ydl_opts`, `download` | `tests/test_cache.py`, `tests/test_cli_core.py`, `tests/test_mpv_player.py` |
| Logging | `ytm/tui/app.py`: `_trace`; `ytm/cli.py`: `_log_path`, player options | Relevant TUI/CLI tests and new focused logging tests |
| Dependency warning | `pyproject.toml`, cover-image code and installed `textual_image` | Existing cover-art tests in `tests/test_tui.py` |
| Browser/platform contract | `ytm/authentication/browser_profiles.py`, `browser_login.py`, README | `tests/test_browser_profiles.py`, `tests/test_browser_login.py`, auth tests |

Line numbers in the evidence appendix are anchors from the audited tree, not a substitute for locating the current function.

## 6. Recommended implementation order and boundaries

| Step | Work package | Depends on | Exit condition |
|---|---|---|---|
| A | D1 account-client builder recovery | Baseline | Failure releases waiters and a later request succeeds |
| B | D4 playback-error event delivery | Existing click fix understood | Realistic synthetic mpv error reaches safe UI banner |
| C | D2 + D5 local-playlist transactions and corruption handling | Storage contract agreed | Concurrent updates survive; damaged files preserved |
| D | D3 cookie export/logout coordination | Auth storage locking understood | No stale export remains after completed logout |
| E | D6 default CLI exception boundary | None beyond baseline | Plain and explicit TUI startup have equal error behavior |
| F | D7 shared runtime selection | Installed yt-dlp API inspected | Playback/download runtime choices agree |
| G | M1–M5 minor findings | Core fixes stable | Each minor item fixed, verified, or explicitly dispositioned |
| H | Integration, documentation and review | A–G | Full required checks pass and claims match evidence |

These are logical work packages, not a request to spawn agents. Keep implementation small and cohesive. A new generic storage framework, database migration, process supervisor, or rewritten API client is not required to solve these defects.

## 7. D1: recover from every account-client builder failure

### Problem to reproduce

`shared_client()` claims `_CLIENT['building']`, constructs a client, then compares the credential stamp a second time. Failure in the final stamp read occurs outside the cleanup handler. The building flag remains set, so later callers wait indefinitely.

### Required behavior

1. Exactly one builder may own initialization at a time.
2. A builder publishes only if its generation and credential revision are still current.
3. Every exit path after claiming builder ownership clears that ownership and notifies waiters.
4. An error is propagated safely to the caller; it is not cached as a successful client.
5. A subsequent call can attempt construction and succeed.
6. `reset_client()` during construction must not permit a stale builder to publish.

### Suggested implementation shape

Use `try/finally` covering **both construction and final validation/publication**, not only the network constructor. Release ownership under `_CLIENT_READY` in the `finally` block and notify all waiters. Keep construction outside the lock.

Pseudocode—not a drop-in patch:

```text
loop:
    read current credential stamp
    under condition:
        return matching cached client if present
        if another builder owns initialization:
            wait and loop
        claim builder ownership and capture generation

    try:
        build client outside condition
        under condition:
            verify generation and current credential stamp
            publish only if unchanged
        return published client, otherwise loop
    finally:
        under condition:
            release this builder's ownership
            notify waiters
```

If the implementation changes to allow overlapping builders after reset, use an ownership token: an older builder must not clear a newer builder's state. The existing single-builder design can stay simpler. Do not “fix” the issue by unconditionally resetting the flag from unrelated callers.

### Regression matrix

- Constructor raises: flag clears; second call retries.
- Final `_auth_stamp` raises `AuthStorageError`: flag clears; second call retries.
- Final stamp raises malformed-record error: same recovery.
- A second thread already waiting is released when the builder fails.
- Logout/reset changes generation during construction: stale client is not published.
- Concurrent initial requests still construct one usable client for the stable generation.
- Anonymous client calls remain independent of this state.

Avoid a permanently blocked test process: bound waits, clean up the shared state in `finally`, and ensure every worker terminates. An assertion that the flag is false is useful, but also prove the next call works.

## 8. D2 and D5: transactional, corruption-aware local playlists

### Separate the two defects

D2 is a concurrency problem: two valid snapshots overwrite each other's edits. D5 is a recovery problem: unreadable data is treated as a writable empty database. Atomic replacement alone solves neither.

### Recommended storage policy

- Missing file: a new empty store is valid.
- Valid file: read, validate, mutate and save under one transaction.
- Invalid JSON, wrong shape or invalid persisted entries: preserve the original bytes and report a concise storage error before mutation.
- Permission/I/O errors: preserve the file and report the access failure, not an empty library.
- Read-only display may remain tolerant if it clearly carries an error/warning state; never pass a fallback empty view into a mutation as authoritative storage.

Default to **refusing mutation on corruption**, which is the smallest safe behavior. An automatic backup/recovery UI is optional and must be deliberately designed; do not silently create a new empty file and call the problem solved.

### Transaction design

Add a small internal mutation helper, for example `_mutate(path, operation)`:

```text
resolve path
acquire in-process lock and cross-process file lock in a consistent order
strictly load and validate the latest on-disk store
apply operation to that store
serialize to a unique same-directory temporary file
flush and fsync the file
atomically replace target
release locks in finally
return operation result
```

Inspect `ytm/state.py` and `SessionStore` for existing POSIX/Windows lock patterns. Reuse a helper only if its contract fits; do not import an unrelated private auth lock and accidentally couple local playlists to Google credentials.

Important decisions:

- Use a stable sidecar lock file, not the JSON file being replaced.
- Do not unlink a lock file while another process may hold it.
- Ensure same-process threads are serialized as well as separate processes.
- Use a unique temporary name per write, not only the process ID.
- Keep serialization/fsync/replace failures from deleting the previous good file.
- Clean up only the temporary file created by the failing operation.
- Do not hold this lock across network requests or mpv calls.
- Keep the existing playlist schema and IDs compatible.

Apply the helper to **all** mutators: create, add items, remove items and delete. Search the tree for direct `save()` callers and establish whether each is a transaction, explicit whole-store replacement or test helper. Do not leave an unsafe mutation escape hatch by changing only `create()`.

### Validation depth

Current tolerant normalization may skip malformed entries. Strict mutation validation must not silently discard those entries and then overwrite the original. Decide and document which historical optional fields are valid; preserve supported old data. Distinguish unknown optional keys from structurally broken records so compatibility is not destroyed in the name of strictness.

### Error delivery

Use a domain-specific exception or an existing appropriate storage exception with a safe message. Map it through CLI/TUI boundaries. The error should identify local playlist storage and explain that the original file was preserved. Avoid a large traceback for expected permission/corruption failures.

### Required tests

1. Two threads create playlists; both IDs/titles persist.
2. Two processes create playlists; both persist.
3. Concurrent adds to the same playlist preserve both sets of tracks according to existing duplicate semantics.
4. Concurrent mutations of different playlists do not lose either change.
5. Delete/add conflict has a defined serial outcome; it never produces a partially written JSON file.
6. Failed serialization/replace preserves original bytes and leaves no orphan temp file.
7. Invalid JSON is refused on create/add/remove/delete; original bytes unchanged.
8. Invalid top-level shape is refused.
9. Invalid nested entry does not disappear during a different successful edit.
10. Missing file still allows create.
11. Read permissions failure is distinguishable from empty storage.
12. Existing valid files retain IDs, ordering and metadata.

### Concurrency test trap

The old diagnostic forced both readers through a barrier after `load()`. That is a good **pre-fix reproduction**, but using the same barrier inside the new serialized transaction can deadlock the test: the second worker cannot reach the barrier while the first holds the lock. After fixing, synchronize workers **before** entering the public mutation call, or pause the first transaction while asserting that the second is blocked outside it. Use events and bounded timeouts.

## 9. D3: make cookie export participate in logout ordering

### Scope and precise guarantee

This issue concerns ytm's exported `cookies.txt` for consumers such as authenticated playback. The anonymous playback default does not require that export.

Guarantee to implement:

> After a completed logout, an exporter that began with older credentials cannot leave a newly recreated managed cookie file behind.

Do not promise that local logout can retract bytes already read by another process or revoke the browser's Google session. That is a separate capability and is not requested.

### Required ordering

Coordinate export with the **same lock and revision state** used by logout:

- If export commits first, subsequent logout removes it.
- If logout commits first, export observes the tombstone/revision change and refuses the old snapshot.
- If another login changes accounts, the old account's export must not be published as current.

The simplest solution is a short locked transaction encompassing record selection, cookie preparation and atomic publication. There is no network operation in basic export, so this need not introduce a long lock hold. Alternatively prepare outside the lock and compare the exact captured revision before publication. Avoid lock nesting without a documented ordering.

### Storage details

- Treat `method: none` as logged out, not as a generic “record exists” state.
- OAuth/no-browser-cookie state still returns no cookie export.
- Preserve legacy import compatibility, but include the legacy state in the consistency decision.
- Audit `_write_text_0600`: direct truncation is not an atomic publication and follows symlinks. Use a managed atomic export writer with private temporary files and an explicit symlink policy.
- Do not rely only on mtime for account identity; timestamps can collide or become stale across account changes.
- If revision metadata is needed, ensure it is managed, permission-restricted where appropriate, and removed by logout. Prefer fewer credential artifacts over adding a sidecar without need.
- Verify every supported export location is included in the appropriate cleanup inventory. Caller-supplied paths need a clearly defined contract, not an implicit promise that logout deletes arbitrary files.

### Required tests

1. Export pauses after selecting account A; logout completes; exporter resumes. No exported secrets remain.
2. Export commits; logout runs next. Both tombstone and missing export are asserted.
3. Account A export races account B login; old A bytes are not published as B's current export.
4. OAuth state produces no browser-cookie file.
5. Fresh/no-session and tombstone states produce no file.
6. Failed export write preserves the previous good export and cleans up staging.
7. POSIX modes are restrictive; tests are appropriately skipped/adapted on Windows.
8. Symlink targets are not unexpectedly truncated.
9. Legacy browser headers still export when that legacy record is effective.
10. Public playback/search does not begin exporting cookies as a side effect of this fix.

Use actual store operations in a temporary directory with fake cookie strings. If the fix holds the lock while reading, do not inject synchronous logout from inside that read on the same thread and expect it to emulate another process: use worker events to verify lock ordering correctly.

## 10. D4: surface asynchronous playback errors without false alarms

### Why command success is insufficient

mpv can accept `loadfile`, begin yt-dlp resolution, and fail later. The current command path sees success; the observer drops the `end-file` error. A user sees silence and may repeatedly restart the application.

### Recommended event architecture

Preserve existing property consumers while adding a defined path for lifecycle events. One reasonable design is a richer `observe_events()` generator plus a compatibility `observe()` adapter. Another is an optional lifecycle callback on the observer. Choose the smaller change supported by current callers; do not overload an ordinary property name with an undocumented error sentinel.

Trace the full path:

```text
mpv JSON event
  -> Player event parser
  -> Backend listener
  -> safe application error event
  -> existing thread-safe Textual message
  -> visible error banner
```

The observer must continue to:

- Write property registrations before consuming initial values.
- Ignore/match command replies correctly.
- Deliver initial property observations.
- Continue listening after a recoverable track error.
- Stop promptly on owned-session shutdown.

### Correct failure attribution

Use the identity mpv provides for the affected playlist entry where available, verifying the installed mpv event contract. If a playback-generation token is needed, propagate it through selection and events. Do not label a late failure from track A as an error for newly selected track B simply because B is the current UI row.

Do not display an error for normal EOF, explicit stop, skip, application quit or intentional replacement. Do not claim that every killed extractor means failure: replacing a track deliberately kills its resolver.

### User-facing wording

A minimal safe message can say:

```text
Could not play this track. Try another track or retry playback.
```

If a category is reliably known, make it more useful. Do not blindly print `file_error`, stderr, a provider response body, command arguments or a signed media URL. Attach safe track metadata or entry identity where possible. Terminal control characters must not pass through unfiltered.

### Required tests

- Synthetic `end-file` with `reason: error` reaches the UI error path.
- A following property update is still delivered.
- EOF/stop/quit/replacement events produce no false banner.
- Old-generation errors do not overwrite the new track's state.
- Error output excludes synthetic cookie, authorization and signed-URL sentinels.
- A valid later play clears the appropriate error according to the existing UI policy.
- Initial property values still arrive; avoid reintroducing the earlier observer-registration bug.
- Closing during failure handling does not hang or touch destroyed widgets.
- Exactly one click still produces exactly one play request.

A real Google account is unnecessary. Use fake IPC messages and the existing fake mpv server. A local invalid-media smoke test is optional only in an isolated owned mpv instance; never interrupt the user's existing playback.

## 11. D6: one exception boundary for both TUI entry points

### Required behavior

`ytm` and `ytm tui` should behave identically for ordinary startup errors and cancellation:

| Outcome | Expected result |
|---|---|
| TUI exits successfully | Exit status 0; no printed `(None, None)` |
| Expected player/startup failure reaching CLI | Short stderr message, exit status 1 |
| KeyboardInterrupt reaching CLI | Cancellation message, status 130 |
| Unexpected programming bug | Preserve diagnosability under the established project policy |

Move default dispatch inside the same error-handling structure as explicit dispatch or select a common callable before entering it. Do not add an unrelated broad catch that converts all exceptions into success.

Inspect actual startup layers: some `BackendError` failures are already displayed within `YTMApp.on_mount`. Preserve that behavior rather than showing the same failure twice. The diagnostic's injected `PlayerError` proves an exception-boundary inconsistency; it is not a claim that every missing-mpv condition currently escapes the UI's own handler.

Tests should inject identical results/errors into both entry points and compare status/output. Verify JSON/help/version behavior is unchanged and global arguments remain supported.

## 12. D7: unify playback and download JavaScript runtime selection

### Proven fact versus inferred impact

The audited machine has Node and no Deno. Playback selects Node. Direct `YoutubeDL` downloads enable only Deno by default. That configuration mismatch is proven. A live video download failing because of it was not tested.

### Implementation guidance

1. Inspect the installed yt-dlp Python API and the command-line/raw-options format used by mpv.
2. Put runtime discovery in a small dependency-neutral helper if needed. Do not import `cli.py` from `cache.py` merely to reuse a private function and create a layering cycle.
3. Preserve the existing preference: Deno first, then Node, unless evidence supports an intentional documented change.
4. Adapt the result separately to the two consumers. A CLI runtime name/string is not automatically the right Python `js_runtimes` value; current Python options use a mapping.
5. Decide how executable paths are represented and test paths containing spaces.
6. If neither runtime is installed, preserve a clear degraded behavior or useful setup message. Do not automatically install system software.
7. Keep downloads anonymous, preserve PO-token provider configuration, and do not change format selection as an incidental side effect.

### Required tests

| Simulated environment | Expected selection |
|---|---|
| Deno and Node available | Deno under existing preference |
| Only Node | Node in playback and Python download options |
| Only Deno | Deno in both |
| Neither | Documented fallback/error; no fabricated path |
| Runtime path contains spaces | Passed as a single appropriate argument/value |

Assert actual `YoutubeDL` normalized parameters using a local constructor where useful, without calling network extraction. Also test the mpv argument builder. A successful real download can supplement these checks, but must be reported separately and must not require account cookies.

## 13. M1: retain useful diagnostics across restarts

### Current limitation

The TUI trace is opened in write mode at startup, and mpv uses a fixed log path. The failed run can disappear when the user restarts to recover. That made it impossible to prove what happened in the earlier failed sessions.

### Desired result

Retain a small bounded history of application runs with enough safe information to correlate:

- Application version and session identifier.
- Startup/exit time and startup failure category.
- Command type and elapsed time.
- Track/playlist operation outcome without credential data.
- Player failure category and the relevant playback generation/entry.

### Design constraints

- Do not assume only one TUI instance runs at a time.
- Per-run filenames can avoid one live instance truncating another's log. If rotating fixed names instead, coordinate rotation so concurrent starts do not overwrite each other.
- Define both retention count and size limits; logs must not grow without bound.
- Avoid rotating/deleting files still actively written by another session.
- Respect `YTM_TUI_LOG`. An explicit diagnostic path should have a documented append/truncate policy and should not unexpectedly rotate unrelated files.
- A logging failure must never prevent playback/startup.
- Store logs outside the repository and apply reasonable per-user permissions.
- Do not retain authentication headers, cookie values, passwords, OAuth tokens, raw network bodies or full signed media URLs.

### Important mpv distinction

A Python log filter cannot sanitize a file written directly by mpv. Choose safe mpv verbosity and retention behavior deliberately. If raw debugging logs can include signed URLs, do not automatically copy them into longer-lived logs under the assumption that application redaction applies. A safe application event summary may be preferable to retaining detailed mpv output by default.

### Tests

1. Two simulated sequential starts preserve the first run's safe diagnostic summary.
2. Concurrent starts do not clobber one another.
3. Retention removes only eligible old logs.
4. A full/unwritable log directory does not stop application work.
5. Explicit log-path override works and is used by tests.
6. Synthetic secret-bearing errors are normalized before persistence.
7. A restart after a simulated playback error leaves enough history to identify that earlier error.

Document the chosen location, retention policy and how the user can collect a safe report. Do not add a remote telemetry service.

## 14. M2: resolve or explicitly track the Pillow/textual-image warning

Two passing cover-art tests trigger a deprecation warning in installed `textual_image`: it calls `Image.getdata()`, which the warning says is scheduled for removal in Pillow 14.

### Work required

1. Record installed Pillow and `textual-image` versions.
2. Identify whether a compatible upstream release fixes the call.
3. If considering a dependency change, inspect upstream release notes/source and run the affected cover-art tests and package compatibility checks.
4. Prefer an upstream fixed version over editing files inside `.venv`.
5. If no suitable release is available, document the compatibility issue and choose a justified temporary constraint only if needed. Do not add a speculative pin solely to hide a warning.
6. Do not globally suppress deprecation warnings: that can hide unrelated future failures.

If external source verification is needed, use primary package documentation/source and record what version was checked. A bare claim that “latest fixes it” is insufficient.

### Acceptance

Either the warning is resolved by a tested compatible dependency change, or the handoff completion report explicitly lists it as an upstream item with current impact and a follow-up condition. A warning disposition is valid; falsely marking it as an application bug fix is not.

## 15. M3: triage static warnings by actual impact

The baseline full scan reported 119 warnings and no errors. Categories included unused imports/variables, import ordering, broad exception handlers and stylistic recommendations. Some tool severity labels overstate risk: a “combine nested if statements” suggestion is not proof of a security vulnerability.

### Triage procedure

1. Save a baseline report.
2. Group findings into correctness/security, maintainability, tests/benchmarks, and intentional patterns.
3. Fix high-confidence behavioral/security defects relevant to the requested work.
4. Remove clearly unused local code/imports where safe, but preserve intentional public re-exports.
5. For broad exception handlers at browser/provider boundaries, inspect whether the handler intentionally prevents secret-bearing exceptions reaching the user. Narrow only when it remains robust against known provider failure shapes.
6. Avoid renaming APIs, changing retry policy or reorganizing architecture merely to satisfy lint.
7. Keep mechanical cleanup separate from behavioral fixes where practical so reviewers can identify the important diff.
8. Run changed-file checks after edits and a final appropriate deep/full check.

### Acceptance

No new unexplained high-impact findings. Provide before/after counts and a short explanation of intentional remaining warnings. Do not require zero warnings by deleting useful compatibility code or disabling broad rule sets.

## 16. M4: browser/OS compatibility validation and honest documentation

This is a verification workstream, not a promise that every browser can be made to export cookies on every OS.

### Existing implementation contract

| Mode | Browser choices | Profile behavior |
|---|---|---|
| Native `ytm login` | Chrome, Chromium, Edge, Firefox, Brave, Vivaldi, Opera, Helium | Opens an ordinary installed browser; imports chosen profile after terminal Enter |
| `--from-browser` | Same eight | Imports existing browser cookies; explicit selection preferred for multiple accounts |
| `--method playwright` | Chromium, Chrome, Edge, Firefox, WebKit | Owned isolated context, not the normal user profile |
| `--method oauth` | Advanced fallback | Existing OAuth flow and refresh behavior retained |

Linux, macOS and Windows branches exist. This is implemented routing/discovery, not evidence of a completed manual sign-in on each combination.

### Mandatory facts to preserve

- Chrome-family native profile default is the directory named `Default`, not the last active profile.
- Firefox profile resolution uses its profile configuration.
- Opera has no explicit profile selector in the current implementation.
- Safari normal-profile import is unsupported. Playwright WebKit is not the Safari user profile.
- Native login waits for Enter; its terminal prompt is not automatically interrupted by the timeout.
- Windows Chromium-family encryption can block extraction even though the browser opens successfully.
- macOS privacy/Keychain permissions can block extraction.
- Snap/Flatpak, portable installs and unusual paths are not automatically certified.
- Google may reject isolated automated browsers.
- Native import does not read live ytcfg and cannot promise brand-channel detection equivalent to Playwright observation.

### Verification matrix to fill, not pre-check

| OS/environment | Native Chrome | Native Firefox | One alternate native browser | Isolated Chromium | Isolated Firefox/WebKit | OAuth compatibility |
|---|---|---|---|---|---|---|
| Linux conventional desktop | Not manually verified by this handoff | Not manually verified | Not manually verified | Not manually verified | Not manually verified | Recheck |
| macOS | Not manually verified | Not manually verified | Not manually verified | Not manually verified | Not manually verified | Recheck |
| Windows | Extraction may be blocked; test | Not manually verified | Extraction may be blocked; test | Not manually verified | Not manually verified | Recheck |

Record actual OS/browser versions, outcome, safe failure category and whether tests were mocked or live. Do not ask an agent to manufacture a platform result it cannot run. Where hardware is unavailable, retain unit tests and document the unverified combination.

### Minimal live test sequence on available platforms

Use the user's explicit authorization and a consenting account for real login. Never make live account access part of unattended CI.

1. Public search without credentials.
2. Launch chosen native profile, manually sign in, press Enter, confirm safe account label.
3. Read an account listing; no mutation required.
4. Cancel a second login and verify the earlier session remains active.
5. Logout and verify local credential cleanup while the normal browser remains signed in.
6. Repeat with an alternate profile/account where practical.
7. Test isolated login separately and record Google rejection honestly if it occurs.
8. Exercise expected unsupported/permission cases and verify useful messages without secrets.

### Acceptance

README and handouts distinguish supported choices, tested configurations, known limits and fallbacks. Lack of access to another OS is a documented verification limit, not justification to remove support or claim success.

## 17. M5: project-memory setup is optional tooling work

The audit's `dejavu memory project recall` returned `PROJECT_NOT_INITIALIZED`. This is not a YTM product defect and must not become a runtime dependency.

The receiving developer should follow their own machine's agent instructions. If they use dejavu and want project memory, inspect its local help, resolve the repository identity correctly, and initialize it only as authorized in that environment. Do not bind your friend's checkout to the author's machine-specific paths or UUID.

If no project-memory tool is present, mark this item “not applicable to the receiving environment.” Nothing in the YTM package, tests or installer should require dejavu. Never store credentials or complete private transcripts in project memory.

## 18. Test isolation and reproducibility rules

The repository already has autouse fixtures redirecting authentication and audio-cache paths and resetting cached clients. Read `tests/conftest.py`; do not assume a standalone Python script gets those fixtures.

Additional isolation requirements:

- Redirect all new temporary playlist paths explicitly.
- Redirect TUI traces using `YTM_TUI_LOG` before tests launch the app.
- Use fake credentials such as `__Secure-3PAPISID=FAKE`; never copy a cookie from your browser into a test.
- Do not invoke real browser import functions when testing argument routing.
- Monkeypatch dependency discovery rather than installing/removing system browsers or runtimes.
- Use fake mpv servers or an isolated owned process, never the live user's IPC endpoint.
- Clean shared builder state after fault injection so a failed test cannot hang the rest of the suite.
- Avoid tests dependent on scheduler luck, wall-clock sleeps or external HTTP availability.
- Use process-safe top-level test worker functions if multiprocessing must support spawn on Windows.
- Every thread/process test has a bounded completion assertion and cleanup path.

## 19. Suggested regression names and expected evidence

These names are suggestions; fit the existing test organization.

```text
test_shared_client_releases_builder_after_final_stamp_failure
test_waiting_client_recovers_after_builder_validation_error
test_stale_builder_cannot_publish_after_reset

test_local_playlist_concurrent_creates_preserve_both
test_local_playlist_processes_do_not_lose_updates
test_local_playlist_mutation_refuses_corrupt_store
test_local_playlist_failed_replace_preserves_original

test_cookie_export_cannot_recreate_file_after_logout
test_cookie_export_rejects_changed_account_revision
test_cookie_export_is_atomic_and_private

test_mpv_playback_error_reaches_backend
test_stop_and_replacement_are_not_playback_failures
test_stale_track_error_does_not_replace_current_track_state
test_playback_error_output_contains_no_secret_material

test_plain_and_explicit_tui_share_startup_error_handling
test_plain_tui_keyboard_interrupt_returns_130

test_node_only_runtime_is_used_for_download_and_playback
test_runtime_discovery_handles_neither_runtime

test_log_history_survives_restart
test_log_retention_does_not_clobber_active_session
test_safe_logs_exclude_secret_sentinels
```

Capture failing-before and passing-after output for each core bug. For D7, state that the regression checks option parity, not successful remote YouTube extraction. For platform tests, label simulated OS behavior as simulated.

## 20. Validation commands and completion gates

Run focused tests while implementing; do not rerun the full suite after every small edit. Examples of useful existing groups:

```sh
.venv/bin/python -m pytest -q tests/test_caching.py tests/test_auth_review.py
.venv/bin/python -m pytest -q tests/test_playlists.py tests/test_tui_backend.py
.venv/bin/python -m pytest -q tests/test_auth_storage.py tests/test_core_state_music_auth.py
.venv/bin/python -m pytest -q tests/test_mpv_player.py tests/test_tui_widgets.py
.venv/bin/python -m pytest -q tests/test_cli_core.py tests/test_cache.py
```

Use the isolated trace environment for any group that creates a TUI. Include newly added tests in each scoped run; the commands above do not magically discover a new file outside the listed group.

Once integrated:

```sh
YTM_TUI_LOG=/tmp/ytm-handoff-final.log .venv/bin/python -m pytest -q
uv pip check --python .venv/bin/python
code-health check --fast --changed
code-health check --deep --changed
git diff --check
```

If dependencies were changed, validate an install from project metadata in a separate environment rather than only the already-provisioned virtual environment. Verify package data and entry points if new modules/resources were introduced. Do not publish a release as part of validation.

### Completion checklist

- [ ] D1: all builder exits release ownership; subsequent caller recovers.
- [ ] D2: whole local-playlist mutation is serialized across threads/processes.
- [ ] D3: export/logout ordering prevents credential-file resurrection.
- [ ] D4: playback errors reach a safe visible UI message without false stop/skip errors.
- [ ] D5: malformed playlist data cannot be silently replaced by mutation.
- [ ] D6: default and explicit TUI entry points share expected error behavior.
- [ ] D7: Node-only and Deno-only option parity tested.
- [ ] M1: bounded safe diagnostic history survives restart.
- [ ] M2: dependency warning fixed or explicitly tracked with evidence.
- [ ] M3: static findings triaged; remaining intentional warnings explained.
- [ ] M4: compatibility claims match actual tests; unavailable platforms labeled.
- [ ] M5: optional tooling disposition recorded; no product dependency introduced.
- [ ] Prior duplicate-click regression remains fixed.
- [ ] Public commands remain anonymous.
- [ ] Existing OAuth credentials/migration continue to work.
- [ ] No real credentials or account data appear in code, tests, logs or report.
- [ ] No unrequested remote changes, browser-profile modifications or system changes.
- [ ] Full suite and appropriate static/package checks completed.
- [ ] Documentation and changelog describe the final behavior accurately.

## 21. Reviewer checklist: actively look for these failed fixes

Reject patches that:

- Clear a builder flag only on the original constructor exception path.
- Fix a hang with a timed wait but leave ownership permanently inconsistent.
- Lock only `save()` and leave the earlier `load()` outside the transaction.
- Protect threads but not separate processes.
- Reuse one temporary file across simultaneous writers.
- Silently normalize away corrupt playlist entries before saving.
- Delete the user's original corrupted file to make the test pass.
- Check the cookie revision before acquiring the lock and never check it again.
- Retain raw signed URLs in newly rotated persistent logs.
- Treat every `end-file` event as a failure, including normal stop/EOF.
- Display an error for track A against track B after the user switches tracks.
- Remove the existing initial mpv property-delivery behavior.
- Turn all unknown exceptions into status 0 or a misleading auth-expired message.
- Enable account cookies in the anonymous download path to fix runtime selection.
- Claim a browser/OS combination passed live verification based on mocks.
- Suppress all dependency warnings to achieve a clean-looking test log.
- Revert the already-applied single-click fix while changing event handling.

## 22. Expected final report from the receiving Codex session

Use a compact summary plus a disposition table:

| ID | Status | Files changed | Regression/validation evidence | Remaining limitation |
|---|---|---|---|---|
| D1 | Fixed / not fixed | ... | failing-before, passing-after | ... |
| D2 | ... | ... | ... | ... |
| D3 | ... | ... | ... | ... |
| D4 | ... | ... | ... | ... |
| D5 | ... | ... | ... | ... |
| D6 | ... | ... | ... | ... |
| D7 | ... | ... | ... | ... |
| M1 | ... | ... | ... | ... |
| M2 | Fixed / upstream tracked | ... | ... | ... |
| M3 | Triaged | ... | before/after counts | ... |
| M4 | Verified / partly verified | ... | actual OS/browser versions | ... |
| M5 | Configured / not applicable | tooling only | ... | ... |

Include final test counts, static-check results, dependency changes, migration implications, and untested platforms. Never write “all issues fixed” if an item is deferred or only documented. It is acceptable for an upstream warning or unavailable platform to remain a clearly stated limitation; it is not acceptable to silently omit it.

## Appendix A. Original diagnostic evidence

The following report is embedded so the handoff can be shared as one file. It is historical audit evidence. Earlier `/tmp` reproduction paths are not required; the receiving agent should implement durable isolated regression tests according to the sections above. Line numbers refer to the audited checkout.

### YTM diagnostic audit — 2026-09-26

#### Summary

The existing suite passes, but targeted fault injection reproduced six defects and identified one playback/download configuration mismatch. Three findings deserve high priority: a stuck authenticated-client initialization flag, local-playlist lost updates, and cookie-export races with logout.

This audit is diagnostic only. It does not change application behavior, authenticate to Google, read actual cookie values, mutate the user's playlists, or control the running player. The previous duplicate-click fix remains in the working tree. Temporary test logs use `/tmp/ytm-diagnostics-*` so the normal TUI log is preserved.

#### Checks performed

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

#### Findings ordered by priority

##### D1 — High: account-client initialization can become permanently stuck

**Location:** `ytm/music.py:234–242`, especially the second `_auth_stamp(path)` call at line 237.

**Trigger:** Client construction succeeds, then reading the credential stamp raises—for example, the file becomes unreadable or malformed between the initial and final stamp reads.

**Root cause:** The final stamp comparison is outside the exception handler that resets `_CLIENT['building']`. An exception bypasses both resetting that flag and notifying waiting threads. Future calls see `building=True` and wait on the condition indefinitely. `reset_client()` also does not clear that flag.

**Reproduction:** Replace `_auth_stamp` with a fake returning one valid revision and then raising `AuthStorageError`; fake client construction succeeds. After the exception, `_CLIENT['building']` is still `True`.

**Impact:** Account-backed TUI operations can appear frozen until the process is restarted. This is a plausible restart-requiring failure mode, but there is no evidence tying this injection scenario to the user's earlier real incident.

**Suggested fix:** Put construction, final revision validation and publication inside a structure that always releases the builder state and notifies waiters. Keep the generation check so a stale builder cannot publish after logout. Add a test that a second request recovers after the final stamp read fails; testing only constructor failures misses this case.

##### D2 — High: concurrent local-playlist mutations lose data

**Location:** `ytm/playlists_local.py:130–144` and other load/modify/save operations; `save()` at lines 78–90.

**Trigger:** Two requests read the same local-playlist store before either writes its change. These can come from background TUI work or separate application instances.

**Root cause:** Atomic rename makes each individual file replacement atomic, but the complete read/modify/write operation has no transaction lock. Both callers change independent copies; the later write replaces the earlier caller's result. Within one process, the PID-only temporary filename is also shared across threads and can cause competing writers to interfere.

**Reproduction:** Two worker threads call `create()` against a temporary store. A barrier makes both finish loading before either saves. Saves are deliberately serialized in the diagnostic to isolate lost updates from temporary-file collisions. Both calls complete, but only one playlist remains.

**Impact:** Successfully acknowledged local playlist creation, additions or edits can disappear.

**Suggested fix:** Lock the whole read/modify/write transaction across threads and processes, use unique temporary filenames, and run mutation tests under concurrent access. A lock around only `save()` is insufficient: the reproduction already serializes saves and still loses data.

##### D3 — High: concurrent cookie export can recreate credentials after logout

**Location:** `ytm/auth.py:1025–1044`, `cookies_file()`.

**Trigger:** Cookie export reads browser headers, logout then commits its tombstone and deletes managed credentials, and the original exporter subsequently writes the previously read headers to `cookies.txt`.

**Root cause:** Export operates outside the session-store transaction and does not verify the captured revision before writing. Its later `active_record()` check treats a logout tombstone as an existing record when choosing the timestamp source; it does not reject the stale export.

**Reproduction:** In a temporary configuration directory, save a synthetic browser session. Inject logout immediately after the export function reads the header. At completion, `session.json` has `method: none`, but `cookies.txt` exists again.

**Impact:** Sensitive local credential material can remain after a reported logout. This does **not** reactivate the authenticated ytmusicapi record. A stale export may also be handed to an external playback consumer. The path is relevant when cookie export is requested, including authenticated streaming; ordinary anonymous playback does not export account cookies by default.

**Suggested fix:** Coordinate cookie generation and logout with the same store lock and revision check. Write the export atomically, refuse a tombstone, and reject a snapshot that changed while preparing the export. Add a two-thread export/logout test.

##### D4 — Medium: playback failures are invisible to the TUI

**Location:** `ytm/player.py:532–543` and the property-only consumption loop in `ytm/tui/backend.py`.

**Trigger:** mpv reports an asynchronous stream failure using an `end-file` event with `reason: error`.

**Root cause:** `Player.observe()` yields only `property-change` messages. It ignores playback failure events. A successful `loadfile` command reply acknowledges the command; it does not prove that stream resolution or audio playback succeeded.

**Reproduction:** Feed the observer a synthetic `end-file` error followed by a pause-property event. The caller receives only `('pause', False)`; no playback error is surfaced.

**Impact:** Failed extraction, inaccessible media or another asynchronous playback failure can leave the UI looking idle/silent without an actionable error. This contributes directly to users trying restarts without knowing what failed.

**Suggested fix:** Forward lifecycle/error events through a typed event path while preserving property observers. Display a sanitized error linked to the affected track. Treat deliberate stop/replacement differently from an actual stream failure. Avoid automatically retrying every event, which could create loops.

##### D5 — Medium: corrupt local playlists can be silently overwritten

**Location:** `ytm/playlists_local.py:51–58` and mutation callers such as `create()` at line 132.

**Trigger:** The playlist file contains invalid JSON or has the wrong top-level shape, and the user subsequently performs a mutation.

**Root cause:** `load()` converts unreadable/corrupt storage into an empty store. Writers cannot distinguish that recovery view from a genuinely new empty store, so the next successful mutation overwrites the existing file.

**Reproduction:** Write `{broken` into a temporary playlist file, call `create()`, then inspect it. The damaged original is replaced with a fresh one-playlist store without an error or backup.

**Impact:** Data that might have been recoverable is lost, and the user has no explanation for missing playlists.

**Suggested fix:** Tolerant reads may keep the UI usable, but mutation must distinguish missing files from corruption or permission failure. Refuse destructive replacement or preserve a recoverable backup before explicit repair.

##### D6 — Medium: plain `ytm` bypasses startup error handling

**Location:** `ytm/cli.py:969–977`.

**Trigger:** The default TUI command raises a normal startup `PlayerError`, such as unavailable mpv or failed player launch.

**Root cause:** `cmd_tui(args)` for a missing subcommand executes before the main `try` block. The explicit `ytm tui` command executes inside it.

**Reproduction:** Mock `cmd_tui` to raise `PlayerError`. `main([])` leaks the exception; `main(['tui'])` returns exit status 1 with a clean error message.

**Impact:** The most common entry point can produce a traceback for an expected environment failure, while its equivalent explicit command behaves correctly. Keyboard interruption during that startup path is also outside the shared handler.

**Suggested fix:** Route default and explicit TUI invocation through the same exception-handling boundary while retaining the correct success exit code.

##### D7 — Medium, confirmed configuration mismatch: downloads omit Node runtime selection

**Location:** `ytm/cache.py:178–195`, compared with `_js_runtime()` and player construction in `ytm/cli.py:53–57,104`.

**Trigger:** A system has Node but no Deno, and a YouTube download requires JavaScript challenge evaluation.

**Root cause:** Playback explicitly selects an available JavaScript runtime. The direct Python yt-dlp download path does not supply `js_runtimes`, so it retains the installed yt-dlp default configuration.

**Local evidence:** Node is available, Deno is absent, playback's selector returns Node, and constructing `YoutubeDL(cache._ydl_opts(...))` yields only `deno` in the enabled runtime configuration.

**Impact:** Playback and offline download can behave differently on the same installation. A track needing JavaScript evaluation may download unreliably despite playing through mpv. No live network download was performed, so this report does not claim a measured extraction failure for a specific video.

**Suggested fix:** Share runtime discovery between playback and downloads and test a Node-only environment. Keep cookie/PO-token policy separate from runtime selection.

#### Minor issues and limitations

1. **Diagnostics disappear on restart.** TUI logs are truncated at startup; the mpv log uses a fixed path. Retaining a small rotated history would help distinguish the failed attempt from the eventual successful run. Do not retain raw authentication headers or resolved signed media URLs unnecessarily.
2. **Dependency deprecation warning.** Two tests exercise `textual_image` code calling Pillow's deprecated `Image.getdata()`. This is an upstream compatibility warning, not a current test failure.
3. **119 static warnings.** Findings include unused variables/imports, import ordering, broad exception handlers and style suggestions. The tool classifies some stylistic rules as security findings; that label alone is not evidence of an exploitable defect. Review relevant findings individually.
4. **Browser compatibility remains conditional.** Unit tests cover routing/discovery with mocks. Real macOS/Windows login, browser cookie encryption, Google acceptance of isolated Playwright login, and portable/sandboxed browser distributions remain unverified. Native login requires terminal confirmation; it is not automatic page observation.
5. **Project memory is not initialized.** The configured recall command returned `PROJECT_NOT_INITIALIZED`. This affects historical agent context, not YTM runtime behavior.

#### Existing duplicate-click fix

The previously reproduced duplicate dispatch is fixed in the current working tree. Exact-count tests now require one playback request per click. Repeated clicks, Enter, changing columns within a row and header selection are covered. The complete suite above includes that fix. These new findings should not be confused with a recurrence of the same click bug.

#### Recommended implementation order

1. Fix D1's stuck initialization state and D4's missing playback errors to improve reliability and make future playback failures observable.
2. Fix D2 and D5 together as a local-playlist storage transaction/recovery change.
3. Fix D3 before describing logout as reliably removing every managed credential copy under concurrency.
4. Unify default TUI error handling (D6) and JavaScript runtime selection (D7).
5. Add safe log rotation and address remaining warnings according to their actual impact.

#### Reproduction artifacts

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
