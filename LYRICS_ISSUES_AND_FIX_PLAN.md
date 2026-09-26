# Synchronized lyrics: issues, evidence, and implementation instructions

Reviewed: 2026-09-15. Repository: `ytm`.

## Scope and status

This report covers the uncommitted synchronized-lyrics implementation, its playback/event integration, and adjacent problems observed while implementing and testing it. It is not an audit of every module in the repository. It does not treat unchecked items in `PR_32_TODO.md` as confirmed findings.

Base commit: `93176dde898d39634d4da695f2aef527344c005a`. References below refer to the working tree inspected on the review date; line numbers will shift after edits.

Only this report was authored for this request. The fixes described here have **not** been implemented. Existing feature changes and unrelated local files were preserved.

### Evidence terminology

- **Reproduced:** a local probe demonstrated the behavior during this review, or a recorded test failure demonstrated it during the preceding implementation session.
- **Code-confirmed risk:** the relevant control flow is present, but the described load or real provider response has not been observed in live playback.
- **Validation gap:** the implementation may work, but current tests do not substantiate the claim sufficiently.
- **Optional improvement:** a product or performance tradeoff, not a demonstrated correctness failure.

### Validation actually performed

During the implementation session, the first full-suite run reported **383 passed, 1 failed**. The failure was `test_dark_and_light_theme_resolve_to_different_styles`, with a late `RequestDone` handler querying an absent `#error-banner`. A subsequent full-suite run passed **384 tests**, with two Pillow deprecation warnings.

During this report's review:

- Ran isolated, network-free probes for empty timed responses, malformed timing records, resize/recentering, and stale same-song result handling.
- Reran the theme test in isolation: **1 passed in 0.41 seconds**. This does not establish that the intermittent race is fixed.
- Ran `code-health check --deep --changed`: **0 errors, 14 warnings**. The warnings point to unchanged lines and are itemized below.
- Inspected installed dependencies: ytmusicapi 1.12.2, Textual 8.2.8, textual-image 0.12.0, Pillow 12.3.0.
- Did not run live music playback, change account credentials, or contact YouTube as part of these probes.

## Prioritized issue index

| ID | Priority | Finding | Evidence |
| --- | --- | --- | --- |
| LYR-01 | High | A late UI callback can access widgets after teardown | Previously reproduced intermittent failure |
| LYR-02 | Medium | Empty timed results skip available plain lyrics | Reproduced with fake provider |
| LYR-03 | Medium | Old requests for the same video can overwrite newer results | Reproduced through actual result handler |
| LYR-04 | Medium | Resizing does not recenter the currently highlighted line | Reproduced in mounted Textual pane |
| LYR-05 | Medium | Invalid timing records can raise inside the UI event loop | Reproduced with malformed fixture; live prevalence unknown |
| LYR-06 | Medium | Rapid track changes create unbounded independent lyrics threads and duplicate requests | Code-confirmed risk |
| LYR-07 | Medium | Tests do not verify visible highlighting, scrolling, and several event boundaries | Validation gap |
| LYR-08 | Low | Every uncached timed lookup rebuilds an authenticated client | Code-confirmed cost; unbenchmarked |
| LYR-09 | Low | Sub-second lyrics events also trigger redundant now-playing updates | Code-confirmed extra calls; unbenchmarked |

Priority reflects suggested implementation order, not an automated severity score. In particular, defensive handling for a hypothetical malformed provider record is distinguished from an observed teardown failure.

---

## LYR-01 — Background completions can outlive the UI they update

**Priority:** High. **Origin:** existing asynchronous request lifecycle, adjacent to lyrics work.

### Evidence and locations

- `ytm/tui/app.py:469`: `_request_async()` schedules threaded work and posts `RequestDone`.
- `ytm/tui/app.py:491`: `on_request_done()` invokes `_show_error()` or `_clear_error()` without a shutdown guard.
- `ytm/tui/app.py:450`: `_clear_error()` requires `#error-banner` to exist.
- `ytm/tui/app.py:409`: the lyrics completion handler likewise assumes the lyrics pane still exists.
- `tests/test_tui.py:630`: the theme test enters and immediately leaves two `run_test()` contexts without explicitly waiting for startup requests.

The recorded failure path was:

```text
run_test() teardown
  -> on_request_done()
  -> _clear_error()
  -> query_one("#error-banner", Static)
  -> textual.css.query.NoMatches
```

The exception reported no matching banner on the default screen. A later run passed. This is evidence of an intermittent lifecycle/order problem, not evidence of broken theme colors. The precise production shutdown interleaving still needs a deterministic regression test.

### Impact

The suite can fail depending on completion timing. The same unguarded widget assumptions deserve examination during real app exit, especially with slow network requests. Do not claim a reproduced production shutdown crash: only the test-context failure was observed.

### Fix instructions

1. Add an explicit application lifecycle state for accepting UI results. Initialize it before worker creation; disable acceptance at the beginning of shutdown, before child widgets disappear.
2. Centralize shutdown bookkeeping so both quit actions and context-driven unmount/exit invalidate results. Do not rely solely on the keyboard quit action: tests and framework shutdown use other paths.
3. Invalidate lyrics request generations and prevent scheduling new requests once closing starts.
4. Guard completion handlers before accessing widgets or invoking callbacks that access widgets. Check lifecycle/attachment at the handler boundary; do not scatter broad `except Exception` blocks around individual updates.
5. Cancel or mark workers obsolete using the framework's supported lifecycle APIs. A cancellation signal does not forcibly terminate blocking HTTP work; timeouts and result invalidation remain necessary.
6. Close backend/listener resources consistently. Preserve the existing difference between quitting the interface and shutting down mpv.
7. Adjust the theme test to wait for startup work when its purpose is solely theme comparison. Separately add a test that intentionally exits with requests pending, so waiting in the theme test does not conceal the lifecycle defect.

### Required regression tests

Use `threading.Event` to hold a fake startup request. Start the app, begin shutdown, release the request, and verify no widget lookup/callback runs against a removed screen. Repeat for success and error completions. Add an equivalent lyrics completion case.

Avoid arbitrary sleeps as the main ordering mechanism. Always release blocked requests in `finally`, including if an assertion fails.

### Acceptance criteria

The deterministic pending-request shutdown test passes; the theme test passes; both quit modes retain their behavior; errors during a running session are still displayed. Do not call this fixed merely because a flaky full-suite rerun is green.

---

## LYR-02 — An empty timed response prevents the plain-text fallback

**Priority:** Medium. **Origin:** new timed-lyrics fetch path.

### Evidence and locations

`ytm/music.py:397–410` retries plain lyrics only when the entire result is falsey:

```python
if timestamps and not result:
    result = yt.get_lyrics(browse_id)
```

A dictionary with an empty `lyrics` list is truthy. The isolated fake provider returned:

```python
{"hasTimestamps": True, "lyrics": [], "source": "test"}
```

Its plain endpoint was configured to return `"plain works"`. Actual result:

```text
empty_timed_result: ([], 'test') calls: [True]
```

Only the timed endpoint was called. `LyricsPane.set_lyrics()` then displays “No lyrics available.” This proves the fallback omission for this input; it does not prove how often YouTube returns that input.

### Impact

The app can report no lyrics despite having a working plain-lyrics route. The existing fallback test covers `None`, not a nonempty response envelope containing no usable lyrics.

### Fix instructions

1. Determine success from usable lyric content, not just the response envelope.
2. For a timed result, normalize and validate the line list. Treat an empty usable list as a candidate for one plain fetch.
3. For a plain result returned by the timed request, retain it directly if it contains usable text. Avoid an unnecessary second request in that case.
4. Make exactly one plain retry when the timed result provides no usable content. Do not add recursive or repeated retries.
5. Preserve the source belonging to the selected response. Do not combine plain text with a discarded timed response's attribution.
6. Preserve authentication handling. An expired-auth exception should still follow `_refreshing`; it should not silently become “No lyrics available.”
7. Consider non-auth timed-endpoint failures separately. A fallback after a supported provider error may be useful, but broad exception suppression would hide programmer errors. Document and test any chosen exception policy.

### Required regression tests

Cover timed `None`, empty timed list, timed response with `lyrics=None`, plain text returned by the timed request, valid timed lines, and both endpoints having no lyrics. Assert request sequence and returned source in each case.

### Acceptance criteria

For the demonstrated empty-list fixture, calls become `[True, False]` and the result contains `"plain works"`. Valid timed lyrics still require one lyrics endpoint call after the watch lookup.

---

## LYR-03 — Video ID does not uniquely identify an in-flight lyrics request

**Priority:** Medium. **Origin:** existing stale-result guard, now also used for timed lyrics.

### Evidence and locations

- `ytm/tui/app.py:85`: `LyricsFetched` stores video ID, result, and error, but no request generation.
- `ytm/tui/app.py:394`: the application stores only `_lyrics_video_id`.
- `ytm/tui/app.py:411`: completion validity is checked only by video-ID equality.

A result-handler probe delivered a new success for A followed by an older error for A. The mounted pane ended with:

```text
late_A_result: lyrics error: old failure
```

The handler cannot distinguish those request instances.

### Realistic triggering sequence

1. Start track A; its lyrics request is slow.
2. Switch to B.
3. Return to A before the first A request completes.
4. The newer A request succeeds and displays lyrics.
5. The older A request fails and its late error overwrites the successful lyrics.

A reconnect or explicit refetch of the same song can produce the same ambiguity. Successful older results may happen to contain identical lyrics; that does not make old errors safe to apply.

### Fix instructions

1. Add a monotonically increasing lyrics request generation to the app.
2. Increment it for every lyrics reset/request, including clearing playback and shutdown.
3. Include the captured generation in `LyricsFetched`.
4. Accept a result only when both its generation and video ID match current state and the app still accepts results.
5. Keep actual playback position in the pane. A valid late result must highlight at the latest observed position, not the position when fetching began.
6. Do not solve this solely by clearing the cache. A cache is about reuse; a generation is about ownership of UI updates.
7. If request coalescing from LYR-06 is added, preserve this generation guard even when two requests share the same underlying fetch.

### Required regression tests

Deterministically hold the first A request, complete B and the second A, then release the first A with an error. The successful A lyrics must remain visible. Also test old success after a newer error, A→B, and A→no current track.

### Acceptance criteria

The newest request controls the visible pane in every completion order. Obsolete results cannot change title, lyric text, active line, or scroll position.

---

## LYR-04 — Resizing leaves the active lyric at an obsolete scroll position

**Priority:** Medium. **Origin:** new automatic scrolling behavior.

### Evidence and locations

`ytm/tui/lyrics.py:44–48` schedules scrolling only when the active line index changes. `_scroll_to_active()` at line 58 computes wrapped rows using the current content width. There is no lyrics resize handler.

A mounted Textual probe used 30 lines, each containing `"long line " * 6` plus its index. It selected line 20 at 20.1 seconds, then resized from 50×15 to 25×15:

```text
before_resize: 33
after_resize: 33
correct_recenter: 53
```

Calling `_scroll_to_active()` explicitly after the resize corrected the scroll offset. The pane-level probe used explicit scroll/container heights so the result reflects wrapping rather than the application's compact-mode hiding rule.

### Impact

A narrower terminal wraps preceding lyrics into more rows, but the scroll offset remains at the old row. The highlighted line can leave the viewport. While paused, no line transition occurs to repair it. A long held lyric can likewise remain misplaced until the next line.

### Fix instructions

1. Handle geometry changes on the lyrics pane or its content viewport.
2. Schedule recentering after layout refresh so width and viewport height reflect the new geometry.
3. Skip recentering when there is no active line, the pane is detached, or the viewport is not visible/usable.
4. If the pane becomes visible after compact mode, schedule another recenter using its real width.
5. Coalesce repeated resize requests into one pending callback if necessary; do not create a recurring timer for this.
6. Continue measuring terminal rows with Rich/Textual wrapping semantics. Python string length will give incorrect results for wide glyphs and combining characters.
7. Define whether manual scrolling should suspend follow mode before coupling every resize to scrolling. The current implementation always follows on line changes; preserving that behavior is the smallest fix.

### Required regression tests

Mount a long lyric fixture, select a late line, resize narrower and wider without changing playback position, and assert the active line remains visible. Repeat while paused, with Unicode text, and across the app's compact/noncompact boundary.

Assert viewport visibility rather than only a hard-coded pixel/row offset, because theme/layout changes can legitimately change exact geometry.

### Acceptance criteria

The selected line stays visible after resizing without requiring a new position event or a change of active line.

---

## LYR-05 — Timing data is trusted inside the UI event loop

**Priority:** Medium defensive-hardening task. **Origin:** new timed-line model/rendering path.

### Evidence and locations

- `ytm/music.py:410`: timed objects are converted with `asdict()` but their fields are not validated.
- `ytm/tui/lyrics.py:30`: sorting indexes `start_time` directly.
- `ytm/tui/lyrics.py:43`: playback indexes and compares `start_time` and `end_time` directly.
- `ytm/tui/lyrics.py:53`: rendering concatenates `text` directly with a string.

A fixture containing `{"text": "missing end", "start_time": 1000}` loaded at position zero. Moving to two seconds then produced:

```text
malformed_line_at_2s: KeyError 'end_time'
```

The failure can be delayed until playback reaches the record because the interval expression short-circuits. The real provider model normally supplies the documented fields; this probe demonstrates a boundary weakness, not a captured malformed YouTube response.

### Impact

Unexpected data can escape into a Textual message handler and terminate the interface rather than presenting a recoverable lyrics error. Wrong types, nonfinite numbers, and invalid intervals can also cause errors or permanently unhighlightable lines.

### Fix instructions

1. Define a small normalized timed-line contract at the music/backend boundary: string text and finite numeric timestamps in milliseconds.
2. Validate each record once before sending it to the UI. Avoid repeated type validation on every playback tick.
3. Require an explicitly chosen interval rule, normally `0 <= start_time < end_time`. Document handling of zero-length markers rather than silently inventing duration.
4. Sort valid lines stably. Decide whether overlapping intervals select one line or several; the current UI intentionally displays only one active line.
5. Skip invalid records or fall back to plain lyrics using LYR-02 when none remain. Retain useful plain text where practical.
6. Keep blank-text timed lines if they represent intentional instrumental gaps. “Empty text” is not necessarily malformed timing.
7. Do not log complete lyric/provider payloads just to diagnose one invalid field. A concise record index and field/type description is sufficient.
8. Keep the UI's internal data contract clear. If it accepts only normalized records, give invalid internal calls an intentional error path rather than an accidental KeyError during playback.

### Required regression tests

Cover missing end/start/text, non-string text, numeric strings, `None`, NaN/infinity, reversed intervals, unsorted input, duplicate starts, and mixed valid/invalid lines. Verify malformed content cannot bring down the mounted application.

### Acceptance criteria

The demonstrated missing-end record does not raise from a playback handler; valid records still synchronize accurately; all-invalid data takes a documented plain/no-lyrics fallback.

---

## LYR-06 — Lyrics fetching has no concurrency bound or same-track request coalescing

**Priority:** Medium. **Origin:** existing thread-per-request pattern; higher relevance with isolated timed clients.

### Evidence and locations

`ytm/tui/app.py:407` starts a fresh daemon thread for every lyrics request. Unlike `_request_async()`, it is not registered with Textual's worker manager. The backend cache at `ytm/tui/backend.py:285–288` performs separate lookup, network fetch, and insertion operations without an in-flight registry.

Therefore two misses for the same video can both perform the network fetch. Distinct track changes also have no explicit concurrency limit. This is established by code inspection; no live load test or account throttling was triggered during review.

### Impact

Rapid skipping during slow responses can accumulate outstanding requests, clients, sockets, and obsolete work. Rejecting stale UI results prevents incorrect display but does not stop that work. The helper `settle()` waits for framework workers, so it does not directly wait for these raw threads either.

### Fix instructions

1. Bound the number of executing lyrics fetches. A small executor or one active worker plus a replaceable pending track is sufficient; avoid an unbounded queue of skipped songs.
2. Keep queued work biased toward the current track. If playback changes several times while one request runs, replace the pending request with the latest one.
3. Coalesce same-video in-flight fetches in the backend when feasible. Use a per-video future/promise protected by a short lock.
4. Release locks before network I/O. Do not serialize playback controls behind lyrics requests.
5. Remove in-flight entries in `finally`, on success and failure, so a failed fetch cannot permanently block later attempts.
6. Use LYR-03 generations to reject obsolete results, including coalesced results.
7. Ensure network calls have finite timeouts supported by the installed client. Confirm the actual timeout rather than assuming daemon threads will terminate promptly.
8. Integrate worker ownership with LYR-01 shutdown handling. Do not claim thread cancellation kills an in-progress HTTP call.

### Required regression tests

Block a fake provider with an Event, request many tracks, and assert active fetches stay within the chosen bound. Verify the latest pending track eventually loads. Request one video twice concurrently and assert only one underlying fetch. Inject an error, retry, and confirm in-flight bookkeeping was cleared. Assert pause/seek requests remain responsive while the fetch is held.

### Acceptance criteria

Concurrency is bounded, obsolete queued tracks are discarded, identical in-flight requests are shared or deliberately serialized, and errors do not poison subsequent requests.

---

## LYR-07 — Current tests validate selection state more than the user-visible feature

**Priority:** Medium validation gap. **Origin:** new feature tests.

### Evidence and locations

`tests/test_tui.py:1831` verifies `_active`, a literal text fragment, and clearing. `tests/test_tui.py:1855` directly calls pane methods to check delayed arrival. Those are useful, but neither test asserts highlighted Rich spans or viewport position. The delayed-arrival test does not exercise an actual blocked background fetch.

There is no live playback evidence from the implementation session. A full green suite therefore does not establish provider availability, actual scrolling, or pause/seek integration against mpv.

### Fix instructions

Add focused tests for behavior that can visibly break:

1. Inspect the rendered Text spans or equivalent public render output: the active line must have the intended style/marker; other lines must not.
2. Use enough wrapped lines to force scrolling. Assert the active line is within the viewport after forward and backward seeks.
3. Test exactly before start, exactly at start, just before end, and exactly at end. Preserve the documented half-open interval behavior.
4. Hold an actual stub request, send a position event while it is pending, release it, and inspect the resulting active style.
5. Send a position event belonging to another video and assert it does not alter the lyrics pane.
6. Add LYR-01 through LYR-06 regression cases as their fixes are implemented.
7. Verify the backend emits a clear event when playback has no current track; the existing lyrics test directly pushes that event and does not prove the backend path.
8. Verify pauses hold the current highlight without a fabricated advancing timer.
9. Keep the plain `ytm lyrics` output contract covered: timed support in the TUI must not turn CLI text/JSON into an unexpected list of dataclass objects.

### Manual smoke-test procedure

After automated checks, use normal playback and a track known to have timed lyrics. Record the app/dependency versions and whether the pane reports `LYRICS · SYNCED`. Observe several transitions, pause for a few seconds, resume, seek in both directions, resize, rapidly switch tracks, and quit during a fetch. Then test a track with plain-only lyrics and a track with no lyrics.

If no known timed track is available, report that limitation instead of treating a plain fallback as proof of synchronization. Do not use captured copyrighted lyric text as a large committed fixture; synthetic lines are enough for tests.

### Acceptance criteria

Tests would fail if highlighting styles were removed or `_scroll_to_active()` became a no-op. Live validation is recorded separately from mocked validation, with failures and unavailable cases stated explicitly.

---

## LYR-08 — Timed lyrics bypass the shared client's reuse and visitor-header optimization

**Priority:** Low, performance follow-up. **Origin:** intentional isolation in the new implementation.

### Evidence and locations

`ytm/music.py:392` constructs `client()` for timed requests, whereas ordinary catalogue calls use `shared_client()`. `shared_client()` at line 148 contains construction reuse, authentication-stamp invalidation, and `_seeded_headers()` reuse of a persisted visitor ID.

The isolation is justified: the installed ytmusicapi timed-lyrics implementation temporarily switches client mode. Sharing that mutable client with concurrent searches would introduce a different correctness problem.

### Impact and uncertainty

Each uncached song creates a fresh authenticated client and bypasses that shared construction path. Replaying a cached song avoids the work. Additional latency and connection setup are plausible, but no latency benchmark was run; do not assign a millisecond or request-count claim without measurement.

### Fix instructions

1. Measure uncached lyrics latency and construction count before optimizing.
2. Preserve isolation from catalogue requests.
3. If material, retain a dedicated lyrics client owned by a serialized lyrics worker, or use bounded worker-local clients. Do not let concurrent timed calls mutate one client.
4. Reuse safe header-seeding logic without aliasing mutable header dictionaries.
5. Invalidate the lyrics client when authentication changes and after refresh, mirroring the shared client's lifecycle.
6. Avoid introducing a second complicated cache hierarchy if constructor cost is negligible relative to fetch time.

### Required regression tests

Assert reuse across distinct songs, invalidation after auth changes, and continued isolation from concurrent search. Inject constructor failure and verify retry remains possible. Measure before/after under equivalent conditions.

### Acceptance criteria

Any optimization demonstrably reduces setup work while preserving auth refresh and client-mode isolation. Keeping the current implementation is acceptable if measurement shows no meaningful cost.

---

## LYR-09 — Every fractional position update redraws the clock as well as updating lyrics

**Priority:** Low, optional performance improvement. **Origin:** new sub-second event delivery.

### Evidence and locations

- `ytm/tui/backend.py:480` now deduplicates exact positions, replacing whole-second deduplication.
- `ytm/tui/app.py:424–427` forwards every position to both NowPlaying and LyricsPane.
- `ytm/tui/nowplaying.py:275–277` updates the progress bar and formatted time on each call.
- `ytm/tui/lyrics.py:42` scans backward over lines on each position update; it redraws only when the active index changes.

Sub-second delivery is necessary for accurate line transitions. The extra clock update calls are real, but their cost is not measured and Textual may suppress some equivalent renders internally.

### Fix instructions

1. Keep exact position values and deliver them promptly to lyrics. Do not restore whole-second backend throttling.
2. Cache the last formatted time string or integer-second/duration key in NowPlaying and skip its Static.update when unchanged.
3. Preserve progress-bar smoothness if desired; clock text and bar updates need not use the same cadence.
4. Profile before changing lyric lookup. For normal song lengths, a linear scan is likely adequate.
5. If lookup needs optimization, specify overlap semantics first. A simple bisect that checks only the latest starting line can differ from the current reverse scan when a shorter overlapping line has ended but an older interval remains active.
6. Keep seeks immediate and preserve duration-only updates even when position does not change.

### Required regression tests

Supply multiple positions within one second. All must reach lyrics; redundant clock-text updates should be omitted if that optimization is implemented. Verify forward/backward seeks and duration changes still render correctly.

### Acceptance criteria

Fractional lyric transitions remain accurate. A performance change is supported by reduced update counts or measured CPU/render cost, not just a more elaborate implementation.

---

## Adjacent observations and low-priority cleanup

### A. Pillow deprecation in the image dependency

Two tests reported a warning from `textual_image/_pixeldata.py:141`: `Image.Image.getdata` is deprecated and slated for removal in Pillow 14. Installed versions were textual-image 0.12.0 and Pillow 12.3.0.

This is dependency compatibility debt, not a lyrics failure. Before a dependency upgrade, inspect the installed/upstream implementation and changelog for replacement with the supported Pillow API. Prefer upgrading a compatible fixed release over editing `.venv` files. If no compatible release exists, document a dependency constraint only when justified by an actual compatibility test.

Validate cover-art rendering, failure placeholders, and the existing album-art tests. Do not blindly replace all `getdata` calls: return types and supported Pillow versions matter.

### B. Static-analysis warnings: disposition rather than automatic fixes

The latest deep changed-files check returned these 14 warnings. Their lines were unchanged by the lyrics feature.

| Location | Rule | Recommended handling |
| --- | --- | --- |
| `tests/test_tui.py:549` | F401 | Remove unused local `time` import in a small cleanup. |
| `ytm/tui/app.py:13` | F401 | Remove unused `Vertical` import after confirming no runtime reference. |
| `ytm/tui/app.py:233` | F541 | Remove unnecessary `f` prefix. |
| `ytm/tui/app.py:234` | F541 | Remove unnecessary `f` prefix. |
| `ytm/tui/app.py:246` | SIM103 | Optional direct negated-condition return; preserve current boolean behavior. |
| `tests/test_tui.py:8` | I001 | Sort the affected import block only. |
| `tests/test_tui.py:377` | I001 | Sort the affected local import block only. |
| `tests/test_tui.py:879` | I001 | Sort the affected local import block only. |
| `tests/test_tui.py:936` | I001 | Sort the affected local import block only. |
| `tests/test_tui.py:1592` | I001 | Sort the affected local import block only. |
| `tests/test_tui.py:1132` | PLR1711 | Remove redundant terminal return if it has no intentional explanatory purpose. |
| `tests/test_tui_backend.py:103` | UP028 | Optional `yield from` simplification preserving generator behavior. |
| `ytm/music.py:493` | BLE001 | Review the deliberate diagnostic-probe fallback before narrowing exceptions. |
| `ytm/tui/app.py:107` | RUF012 | Framework class-level BINDINGS declaration; consider an appropriate ClassVar annotation or documented suppression rather than changing lifecycle/storage. |

The tool categorized SIM103 as a security warning. That categorization is not evidence of a vulnerability: the finding is a boolean-return simplification.

The broad catch at `music.py:493` belongs to a diagnostic authentication probe. Its comment explicitly says the probe's own failure should not replace the original error. Preserve that purpose. If narrowing it, enumerate expected provider/transport exceptions and verify both signed-out detection and sanitized original-error behavior. Do not expose raw provider payloads through a “cleanup.”

### C. Compact terminals hide lyrics entirely

`YTMApp.COMPACT_WIDTH` is 100 and `COMPACT_HEIGHT` is 24. `ytm/tui/app.tcss:140–143` hides lyrics when compact mode is active. Therefore an 80-column terminal cannot display the feature in the current layout.

This is an existing deliberate layout choice, not a new sync bug. If lyrics on small terminals are desired, add an explicit lyrics-focused view or pane toggle while retaining transport shortcuts and a clear route back to the queue. Do not squeeze all existing panes into unreadable columns. Validate at 80×24 and with custom key bindings. This requires a product decision and was not implemented.

### D. Automatic following overrides manual reading on the next line

The current pane always recenters on an active-line change. A user scrolling back to read earlier lyrics will be pulled back when the song advances. This follows directly from the implementation and may be acceptable for a synchronized pane.

If browsing old lines matters, add a visible follow-mode toggle or suspend following on deliberate user scroll and provide a resume action. Keep pause behavior distinct from follow behavior. Treat this as an optional interaction improvement, not a release blocker.

## Suggested implementation sequence

1. **Lifecycle and ownership:** LYR-01 and LYR-03. Write deterministic completion-order tests first.
2. **Data/fallback correctness:** LYR-02 and LYR-05. Centralize normalization and avoid provider-specific checks scattered through widgets.
3. **Visible sync behavior:** LYR-04 and the rendering/viewport portions of LYR-07.
4. **Bound background work:** LYR-06; coordinate with lifecycle/generation handling already added.
5. **Measure before optimizing:** LYR-08 and LYR-09.
6. **Separate cleanup:** dependency warning and static-analysis nits. Keep optional UI changes as distinct decisions.

## Verification commands after fixes

Run focused tests as the affected modules change:

```bash
.venv/bin/python -m pytest -q tests/test_api.py tests/test_tui_backend.py tests/test_caching.py tests/test_tui.py
code-health check --fast --changed
```

Once the intended fixes are complete:

```bash
.venv/bin/python -m pytest -q
code-health check --deep --changed
git diff --check
```

A passing test rerun does not erase an unexplained earlier failure. Record the root cause or retain the intermittent failure as an open item. Complete the live smoke test from LYR-07 separately, and state explicitly if it remains unperformed.

## Completion checklist

Implemented 2026-09-15 (working tree on top of `93176dd`). Verification: `.venv/bin/python -m pytest -q` 420 passed; `code-health check --deep --changed` 0 errors, 2 warnings (both the pre-existing deliberate `BLE001` catches in `music.py` and `nowplaying.py`); `git diff --check` clean.

- [x] No UI completions update detached widgets. (`YTMApp._accepts_results` / `_begin_shutdown`; `on_unmount` takes the same path as `e`/`x`.)
- [x] Request generations protect A→B→A and shutdown transitions. (`LyricsFetched.generation`.)
- [x] Empty/unusable timed results reach the plain fallback exactly once. (`music._usable_lyrics`.)
- [x] Invalid timing fields cannot crash a playback event handler. (`ytm/timed_lyrics.py`, applied in `music.get_lyrics` and again in `LyricsPane.set_lyrics`.)
- [x] Active lyrics remain visible after resize, including while paused. (`LyricsPane.on_resize` / `on_show`, coalesced into one `call_after_refresh`.)
- [x] Background lyric work has an explicit concurrency and lifetime policy. (One `ytm-lyrics` thread, one replaceable pending track; backend coalesces same-video in-flight fetches; ytmusicapi's 30 s session timeout bounds a stalled fetch.)
- [x] Tests verify style and scrolling, not only `_active`. (Removing the guard, the scroll, or the active style each makes tests fail; checked by mutation.)
- [x] Plain CLI lyrics output remains compatible. (`test_plain_cli_lyrics_contract_is_text_not_records`.)
- [x] Full tests and required static analysis pass, with residual warnings explained.
- [ ] Live sync behavior is verified or clearly recorded as unverified. **Unverified: no live playback was run.** The manual smoke test in LYR-07 is still to be performed.
- [ ] Optional performance and compact-layout changes are decided independently. LYR-08 left as is (unmeasured, isolation kept); LYR-09 clock-text dedupe implemented; compact-mode lyrics (C) and follow-mode toggle (D) not implemented, product decisions pending.
