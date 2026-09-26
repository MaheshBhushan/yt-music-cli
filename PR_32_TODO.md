# PR #32 Review and Merge TODO

PR: [Stop repeating requests to YouTube, and redraw the queue once per change](https://github.com/MaheshBhushan/yt-music-cli/pull/32)

This checklist is intentionally exhaustive. It separates hard merge gates from review work, manual validation, and optional follow-ups so the PR can be evaluated without losing track of its many independent changes.

## Status as of 2026-09-13

Done (all by review session, head still `e79b478`):

- Snapshot confirmed: same head SHA, targets `main` from `safzanpirani:fewer-repeated-requests`, mergeable, no `.github/workflows/` changes, `HANDOUT.md` untouched.
- CI approved and green on 3.11, 3.12, 3.13 (run 34759729450). Local 3.11 run: 326 passed.
- Thread warning in `test_mpv_player.py` is pre-existing: `main` emits it 2x per run, PR emits it 2x. Follow-up, not a regression.
- All twelve review-map sections read against the diff and ytmusicapi 1.12.2 source. `base_headers` is a `cached_property` that skips `get_visitor_id` when the header is seeded, so the headline claim holds.
- Real CLI smoke in a disposable `$HOME` (auth symlinked read-only): cold search 0.96 s and writes `visitor.json`; warm search 0.56 s; `ytm status` with no mpv gives a one-line error; no temp files left.
- sdist + wheel built; `ytm/api.py` absent from both, CSS and Lua present; `ytm --help` works from a fresh venv; `yt_dlp` no longer imported by `ytm.cli`/`ytm.music`. No version bump in the PR.
- Decisions: accept `ytm/api.py` removal (pre-1.0, self-described transitional shim, no importers, changelog line present). Incidental fixes reviewed individually, all tested, all accepted.
- Approving review posted on GitHub with six non-blocking follow-ups. `reviewDecision` is APPROVED, `mergeStateStatus` CLEAN.
- Review worktree and disposable home removed. A stale detached worktree from an earlier session remains at `/tmp/ytm-pr32-review.703i6A`; not created by this session, left in place.

Update, later on 2026-09-13:

- Headless smoke against real YouTube and a silent mpv (`ao=null`): search cache hit 1 ms vs 0.9 s; playlist listing twice = one `library_playlists` call (counts and mixes cached); `r` refetches counts and mixes; 20-track playlist queued in 0.38 s with 5 redraws instead of 20; UI shows 20 rows; lyrics fetched once; radio queued 26 unique tracks; pause/resume/seek/next/prev correct; one client built per process; `visitor.json` written; no TUI error banner.
- Not exercised: system-volume mode (no PipeWire session in the review shell), add-to-playlist against the real account, re-auth while open. Cookie refresh itself did run (main-branch code, real home) and rewrote `auth.json`.
- Mahesh merged the PR himself with a merge commit `ee5ce3d` at 16:40. Local `main` pulled; focused 177 and full 326 tests pass; main CI run 34763354427 green.
- Follow-up issues filed: #34 thread warning + Pillow deprecation, #35 cached None count, #36 visitor.json skew/temp file, #37 STATE_PATH vs XDG, #38 cross-process lost updates, #39 broad except in `_playlist_list`, #40 dead offline cache, #41 weak `<= 1` test bound, #42 client built under lock.
- Remaining: release/version bump is a separate decision.

Previously open (now covered above):

- The interactive TUI items under "Manual smoke test": playlist load burst, add-to-playlist count in place, mixes refresh, lyrics round trip, re-auth while open, both volume modes.
- Merge procedure, post-merge verification, and filing the follow-up issues.

Follow-ups noted in the review: `_count_of` caches a `None` count; future `written_at` counts as fresh; `_write_visitor_id` can leave a temp file after a failed replace; `STATE_PATH` ignores `XDG_STATE_HOME` while `VISITOR_PATH` honours it; `<= 1` read bound in `test_tracks_for_reads_the_state_file_once`; client built under `_CLIENT_LOCK` (OAuth refresh may do network there).

## Current snapshot

- [ ] Confirm the PR still targets `MaheshBhushan/yt-music-cli:main` from `safzanpirani/yt-music-cli:fewer-repeated-requests`.
- [ ] Confirm the reviewed head SHA is still `e79b47894f994b364e7c41e44f80d0980fd6b20d`; restart the relevant review steps if the contributor pushes another commit.
- [ ] Confirm GitHub still reports the PR as mergeable and without conflicts.
- [ ] Note the review size before starting: 17 files, 1 commit, 1,043 additions, and 133 deletions.
- [ ] Preserve the untracked local `HANDOUT.md`; it is unrelated to this PR and must not be accidentally committed, deleted, or included in a merge.
- [ ] Do not use `HANDOUT.md` as architectural truth during this review. It describes an older daemon-based design and version 0.1.0, while the current repository is version 0.5.15 and uses persistent mpv directly.

## Hard merge gates

- [ ] Open the blocked GitHub Actions run: <https://github.com/MaheshBhushan/yt-music-cli/actions/runs/34759729450>.
- [ ] Review the workflow files changed by the contributor, if GitHub presents an approval warning. The PR does not currently change `.github/workflows/`, but verify this again at the current head SHA.
- [ ] Approve the fork workflow run from the GitHub Actions UI.
- [ ] Wait for all three test matrix jobs to complete on Python 3.11, 3.12, and 3.13.
- [ ] Require all three matrix jobs to pass; do not treat the local 3.11 run as a substitute for the version matrix.
- [ ] If a job fails, open its log, record the exact failing test and Python version, and determine whether the failure is deterministic before requesting changes.
- [ ] Confirm no new commits were pushed while CI ran. If the head SHA changed, approve/re-run CI for the new SHA and re-review the new diff.
- [ ] Ensure there are no unresolved review threads.
- [ ] Ensure there is at least one explicit maintainer review decision before merging this large change.
- [ ] Decide whether removal of the public/importable compatibility module `ytm/api.py` is acceptable for a patch/minor release. If compatibility is expected, request that the deletion be reverted or deferred to a breaking release.
- [ ] Decide whether the PR's unrelated correctness fixes belong in this performance PR or should be split. At minimum, review each one independently rather than accepting them under the caching headline.

## Reproduce the branch locally

- [ ] Update the PR ref without changing the current working branch:

  ```bash
  git fetch origin pull/32/head:refs/remotes/origin/pr/32
  ```

- [ ] Confirm the fetched commit:

  ```bash
  git rev-parse origin/pr/32
  git log --oneline main..origin/pr/32
  ```

- [ ] Create an isolated worktree so local `main` and the untracked handout remain untouched:

  ```bash
  review_dir="$(mktemp -d /tmp/ytm-pr32-review.XXXXXX)"
  git worktree add --detach "$review_dir" origin/pr/32
  cd "$review_dir"
  ```

- [ ] Install the branch in an isolated Python 3.11 environment and run the full suite:

  ```bash
  python3.11 -m venv .venv
  .venv/bin/pip install -e '.[dev]'
  .venv/bin/python -m pytest -q
  ```

- [ ] Record the baseline result already observed locally: 326 tests passed in 54.62 seconds on Python 3.11.
- [ ] Investigate the `PytestUnhandledThreadExceptionWarning` from `tests/test_mpv_player.py::test_enqueue_next_inserts_after_current_without_interrupting`. Determine whether it also occurs on `main`; if it does, file or note it as pre-existing. If it only occurs on the PR branch, treat it as a regression.
- [ ] Treat warnings as evidence, not noise: retain the two `textual_image`/Pillow deprecation warnings as a separate dependency follow-up unless this PR caused them.
- [ ] After review, remove only the explicit temporary worktree and directory created above:

  ```bash
  cd /run/media/maheshk/New\ Volume1/MK-solutions/ytm
  git worktree remove "$review_dir"
  ```

## Review map by behavior

### 1. Shared ytmusicapi client

- [ ] Verify `music.shared_client()` creates only one client while the auth file stamp is unchanged.
- [ ] Verify the cache key includes the auth path, nanosecond mtime, and file size.
- [ ] Consider the same-size/same-timestamp rewrite edge case. Decide whether it is acceptable on supported filesystems or whether hashing/inode information is warranted.
- [ ] Verify missing, malformed, and expired authentication failures are never cached.
- [ ] Verify a subsequent call retries client creation after a failure.
- [ ] Verify rewriting credentials invalidates the cached client.
- [ ] Verify `refresh_from_browser()` explicitly resets the client before retrying the failed catalogue request.
- [ ] Verify callers that inject `yt=` retain their old behavior and never silently use the shared client.
- [ ] Verify access to `_CLIENT` is consistently protected by `_CLIENT_LOCK` and no lock is held across a network request.
- [ ] Verify test isolation: the autouse fixture resets the shared client before and after every test and redirects `VISITOR_PATH` to `tmp_path`.

### 2. Visitor ID persistence

- [ ] Verify only browser-header authentication is seeded with a stored visitor ID; OAuth must continue through its existing client construction path.
- [ ] Verify a visitor ID is not fetched eagerly through the lazy `base_headers` property.
- [ ] Verify visitor data is stored only after the live client has actually learned a non-empty ID.
- [ ] Verify the file is written under the XDG state directory with the intended 24-hour TTL.
- [ ] Verify malformed JSON, missing keys, wrong value types, empty IDs, write failures, and expired timestamps degrade to a cache miss.
- [ ] Check clock-skew behavior: a `written_at` timestamp in the future currently remains fresh. Decide whether to accept or clamp/reject it.
- [ ] Verify the visitor file contains no cookies, authorization headers, OAuth credentials, or other secrets.
- [ ] Verify temporary visitor files are cleaned up after a failed write. `_write_visitor_id()` currently swallows `OSError`; confirm it cannot leave stale `.tmp<PID>` files indefinitely.
- [ ] Verify concurrent processes cannot corrupt the visitor file. PID-scoped temp files avoid cross-process name collision; confirm same-process writes are serialized or otherwise harmless.
- [ ] Verify the cache does not prevent a genuinely changed visitor ID from being persisted.

### 3. Search and lyrics caches

- [ ] Verify search keys include both the exact query and result limit.
- [ ] Verify the five-minute search TTL uses monotonic time.
- [ ] Verify cache hits still call `state.remember_search()` so `ytm play 3` refers to the latest displayed result set.
- [ ] Verify results are bounded to 32 search entries and eviction order is truly least-recently-used as intended.
- [ ] Verify a repeated lookup refreshes recency only if that is the intended policy; document the behavior otherwise.
- [ ] Verify cached mutable lists/dicts cannot be modified by a caller and poison later responses.
- [ ] Verify exceptions and partial responses are never cached.
- [ ] Verify lyrics are keyed by video ID, bounded to 64 entries, and cache both successful lyrics and intentional “no lyrics” results correctly.
- [ ] Decide whether lyrics need a TTL or explicit invalidation; record the choice.
- [ ] Verify an empty or missing video ID does not create a misleading shared cache entry.

### 4. Playlist and mix request reduction

- [ ] Verify an empty mix list is represented as fetched data (`[]`) rather than “not fetched yet” (`None`).
- [ ] Verify `mixes_refresh` clears mix listings, mix track lists, and cached playlist counts together.
- [ ] Verify missing remote playlist counts are requested once per session and successful values are cached.
- [ ] Verify failed count requests are not cached so a transient failure can recover.
- [ ] Verify a legitimate count of zero is cached and not mistaken for missing data.
- [ ] Verify adding to a remote playlist invalidates the old cached count and stores the fresh returned count when available.
- [ ] Verify adding to a local playlist does not touch the remote count cache.
- [ ] Verify the playlist pane updates the count in place and no longer needs a full library refresh after adding a song.
- [ ] Verify playlist creation still triggers a full refresh because a new row must be discovered.
- [ ] Verify broad exception handling in `_playlist_list` does not hide programming errors as “remote playlists unavailable.” Consider narrowing it to expected auth/network exceptions.

### 5. Batch enqueue and mpv protocol

- [ ] Verify `Player.enqueue_many()` reads the existing playlist exactly once.
- [ ] Verify it preserves input order.
- [ ] Verify it removes duplicates already in mpv's playlist.
- [ ] Verify it removes duplicates within the incoming batch itself.
- [ ] Verify it handles generators without accidentally iterating them twice.
- [ ] Verify radio, remote playlist playback, local playlist playback, and mix playback all use the batch path where applicable.
- [ ] Verify the first track is played immediately and remaining tracks are appended without interrupting playback.
- [ ] Verify an empty batch is a no-op.
- [ ] Verify titles and URLs are passed to mpv exactly as before.
- [ ] Verify a failure partway through a batch has a defined result; decide whether partial enqueue is acceptable and whether callers need an error message.

### 6. Batched status reads

- [ ] Verify `get_many()` sends all property requests before reading replies.
- [ ] Verify replies are matched by request ID and do not rely only on arrival order.
- [ ] Verify interleaved mpv event messages are ignored without losing requested replies.
- [ ] Verify unavailable properties return the caller's default just like individual `get()` calls.
- [ ] Verify duplicate property names, an empty name list, socket closure, timeout, malformed JSON, and an mpv error response are covered or handled safely.
- [ ] Verify the I/O lock prevents another thread from consuming replies belonging to the batch.
- [ ] Verify `Player.status()` maps every property to the same output types and defaults as before.
- [ ] Compare status output on idle mpv, paused playback, active playback, and stopped playback.

### 7. Queue state lookup and state-file cache

- [ ] Verify a queue render calls `state.tracks_for()` once for all entries.
- [ ] Verify unknown or URL-only mpv entries still produce a valid fallback `Track`.
- [ ] Verify current-index detection is unchanged for no current item and for one current item.
- [ ] Verify `state.load()` normalizes malformed top-level, `last_search`, and `tracks` values.
- [ ] Verify cache invalidation notices another process rewriting the file.
- [ ] Consider filesystems whose mtime resolution may not distinguish rapid same-size writes; decide whether the current mtime-nanoseconds/size stamp is sufficient.
- [ ] Verify callers honor the “shared object, do not mutate” contract. `remember_search()` and `remember_tracks()` intentionally mutate under the module lock; external callers must not.
- [ ] Verify `_WRITE_LOCK` is an `RLock`, since `remember_*` calls `load()` and `save()` while already holding it.
- [ ] Verify PID-scoped temporary names prevent cross-process collisions.
- [ ] Verify same-process thread writers remain serialized.
- [ ] Verify a failed `os.replace()` removes its temporary file without deleting the destination.
- [ ] Review lost-update behavior across separate CLI/TUI/autoplay processes. Atomic replacement prevents corruption but does not by itself prevent two read-modify-write operations from overwriting each other's logical changes; decide whether this existing risk needs a follow-up issue.
- [ ] Verify future-version unknown fields in stored tracks are ignored by `track_from_dict()` without losing required fields.

### 8. Queue redraw coalescing

- [ ] Verify the first queue change after an idle period renders immediately.
- [ ] Verify changes inside the 150 ms window schedule only one timer.
- [ ] Verify the scheduled render uses the newest payload, not the first payload in the burst.
- [ ] Verify a change arriving during `_flush_queue()` cannot be lost.
- [ ] Verify timers do not fire after the Textual app is unmounted or shut down.
- [ ] Verify `_queue_timer` is cleared even if rendering raises.
- [ ] Verify manual `_refresh_queue()` and pushed `queue_changed` events cannot reorder the UI into stale state.
- [ ] Manually load a large playlist and confirm the queue ends complete and in correct order.
- [ ] Confirm transport controls and now-playing columns remain responsive during a burst.

### 9. Observer state and system volume

- [ ] Verify initial `pause` and `volume` observer events establish correct local values before the first emitted state update.
- [ ] Verify pause changes do not call full `status()`.
- [ ] Verify mpv-volume mode uses the observed volume directly.
- [ ] Verify system-mixer mode continues querying the system volume because mpv remains pinned to 100.
- [ ] Verify the fallback volume query runs only when needed.
- [ ] Test play/pause and volume changes initiated both inside ytm and externally via media keys or the desktop mixer.
- [ ] Verify emitted `state_changed` payloads always contain valid `paused` and `volume` values, including unusual observer ordering.

### 10. Lazy yt-dlp import

- [ ] Verify normal CLI/TUI imports no longer import the full `yt_dlp` package.
- [ ] Verify every browser-cookie extraction path still imports and calls the cookie loader successfully.
- [ ] Verify automatic stale-cookie refresh still works after moving the import.
- [ ] Test missing or incompatible yt-dlp behavior to ensure the error remains actionable.
- [ ] Reproduce the claimed startup improvement with several runs if performance justification matters; do not block the merge solely on a noisy microbenchmark.

### 11. Incidental bug fixes

- [ ] Verify launching plain `ytm`, then exiting normally, returns exit status 0 and does not print `(None, None)`.
- [ ] Verify genuine TUI startup/runtime failures still return a nonzero status rather than being swallowed by the exit-code fix.
- [ ] Verify `video_id_of()` accepts normal YouTube and YouTube Music watch URLs.
- [ ] Verify it does not mistake `rv=`, `sv=`, or another parameter ending in `v` for the real `v` query parameter.
- [ ] Verify bare 11-character video IDs still work.
- [ ] Verify dropped network connections become concise user-facing errors without tracebacks.
- [ ] Verify `AuthError`/`AuthExpired` messages are not redundantly prefixed with the class name.
- [ ] Verify unexpected exceptions still preserve enough type/context to diagnose defects.

### 12. Removal of `ytm/api.py`

- [ ] Search the repository for imports of `ytm.api` and confirm none remain outside tests intentionally migrated to `ytm.music`.
- [ ] Search documentation and examples for `ytm.api` references.
- [ ] Consider third-party users: `ytm.api` is importable even if documented as a compatibility shim. Decide whether semantic-versioning policy permits deleting it in 0.5.x.
- [ ] If keeping compatibility, request a deprecated re-export module and a changelog warning instead of deletion.
- [ ] If deleting it, ensure package build contents no longer include it and the changelog clearly calls out the removal.

## Diff-quality review

- [ ] Review the single commit by semantic area instead of scrolling the entire 1,176-line diff once.
- [ ] Confirm each production behavior has a focused regression test that would fail on `main` and pass on the PR.
- [ ] Watch for tests that only assert implementation details or permit regressions with weak bounds such as `<= 1` when exactly one read is expected.
- [ ] Confirm the global autouse fixture does not conceal ordering/state leaks that real processes can experience.
- [ ] Confirm new caches have explicit ownership, bounds, invalidation rules, and failure behavior.
- [ ] Confirm comments describe current behavior without overselling guarantees.
- [ ] Check line length and formatting consistency with the existing codebase.
- [ ] Check that no debug prints, temporary paths, credentials, tokens, captured API data, or machine-specific values entered the diff.
- [ ] Verify `CHANGELOG.md` statements match the implementation and do not claim unmeasured guarantees.
- [ ] Decide whether 1,043 additions in one commit is reviewable as-is or whether to ask for a small commit split by concern. Do not require a split merely for aesthetics if the diff is coherent and verified.

## Manual smoke test

- [ ] Back up or use disposable XDG config/state directories before manipulating real auth and session state.
- [ ] Launch plain `ytm`, exit, and confirm exit code 0 with no tuple printed.
- [ ] Search for a query, backspace, and re-enter it; confirm results remain correct and the UI does not stall.
- [ ] Play a song, pause/resume, seek, change volume, and inspect `ytm status`.
- [ ] Load a large mix or playlist and confirm queue order, deduplication, current item, and UI responsiveness.
- [ ] Add a song to a playlist and confirm the displayed count changes without rebuilding the entire list.
- [ ] Refresh mixes and confirm fresh mix/count data is fetched.
- [ ] Play a song with lyrics, navigate away, return, and confirm lyrics remain correct.
- [ ] Re-run browser authentication while the TUI is open, then perform a catalogue action and confirm the new credentials are picked up.
- [ ] Test signed-out/local-playlist-only behavior and confirm remote errors do not hide local playlists.
- [ ] Restart ytm and confirm session/search metadata remains readable.
- [ ] If practical, test on both system-volume and mpv-volume configurations.

## Packaging and compatibility

- [ ] Build both source distribution and wheel from the PR branch:

  ```bash
  .venv/bin/python -m pip install build
  .venv/bin/python -m build
  ```

- [ ] Inspect wheel contents for the expected Python modules, TUI CSS, and mpv Lua script.
- [ ] Install the wheel into a fresh environment and run `ytm --help`.
- [ ] Confirm Python 3.11, 3.12, and 3.13 compatibility through CI.
- [ ] Confirm there is no version bump in this feature PR; release versioning should remain a separate maintainer decision.

## Review decision

- [ ] Summarize verified benefits, residual risks, and any requested changes in a GitHub review.
- [ ] If blocking problems exist, use **Request changes** and give each blocker a concrete reproduction or file/line explanation.
- [ ] If only optional improvements remain, approve and list them explicitly as non-blocking follow-ups.
- [ ] If requesting changes, wait for the contributor's update, verify the new head SHA, rerun targeted tests, rerun the full suite, and re-check all affected checklist sections.
- [ ] Before approval, confirm the PR description's reported 326 tests still matches CI at the final head.

## Merge procedure

- [ ] Reconfirm `main` has not moved in a way that invalidates the review; update the branch if GitHub requires it.
- [ ] Reconfirm the final head SHA, green CI, mergeability, review status, and lack of unresolved threads.
- [ ] Prefer **squash and merge** because the PR already contains one cohesive commit and the repository history uses concise outcome-oriented commits.
- [ ] Use a concise final commit title, for example: `Reduce repeated YouTube and mpv requests (#32)`.
- [ ] Keep useful implementation context in the squash commit body, but do not add Claude/Codex attribution or generated-by footers.
- [ ] Merge through GitHub.
- [ ] Delete the contributor branch only if GitHub offers it and the contributor's fork permissions make that possible; branch deletion is optional.

## Post-merge verification

- [ ] Pull the updated `main` locally without discarding unrelated local files.
- [ ] Confirm the merge commit/squash landed and `git status` still shows only the previously untracked `HANDOUT.md` unless other known work exists.
- [ ] Run the focused caching/player/TUI tests on merged `main`:

  ```bash
  .venv/bin/python -m pytest -q \
    tests/test_caching.py \
    tests/test_mpv_player.py \
    tests/test_tui.py \
    tests/test_auth_refresh.py \
    tests/test_cli_core.py
  ```

- [ ] Run the full suite once on merged `main`.
- [ ] Confirm the `main` branch GitHub Actions run passes.
- [ ] Close or link any issues fully resolved by the merge.
- [ ] Create follow-up issues for accepted non-blockers rather than leaving them buried in review comments.
- [ ] Plan the next version and release notes separately; do not tag or publish merely because the PR merged.

## Suggested follow-up issues

- [ ] Investigate and eliminate the fake mpv server's connection-reset thread warning if it reproduces on `main`.
- [ ] Decide and document whether `ytm.api` is a supported public compatibility surface.
- [ ] Harden cross-process session-state updates against logical lost updates, beyond atomic file replacement.
- [ ] Clean up visitor temp files after failed writes.
- [ ] Narrow broad playlist-list exception handling so programmer defects are not presented as remote service failures.
- [ ] Address the `textual_image` use of Pillow's deprecated `Image.getdata` path before Pillow 14, likely by tracking/updating the dependency rather than patching vendored code.
- [ ] Fix the unrelated dead offline cache noted by the contributor: downloads exist, but playback does not currently call `get_cached_path()` or `is_cached()`.

## Final completion definition

PR #32 is done only when the final reviewed SHA has passed the complete Python matrix, compatibility decisions are explicit, manual high-risk paths have been exercised, the maintainer has approved it, the PR is merged, merged `main` is green, and any deliberately deferred risks are captured as follow-up issues.
