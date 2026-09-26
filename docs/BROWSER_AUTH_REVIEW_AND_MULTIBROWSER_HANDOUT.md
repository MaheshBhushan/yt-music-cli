# YTM browser authentication: review, implementation and maintenance handout

Reviewed 2026-09-26. This document describes the implementation in this working tree, including the follow-up review fixes. It supplements the [detailed original research and implementation plan](BROWSER_SESSION_AUTH_IMPLEMENTATION_HANDOUT.md), which contains the source-by-source BitChord trace, ytmusicapi analysis, Issue #56 investigation and original acceptance plan.

## 1. Outcome and verification boundary

Public operations construct anonymous ytmusicapi clients. Account operations use a central authentication manager. Browser sessions are preferred; existing OAuth remains supported. New credentials are validated before activation. Logout is local.

There are two distinct browser experiences:

1. **Normal-profile login, the default:** open the selected installed browser normally, let the user sign in, wait for Enter in the terminal, then import only that profile's YouTube cookies and verify the account.
2. **Isolated Playwright login:** launch an owned temporary browser context, observe the signed-in YouTube Music page, capture a minimal session, verify it, and close the owned browser.

The normal-profile path fulfills the request to open Chrome's actual `Default` profile without attaching automation to it. It cannot automatically observe page completion. The Playwright path detects completion automatically but does not inherit the user's ordinary profile.

**Do not claim these are equivalent.** Chrome 136+ disallows remote debugging against its normal user-data directory, and Playwright warns against automating that profile. Do not solve this by copying profiles, killing the user's Chrome process, exposing debugging ports or weakening browser protections.

This review uses automated tests with fake browser/account boundaries. It does not certify a successful real Google sign-in or cookie decryption on every supported operating system. Use the manual release checklist below before advertising platform-wide reliability.

## 2. Source baseline and external contracts

The original research inspected BitChord at `fe198ac0bde052f8b84cfd84b1d271f8f1fcc7b2`, upstream YTM at `244a34d`, and ytmusicapi main at `bf310f7`. The Python implementation is scoped to `ytmusicapi>=1.12.2,<1.13`; the local test environment uses 1.12.2. Stable 1.12.3 source was also inspected; inspection is not a separate runtime test.

Primary references:

- [Chrome remote-debugging changes](https://developer.chrome.com/blog/remote-debugging-port)
- [Playwright persistent-context profile warning](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)
- [Playwright browsers and channels](https://playwright.dev/python/docs/browsers)
- [ytmusicapi browser authentication](https://ytmusicapi.readthedocs.io/en/stable/setup/browser.html)
- [ytmusicapi source](https://github.com/sigma67/ytmusicapi)
- [BitChord source](https://github.com/kushagrasinghx/BitChord)
- [YTM Issue #56](https://github.com/MaheshBhushan/yt-music-cli/issues/56)

BitChord's reusable idea is the transition from a real Google login to a cookie-backed Music API session. Its Android WebView, CookieManager and JavaScript callback APIs are not dependencies of this implementation. Its broader API configuration is not copied wholesale: ytmusicapi owns request construction and signing.

## 3. User command guide

### Fresh installation

```sh
pip install ytm
ytm search "Daft Punk"
```

Search needs neither OAuth nor browser login. The distribution used by this repository is `ytm`; do not assume the task's illustrative `ytm-cli` package name is the published name.

### Normal browser profile

```sh
ytm login
ytm login --browser chrome
ytm login --browser chrome --profile "Profile 1"
ytm login --browser firefox
ytm login --browser edge --authuser 1
```

The sequence is:

1. Resolve the OS HTTPS browser association unless `--browser` is explicit.
2. Resolve its executable and selected profile.
3. Open `https://music.youtube.com` with ordinary browser arguments.
4. The person signs in on Google's page.
5. The person returns to the terminal and presses Enter.
6. Read only the selected profile's applicable YouTube cookies through the existing cookie adapter.
7. Build a candidate browser session and call `get_account_info()`.
8. Show a safe account label and ask whether to use it.
9. Atomically activate the candidate only if the credential revision has not changed.

Answering no cancels; it does not reopen account selection. `--yes` skips step 8, not step 5. Native mode requires a terminal. ytm leaves the normal browser running and never submits passwords.

Chrome-family `Default` means the on-disk profile directory named `Default`. It does not mean whichever profile the user last used. `--profile "Profile 1"` explicitly changes that selection.

### Isolated browser session

```sh
pip install "ytm[login]"
ytm login --install-browser
ytm login --method playwright
ytm login --method playwright --browser chrome
ytm login --method playwright --browser edge
ytm login --install-browser --browser firefox
ytm login --method playwright --browser firefox
ytm login --install-browser --browser webkit
ytm login --method playwright --browser webkit
```

Do not pass `--profile` or `--authuser` to this mode. Select the account in the browser; the observed page supplies its account index. Google may refuse software-controlled browsers. That is a feasibility limit, not evidence that the user's password is wrong.

### Import without opening a browser

```sh
ytm login --from-browser firefox
ytm login --from-browser chrome --profile "Default"
ytm login --from-browser chrome --profile "Profile 1" --authuser 1
ytm login --from-browser
```

Explicit import keeps the existing discovery behavior. In particular, bare `--from-browser` may scan supported browsers/profiles, whereas normal `ytm login` deliberately imports the profile it opened. Prefer explicit browser/profile selection when several accounts exist.

### Account commands and logout

```sh
ytm liked
ytm library
ytm playlists
ytm account
ytm account --no-check
ytm logout
```

Missing credentials produce a short instruction to run `ytm login`. Account status never prints the cookie header or authorization value. Logout removes ytm-managed credentials and retains a logged-out record. It does not sign out Google in the normal browser.

### OAuth compatibility

```sh
ytm login --method oauth
ytm login --method oauth --client-file /path/to/desktop-client.json
ytm login --method oauth --client-id ID --client-secret SECRET
ytm auth
```

OAuth remains an advanced fallback. The legacy command is retained. New OAuth acquisition writes into a fresh managed generation, verifies the account, then activates its token path. Failed acquisition does not overwrite the previous token or client configuration. Saved desktop client configuration is reused from the active OAuth generation when applicable.

## 4. Browser support matrix

| Choice | Normal-profile login | Isolated Playwright | Important limitation |
|---|---|---|---|
| Chrome | Yes; Default or explicit profile | Installed Chrome channel | Normal profile is never automated |
| Chromium | Yes | Bundled Chromium | Bundled binary may need installation |
| Edge | Yes | `edge` maps to `msedge` | Cookie encryption can block native import |
| Firefox | Yes; configured profile | Playwright Firefox | Playwright uses its own build/context |
| Brave | Yes | No dedicated supported channel | Use native import or Chromium engine |
| Vivaldi | Yes | No dedicated supported channel | Cookie adapter/platform dependent |
| Opera | Yes; no profile selector | No dedicated supported channel | Explicit `--profile` rejected |
| Helium | Yes | No dedicated supported channel | Uses existing fork cookie adapter |
| WebKit | No | Yes | Does not mean normal Safari profile |
| Safari | No | No normal-profile support | Choose another supported browser |

Normal executable discovery checks PATH and conventional macOS/Windows installation locations. Linux association lookup uses `xdg-settings`; Windows reads the HTTPS UserChoice association; macOS reads LaunchServices associations. Firefox resolves profiles through `profiles.ini`, preferring the installation default.

These paths are implemented and unit-tested with simulated platform inputs. Flatpak, Snap, portable builds, unusual installation roots and browser-specific OS encryption remain compatibility limits. Do not label every row as manually certified.

## 5. Implementation map

| Module | Responsibility |
|---|---|
| `ytm/authentication/browser_profiles.py` | OS browser discovery, profile resolution, normal launch, selected-profile import |
| `ytm/authentication/browser_login.py` | Playwright engine selection, page observation, bounded capture, verify/confirm/commit workflow |
| `ytm/authentication/session.py` | Header allowlist, cookie validation, minimal session representation |
| `ytm/authentication/storage.py` | Versioned records, atomic file replacement, revision checks, OS locking, logout inventory |
| `ytm/authentication/manager.py` | Anonymous/authenticated factory, candidate verification, account status, logout |
| `ytm/authentication/errors.py` | Safe domain-specific errors |
| `ytm/auth.py` | Legacy adapters, cookie import, OAuth generation staging, ytmusicapi construction |
| `ytm/music.py` | Public/account routing, operation-aware errors and retry policy, client invalidation |
| `ytm/cli.py` | Flag validation, prompts, command output and browser-mode routing |
| `ytm/tui/backend.py` | Account cache invalidation and rejection of results from older credential revisions |

Keep changes at these boundaries. Do not place cookie parsing in command handlers or browser discovery in the API request layer.

## 6. Feature authentication policy

| Feature | Client/policy |
|---|---|
| Search | Anonymous, even with corrupt stored credentials |
| Song/catalogue metadata | Anonymous |
| Public playlist read/count | Anonymous by default |
| Radio/playback discovery | Anonymous where supported |
| Lyrics | Anonymous client path |
| Local playlists, queues and playback controls | Local; no Google login prerequisite |
| Liked songs | Authenticated |
| Library songs/playlists | Authenticated |
| Personal daily mixes/home recommendations | Authenticated |
| Private/account playlist reads | Explicit account route |
| Create/edit/delete remote playlists | Authenticated |
| Add/remove remote playlist items | Authenticated |
| Like a song | Authenticated |
| Account status | Stored-session inspection; optional read-only verification |

Artist/album search results remain public. This change does not invent standalone commands for every hypothetical feature in the original brief. Future subscriptions, history and library mutations must explicitly request the authenticated factory.

A public request's failure must not silently trigger OAuth or scan local cookie stores. A private playlist needs the explicit account route; do not infer expiration from every public playlist parsing failure.

## 7. Minimal browser session contract

The persisted record contains a schema version, revision, method, timestamps, source description, allowlisted headers and optional brand identity. It never contains password fields, request bodies, DOM snapshots or browser storage dumps.

The signing-cookie requirement for the scoped ytmusicapi implementation is `__Secure-3PAPISID`. A standalone `SAPISID` cookie is not treated as an interchangeable guarantee. Cookies must apply to the Music request URL, and duplicate cookie names are rejected rather than choosing an arbitrary account.

The normalized state includes the YouTube cookie header, Music origin and numeric `x-goog-authuser`. A SAPISIDHASH authorization marker selects ytmusicapi's supported browser-auth mechanism. ytmusicapi regenerates the time-dependent authorization hash; ytm does not maintain its own signer.

Playwright probes only:

- `LOGGED_IN`: must be true.
- `SESSION_INDEX`: supplies the account index.
- `DELEGATED_SESSION_ID`: optional selected brand identity.
- `VISITOR_DATA`: optional visitor header.

`INNERTUBE_CLIENT_VERSION`, `INNERTUBE_CONTEXT` and `DATASYNC_ID` are not persisted merely because BitChord uses them. ytmusicapi supplies its own client context; the supported `user=` constructor hook carries the selected brand identity.

The native cookie-import path does not read live ytcfg or automatically infer the active brand channel. `--authuser` selects the Google account index. A brand-sensitive use case must be verified with isolated page observation and the returned account confirmation.

## 8. Playwright capture details

Observation listens at browser-context scope, covering popups in the owned context. It filters for HTTPS requests to the exact Music origin and approved Music API paths before retaining allowlisted headers. Google login form submissions are outside that filter.

Captured headers are bounded to eight candidates. A candidate request's account index, delegated identity and signing cookie must match the current page/current cookie jar. Current cookies replace stale request cookies. The page is probed again after collection; a changed identity invalidates that snapshot.

The polling loop uses Playwright's event-pumping wait instead of blocking `time.sleep`. Deadlines are checked before and after capture. Browser launch and navigation receive bounded timeouts. Closing the owned browser produces cancellation. Contexts and browsers are closed in cleanup paths.

Explicit browser choices do not silently switch engines. With no isolated Chromium channel selected, launch may try bundled Chromium followed by installed Chrome and Edge. Firefox/WebKit launch failures do not fall back to a Chromium account context.

## 9. Storage, transactions and migration

Default active-record locations use `platformdirs.user_config_path("ytm", appauthor=False)`:

| Platform | Typical path |
|---|---|
| Linux | `~/.config/ytm/session.json`, respecting XDG overrides |
| macOS | `~/Library/Application Support/ytm/session.json` |
| Windows | `%LOCALAPPDATA%\ytm\session.json` |

The legacy `~/.config/ytm/auth.json` location remains readable for compatibility. Do not conflate that historical path with the new platform-specific active record.

Files use mode 0600 and newly created credential directories mode 0700 where supported. This is permission restriction, not encryption. Windows protection depends on the user directory's ACL; POSIX chmod does not implement Windows ACL hardening. System keyring storage is not implemented. Cookie extraction may invoke the browser's existing OS key-store integration, which is separate from encrypting ytm's saved record.

The active file is written to an exclusive temporary file, flushed/fsynced, and atomically replaced. A persistent lock file carries an OS advisory lock: `flock` on POSIX and byte locking on Windows. Process death releases the lock. A long-running owner is not evicted merely because the file is old.

A login captures the current revision before browser interaction or cookie extraction. Commit compares that expected revision inside the lock. Another login or logout changes the revision, so a late candidate cannot overwrite it.

Logout writes a new `method: none` tombstone and performs managed credential cleanup under one transaction. It removes managed OAuth generation directories as well as legacy credential files. The tombstone remains to prevent old legacy credentials from reappearing. Cleanup failures are reported, and ordinary user browser profiles are never deleted.

OAuth generation layout:

```text
<user-config>/ytm/
  session.json
  session.json.lock
  oauth/
    <32-hex-generation>/
      auth.json
      oauth_client.json
      oauth_desktop_client.json  # when used
```

Token references are restricted to the legacy token path or managed generation paths. A stored arbitrary path must not cause ytm to open an unrelated file. On unsuccessful staged OAuth login, the candidate generation is removed; the previous active generation is retained.

## 10. Review findings and corrections

| Finding | Correction | Regression evidence |
|---|---|---|
| Anonymous factory read credential storage first | Return anonymous client before storage access | Public factory forbids storage reads |
| Empty/malformed account response marked valid | Require a nonempty safe account name | Parameterized malformed-account tests |
| Constructor/network/parser failures escaped status checks | Report unknown/unavailable safely | Secret-bearing synthetic exceptions |
| Every 403 treated as expired login | Only strong auth rejection maps to expiry | Provider error classification tests |
| OAuth overwrote working files before verification | Stage and verify new generation before activation | Failure preserves old token/client |
| Legacy import overwrote file before account validation | Validate candidate first, commit under revision guard | Rejected legacy candidate preserves file |
| Import revision captured too late | Snapshot before extraction | Ordering test |
| Capture reused headers from another account | Match identity and signing cookie against current page/jar | Stale-account capture test |
| Login could complete after deadline | Check deadline before and after capture | Expired-deadline test |
| Synchronous sleep starved browser events | Pump Playwright while waiting | Polling implementation and browser tests |
| Brand identity lost through old visitor-cache reconstruction | New record bypasses legacy seeded-header path | Brand identity regression |
| Cookie domain substring accepted lookalikes | Exact YouTube domain applicability | Malicious suffix/lookalike tests |
| Lock age could permit concurrent writers | OS advisory ownership, no age-based stealing | Storage transaction tests |
| Logout cleanup raced credential commit | Tombstone and cleanup share transaction | Storage/revision tests |
| TUI late account response could repopulate logged-out caches | Recheck revision around serialized account routes | Stale TUI response regression |
| Browser options conflated normal profile and Playwright channel | Explicit native vs isolated modes and validation | Browser routing/profile tests |
| Non-finite timeout values accepted | Reject NaN/infinity/nonpositive values | CLI argument tests |
| Newly staged desktop client not reused | Resolve remembered client from active OAuth generation | Repeated OAuth regression |

The working tree also contains unrelated pre-existing player/TUI/lifecycle work. This handout does not claim those changes were authored or comprehensively reviewed as part of browser authentication.

## 11. Failure handling and retries

Keep these outcomes distinct:

- **Missing:** explain that this operation needs the user's account and suggest `ytm login`.
- **Expired/rejected:** suggest signing in again; preserve the record for explicit replacement.
- **Unknown:** network outage, unexpected provider response or account verification unavailable; do not delete credentials.
- **Permission denied:** the account may still be valid; do not equate all HTTP 403 responses with expiration.
- **Malformed storage:** safe invalid-record message; public operations remain available.
- **Cancelled:** retain existing credentials and clean up only the owned temporary browser/candidate.
- **Conflict:** another login/logout changed the revision; discard the stale candidate.

Provider exception bodies can include sensitive material. User-facing CLI/TUI errors must use normalized messages rather than raw exceptions or HTTP request dumps.

Do not blindly replay mutations after an ambiguous failure. A playlist write may have reached the server even when its response was lost. Existing legacy read operations may perform their bounded browser reimport retry. New browser-session records do not automatically re-extract cookies; the recovery is `ytm login`. OAuth keeps its separate supported token refresh.

## 12. Automated verification

Run from the repository virtual environment:

```sh
.venv/bin/python -m pytest -q
code-health check --fast --changed
code-health check --deep --changed
```

Focused coverage resides in:

- `tests/test_public_mode.py`: public operations without account setup.
- `tests/test_auth_commands.py`: login/logout/status and option contracts.
- `tests/test_account_commands.py`: account-listing behavior.
- `tests/test_browser_login.py`: page observation/session capture.
- `tests/test_browser_profiles.py`: normal browser discovery, profile selection, no debugging flags, engine routing, TTY and timeout validation.
- `tests/test_auth_manager.py`: factory, verification and status.
- `tests/test_auth_storage.py`: storage and transaction behavior.
- `tests/test_auth_migration.py`: legacy precedence and compatibility.
- `tests/test_auth_review.py`: reproduced review defects.
- `tests/test_provider_errors.py`: permission/expiry/network classification and mutation retry policy.

All automated authentication tests use synthetic credentials. Never add real cookies or OAuth tokens to fixtures, screenshots, logs, CI secrets or this document.

Final run totals are recorded in the review completion note below. Passing mocks establishes program behavior, not acceptance of Google sign-in in an automated browser.

## 13. Manual release checklist

Use a consenting test account; keep all resulting credentials outside the repository.

1. Fresh configuration directory: run search; confirm no browser or OAuth prompt.
2. Run liked/library without a session; confirm a concise login instruction and nonzero command status.
3. Open Chrome Default using native mode; confirm existing tabs/profile remain intact.
4. Sign in, press Enter, inspect the displayed account label and confirm.
5. Verify account, liked songs and library; confirm private playlist routing.
6. Repeat with explicit Chrome Profile 1 and account index 1; verify the correct account.
7. Repeat normal Firefox login; verify the same resolved profile is opened and imported.
8. Test isolated Chromium and one additional Playwright engine; record whether Google permits login.
9. Cancel login, decline account confirmation, close the isolated browser, and let a timeout expire; verify previous credentials remain active.
10. While login waits, run logout in a second terminal; finish the first login and verify its stale commit is rejected.
11. Go offline for account status; expect unknown, not expired, with credentials retained.
12. Revoke/expire only the test session; verify a useful login instruction.
13. Test failed OAuth login over an existing working session, then successful OAuth and repeat login using the remembered client.
14. Logout twice; confirm managed secrets are removed, tombstone retained, normal browser still signed in and public search still works.
15. Test Windows cookie-encryption and macOS permission failures; verify actionable errors without cookie/token values.
16. Inspect file permissions/ACLs and repository status without printing credential contents.

Record OS/browser versions and outcomes, not authentication material. Do not mark unsupported deployment combinations as certified based solely on fake-platform tests.

## 14. Remaining limits and future work

- Native mode's terminal prompt is blocking; its timeout is checked when the user returns. It is not an automatic login detector.
- Modern Chromium App-Bound Encryption can prevent local extraction on Windows. Firefox or a working isolated login is the practical fallback.
- Google can reject Playwright sign-in regardless of correct implementation.
- Native import cannot derive brand-channel identity from live ytcfg.
- Browser executable/profile discovery covers conventional installs, not every sandboxed or portable distribution.
- File storage is unencrypted; a future keyring backend needs availability, locked-keyring, migration and headless behavior designed explicitly.
- New browser records do not support silent reimport or guaranteed indefinite session renewal.
- The ytmusicapi bound is deliberate. Before widening it, test signing-cookie requirements, browser-mode detection, account parsing, brand identity and OAuth refresh against that version.

These limitations should remain visible in user documentation. Do not remove them merely because unit tests pass.

## 15. Review completion evidence

Final local verification on 2026-09-26:

- Full suite: **701 passed**, two existing `textual_image`/Pillow deprecation warnings, 90.36 seconds.
- Focused authentication/browser/OAuth suite during review: **295 passed** before the final additional regressions.
- `code-health check --fast --changed`: **0 errors, 69 warnings**.
- `code-health check --deep --changed`: **0 errors, 69 warnings**.
- `git diff --check`: clean.
- Installed console entry point `.venv/bin/ytm login --help`: successful, with normal-profile, Playwright and OAuth options.

Remaining static findings are mostly import ordering, unused test variables, broad exception-handler style and findings in existing player/volume/lifecycle changes. Broad adapter-boundary handlers intentionally return safe errors instead of provider exception bodies; do not remove that protection merely to satisfy a style rule. No live Google authentication or full cross-platform browser matrix was performed. No changes were committed or pushed by this review.
