# YTM CLI: full-process benchmark implementation handout

**Status:** implementation specification, not measured results.  
**Repository inspection date:** 2026-09-25.  
**Deliverable to implement later:** `benchmarks/benchmark.py`, supporting fixtures/tests, and reproducible raw results plus a generated public report.

This handout defines how to benchmark the application users actually run: the YTM Python process, mpv, and every temporary child involved in that session. It covers RAM, CPU, startup, search, playback and switching latency, long-session behavior, network traffic, and installed footprint.

Do not turn the example numbers in the original request into claims. Being below 100 MB is a hypothesis to test, not a pass condition for the measurement system. A correct result above that target is more useful than an incomplete result below it.

## 1. Required outcome and reporting contract

The first useful output must answer four questions:

1. How much total resident memory does an idle interactive session use?
2. How much total resident memory does steady playback use?
3. What is the highest observed simultaneous total resident memory during the session?
4. What is the average CPU consumption during steady playback?

The report must say:

> Memory benchmarks include the YTM CLI process, mpv, and child processes, including temporary stream-resolution and radio helpers.

Qualify this statement if process membership could not be measured completely. Do not generate an unqualified headline from a partial collection.

Use MiB internally and in the detailed report: `bytes / 1_048_576`. If a Reddit reply uses MB, convert to decimal MB: `bytes / 1_000_000`. Never change only the label. Store raw bytes so either presentation is possible.

The user's 100 MB comparison is 100,000,000 bytes if interpreted literally. Another player's measurement is comparable only when scenario, memory metric, included processes, and units are known. Do not imply a controlled comparison from someone else's unverified number.

### Definition of done

- One command runs the selected benchmark profile and writes machine-readable evidence.
- A full profile exercises all ten measurement areas in this document.
- Default interactive behavior is measured with a real PTY and real audio output.
- Controlled local fixtures separately validate measurement correctness.
- Memory totals include descendants that detach, reparent, or briefly run another Python interpreter.
- RSS, PSS, per-process high-water marks, and cgroup memory are distinct metrics.
- Latency timestamps have defined boundaries and correlation IDs.
- Failures, timeouts, missing permissions, and unsupported measurements remain visible.
- A 30-minute session and a 50-track switching run are separate experiments.
- Reports can be regenerated offline from raw artifacts.
- No measured results are required to finish this handout; implementation and actual runs are subsequent work.

## 2. Current repository architecture: anchor the implementation here

The inspected checkout contains existing uncommitted changes, including lifecycle code. These observations describe that working tree, not necessarily the released package. Re-check the symbols below before implementing and record the commit plus dirty status for every run.

| File / symbol | Current behavior | Benchmark implication |
| --- | --- | --- |
| `pyproject.toml` | Package version is `0.9.3`; console entry is `ytm.cli:main`; Python requirement is 3.11+ | Record installed version, actual interpreter, executable, and source revision separately |
| `ytm/cli.py:main`, `player` | CLI dispatch and configured mpv construction | Start the actual console command for public startup measurements |
| `ytm/tui/app.py:YTMApp.on_mount` | Constructs backend, starts listener, requests initial data, focuses search, may check updates | Mount alone does not prove the screen is usable or all background work is finished |
| `ytm/tui/backend.py:Backend.__init__` | Creates a session owner, private runtime directory/socket, and player with `spawn=True` | **Idle TUI generally includes an idle mpv**, so do not assume mpv is 0 MiB |
| `ytm/tui/app.py:on_input_changed`, `_live_search` | Debounced live search | Primary search latency starts with the final query edit; includes debounce |
| `ytm/tui/app.py:on_input_submitted` | Enter can use existing results or fetch results and play the first result | Enter-to-results and Enter-to-audio are separate, sometimes overlapping operations |
| `ytm/tui/app.py:_show_search_results` | Rejects stale sequence results and updates the results pane | Emit rendered completion only for the accepted query generation |
| `ytm/tui/search.py:SearchPane.set_results` | Mutates table rows | Row mutation is earlier than terminal rendering completion |
| `ytm/tui/backend.py:_search` | Caches search results with a TTL | Separate cache hits from network searches |
| `ytm/tui/backend.py:_load` | Uses `cache.playback_url` and sends playback command | Distinguish cached local audio from streamed audio |
| `ytm/player.py:spawn_mpv`, `Player` | Detached mpv with JSON IPC; mpv resolves watch URLs through its ytdl hook | Python command completion is not URL resolution or audio start |
| `ytm/mpv/autoplay.lua` | May spawn `python -m ytm.cli --json radio` when the last queued entry loads | Count this additional Python process and its descendants |
| `ytm/lifecycle.py:ProcessOwner` | Session token, process identity tracking, and Linux subreaper handling | Reuse ownership information where suitable; it is not a replacement for measurement isolation |
| `ytm/volume.py` | System mixer integration can invoke utilities | Include temporary mixer helpers in totals |
| `ytm/cache.py` | Offline audio cache and eviction | Record cache state, hits, baseline size, and final size |
| `ytm/config.py` | Config path is based on `Path.home()` | XDG overrides alone do not fully isolate the app |

Important distinctions:

- The TUI remains resident alongside its player. One-shot CLI playback may outlive the issuing CLI process. Benchmark both only as separate profiles.
- A TUI creates its own IPC endpoint; setting `YTM_IPC_PATH` alone does not select that endpoint. Add an opt-in metadata event exposing the actual session endpoint to the collector.
- The configured PO-token provider may be an already-running service outside the process tree. Disclose it and its version/location. If benchmark-dedicated, collect its footprint separately; if shared, do not pretend its total RAM is uniquely attributable to YTM.
- PipeWire/PulseAudio, the terminal emulator, compositor, DNS services, and the OS are shared infrastructure. Exclude them from the stated process-tree total and disclose that boundary. Report a separately labeled system-wide delta only if independently measured.
- Do not disable lyrics, album art, system volume polling, radio, or update checks in the headline profile to obtain a smaller result. Diagnostic profiles may vary them explicitly.

## 3. Proposed repository layout and command interface

All commands and files in this section are **proposed interfaces**, not currently implemented functionality.

Start with a small implementation. Keep collector, scenarios, and reporting in the script initially if that stays readable; split by responsibility when needed.

```text
benchmarks/
  benchmark.py                 # CLI orchestration; outside measured target
  scenarios.json               # queries, track IDs, queue, deadlines, seed
  README.md                    # short operator instructions
  fixtures/                    # generated/licensed local audio description
  tests/
    test_accounting.py         # totals, quantiles, identity, missing data
    test_protocol.py           # event correlation, partial writes, EOF
    test_process_scope.py      # detached and short-lived children
    test_scenarios.py          # workflow failures and fixture playback
  results/                     # ignored local artifacts, not credentials
    <run-id>/
      manifest.json
      events.jsonl
      samples.jsonl
      processes.jsonl
      trials.jsonl
      footprint.json
      network.json
      summary.json
      report.md
      memory-by-track.csv
```

Proposed commands:

```bash
python benchmarks/benchmark.py preflight
python benchmarks/benchmark.py run --profile smoke
python benchmarks/benchmark.py run --profile public --startup-runs 10 --search-runs 50 --playback-runs 30
python benchmarks/benchmark.py run --profile longevity --tracks 50 --steady-seconds 30
python benchmarks/benchmark.py run --profile endurance --duration-minutes 30
python benchmarks/benchmark.py run --profile full
python benchmarks/benchmark.py report benchmarks/results/<run-id>
python benchmarks/benchmark.py compare <baseline-run-directory> <candidate-run-directory>
```

`full` must expand into a documented list of sub-runs, not one enormous scenario with ambiguous boundaries. It should include startup, interaction, playback, switching, longevity, endurance, footprint, and network collection or an explicit unsupported result for the latter.

Required CLI options:

| Option | Purpose |
| --- | --- |
| `--ytm-executable PATH` | Select actual installed entry point |
| `--profile NAME` | Select a named, versioned workload |
| `--scenario-file PATH` | Provide reproducible query/track workload |
| `--output DIR` | Keep artifacts outside target cache/home |
| `--sample-ms 100` | RSS cadence; effective cadence recorded |
| `--pss-ms 1000` | Slower PSS cadence to control overhead |
| `--cpu-window-ms 1000` | Public peak CPU aggregation window |
| `--seed INTEGER` | Query and track ordering |
| `--timeout-seconds N` | Operation deadline, overridden per scenario if needed |
| `--audio-observation ipc|loopback` | Declare playback endpoint method |
| `--network-mode none|attributed` | Select capability, never imply unavailable accounting |
| `--terminal-size 120x40` | Fix rows/columns and capture TERM/color/image settings |

Use structured arguments with `subprocess`, not shell interpolation. Do not add benchmark-only dependencies to normal application runtime requirements. A standard-library Linux `/proc` collector is sufficient to start; optional `psutil` can support portability but does not solve process ownership or short-lived-child accounting by itself.

## 4. Preflight, isolation, and reproducibility

### 4.1 Record the machine and software

Write a manifest before launching the target:

- Run ID, UTC start, monotonic clock implementation, schema version.
- OS distribution, kernel, architecture, cgroup version/controller availability.
- CPU model, logical CPU count, affinity/cpuset, governor if readable.
- Physical RAM and swap configuration; memory pressure at run start.
- AC/battery state and power profile; thermal throttling observations if available.
- Python version and executable, YTM package version, commit, dirty state.
- Dependency versions/lock snapshot, mpv, yt-dlp, JavaScript runtime, token-provider versions.
- Audio server, device/backend, sample rate if available, and output route.
- Terminal size, TERM, rendering/art settings, locale, and whether display is a PTY sink or visible terminal.
- Ethernet/Wi-Fi, VPN/proxy presence, coarse region if volunteered, and UTC test window.
- Authentication mode only; never credentials, cookies, or account identity.
- Effective sanitized YTM configuration and relevant mpv settings.
- Search/lyrics/art/audio cache state, update-check state, radio setting.
- Workload identifiers/hash, random seed, requested/completed/failed run counts.
- Collector revision, configured intervals, achieved intervals, measurement limitations.

Do not assume the local machine description matches the execution host: query it. Do not restart desktop services or change machine power policy for this benchmark.

### 4.2 Isolate app state without breaking desktop audio

Create a private temporary home and separate cache/config/state directories. Pass a child-specific environment dictionary; do not reassign the operator's shell `HOME`. Preserve the session bus and audio connection environment needed by the selected output route.

Inspect all path definitions before relying on XDG variables. In the current code, some paths use `Path.home()` while others honor XDG overrides. Launching the child with a temporary home is the more complete isolation boundary; apply it consistently to mpv, yt-dlp, and helper children.

For authenticated tests, stage only the necessary files into the private directory with restricted permissions and explicit operator-selected credentials. Keep that directory out of report output. Do not publish auth files, raw environment dumps, browser profiles, signed stream URLs, or debug logs containing secrets.

Default runs must not refresh or replace packages mid-measurement. If auto-update is enabled in a user's config, stage a config with auto-install disabled and disclose this experimental control. Separately record whether routine update checking runs; resetting its state can change startup background traffic.

### 4.3 Preflight failures

Check these before counting a trial:

1. The selected `ytm` executable belongs to the intended environment.
2. mpv and stream-resolution tools are present.
3. The target audio device works with a known local fixture.
4. The event channel and PTY are operational.
5. Process-scope containment works, including detached descendants.
6. RSS is readable; PSS availability is separately recorded.
7. cgroup CPU usage is readable if this is the primary accounting mode.
8. Online profiles can complete a representative search and playback.
9. The workload has sufficient playable tracks for the selected profile.
10. The collector has enough disk space and can write artifacts.

Separate preflight traffic and warmups from recorded trials. They warm caches and network state, so they must not precede a claimed OS-cold trial without resetting that state in a controlled environment.

## 5. Process containment: the central correctness requirement

### 5.1 Preferred Linux design: a dedicated cgroup v2 target scope

Place the YTM target in a fresh dedicated cgroup **before Python application initialization can spawn children**. Leave the collector, report generator, PTY driver, packet monitor, and audio monitor outside it.

A launcher may join a delegated cgroup and then `exec` the console entry point, or a supported user-systemd transient unit may own the target. The implementation must verify containment and permissions, not assume either mechanism is available. A launcher blocked on a control pipe can let the collector finish setup before releasing `exec`; document whether its startup overhead is included.

Do not simply move an already-running YTM PID into a group after it has spawned mpv. Already-created children can remain outside. Likewise, do not put the whole benchmark runner in the target group and accidentally count the measuring tools.

For membership snapshots, walk descendant cgroup directories and read their `cgroup.procs`; the root group's file alone is not recursive. Deduplicate process IDs. Session detachment does not itself escape a cgroup, but a child explicitly starting a separate service may do so. Detect and report known external dependencies.

Capture final counters before the group disappears. Each independent trial uses a fresh group, which also avoids ambiguous high-water-mark resets. cgroup CPU and memory counters are auxiliary measurements with different semantics; see sections 6 and 7. These interfaces are documented by the [Linux cgroup v2 reference](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html).

### 5.2 Fallback membership collector

If cgroup placement is unavailable:

- Start with the owned root PID and recursively discover descendants.
- Identify processes by `(pid, start_time_ticks)`, not PID alone.
- Keep known descendants tracked after reparenting.
- Incorporate session ownership metadata where accessible.
- Do not rely only on process groups: mpv and extractors can create new sessions/groups.
- Do not find targets by executable name; another mpv or Python may belong to the user.
- Include helpers spawned by mpv, not just direct children of Python.
- Mark the accounting mode `proc-descendant-fallback` and explain its limits.

Polling can miss an entire short-lived process. A high-frequency recursive scan does not make this impossible. Lifetime process tracing or cgroup CPU accounting can close some gaps; sampled RSS still has a temporal limit. Never label fallback coverage complete merely because no child was observed.

### 5.3 Component classification

Each PID belongs to exactly one additive component:

| Component | Includes |
| --- | --- |
| `ytm_main` | The interactive Python application process |
| `mpv` | Owned mpv processes |
| `helpers` | yt-dlp, JavaScript runtimes, radio Python process, mixer utilities, and all other owned children |
| `total` | Sum of the three components at the same sample |

Additionally provide non-additive diagnostic tags such as `python_all`, `resolver`, `radio`, `mixer`, and `unknown`. `python_all` overlaps the main process and Python helpers; do not add it to `total` again. Keep unknown owned processes in the total even if classification fails.

Record births, exits, executable category, identity, parent identity when known, and peak-observed memory. Avoid storing raw command lines if they could contain auth or signed URLs.

## 6. RAM: definitions, acquisition, and aggregation

### 6.1 Metrics to collect

| Metric | Meaning | Use |
| --- | --- | --- |
| Current process RSS | Resident mappings attributed to a process, including shared pages | Familiar component reading |
| Current process PSS | Shared resident pages divided proportionally among sharers | Better estimate of apportioned physical memory |
| Process high-water RSS | Linux `VmHWM`, when available | Per-process lifetime peak, with kernel accounting limitations |
| Sampled process peak RSS | Maximum observed current RSS for that process identity | Shows what this collector saw |
| Total sampled RSS | Sum of current RSS of all target processes in a snapshot | Headline conservative process-tree RAM |
| Total sampled PSS | Sum of PSS in a PSS snapshot | Companion physical-memory attribution |
| Peak total sampled RSS | Maximum simultaneous total RSS snapshot | Session headline peak, explicitly sampled |
| cgroup current/peak memory | Memory charged to target cgroup | Separate diagnostic; includes charges beyond process RSS |

RSS is resident physical memory, but summing RSS double-counts shared mappings. Therefore the total is not a precise measure of unique physical pages. State this once near public results and provide PSS when possible.

Use `/proc/<pid>/smaps_rollup` for PSS where accessible and a fast `/proc` RSS source for high-frequency samples. Verify field units and multiply KiB-style values by 1024. Fast RSS sources can have accounting inaccuracies; compare them against rollup readings during collector validation. Access can fail because a process exited or permissions restrict inspection. These are different outcomes.

### 6.2 Correct formulae

For snapshot `k` with owned process set `P(k)`:

```text
rss_total(k) = sum(rss(p, k) for p in P(k))
pss_total(k) = sum(pss(p, k) for p in P(k))  [only if coverage is complete]
peak_total_rss = max(rss_total(k) across the session)
```

**Never compute total peak by summing per-process peaks.** Their peaks may occur at different times, and exited helpers would inflate a nonexistent simultaneous total.

**Never call cgroup `memory.peak` “peak RSS.”** Keep its name and semantics separate.

**Never treat missing PSS as zero.** Output `null`, a coverage count, and reason. A process known to be absent can correctly contribute zero; a process that exists but cannot be read cannot.

### 6.3 Cadence and snapshot integrity

Initial proposal:

- RSS/process membership every 100 ms throughout the session.
- PSS every 1 s, plus explicit checkpoint samples.
- Optional 20–50 ms RSS diagnostic runs around loading after measuring collector overhead.
- Raw cumulative CPU readings at the RSS cadence; public CPU peaks over 1 s windows.

A `/proc` walk is not atomic. Record sample start/end, read duration, membership at discovery, and read failures. Recheck process start time when required to prevent PID reuse from joining unrelated data. Do not carry an exited process's last RSS forward indefinitely. Preserve a partial subtotal only with an explicit incomplete flag; exclude incomplete totals from an unqualified summary.

PSS and RSS sampled at different times must not be combined into one supposed simultaneous observation. Each needs its own timestamp, duration, and coverage.

For each stable window, publish time-weighted mean RAM, median, P95, and sampled maximum in the detailed report. The public table uses time-weighted mean total RSS except the explicitly labeled peak row. For an instantaneous checkpoint, publish the sample timestamp and offset from the boundary event.

### 6.4 Required checkpoints

- First measurable sample after launch, with elapsed milliseconds.
- UI-ready sample.
- Idle main-screen window after initial background work settles.
- Active search window.
- Search-results display window with no selection made.
- Track-load window from selection to playback endpoint/failure.
- Steady playback from 30 to 60 seconds after confirmed start.
- Populated queue/playlist window, with queue length recorded.
- Equivalent steady windows after tracks 1, 5, 10, 25, and 50.
- Final quiescent window and cleanup state.
- Highest complete total-RSS snapshot across all recorded activity.

The peak row must include timestamp, scenario, track/trial ID, and component breakdown at that exact sample.

## 7. CPU: include work done by children that have already exited

Prefer cumulative target-cgroup CPU usage for Linux totals. It retains CPU charged to short-lived members even when polling never sees their PIDs. Per-process user+system CPU counters supply component diagnostics but can undercount exited helpers.

Use one-logical-core normalization:

```text
cpu_percent = 100 * delta_cpu_seconds / delta_monotonic_wall_seconds
```

100% means one logical core fully occupied. Multithreaded or multi-process totals can exceed 100%. If also showing a percentage of all CPUs, divide by the actual available CPU count and label it separately.

For a phase:

```text
phase_average_cpu = 100 * (cpu_counter_end - cpu_counter_start) / phase_duration
```

Read boundary counters close to phase events and record the timing offset. If only periodic readings are available, document interpolation or boundary uncertainty. Never average irregularly spaced CPU percentages without time weighting.

Do not add recursive child CPU counters to counters already counted for the child, and do not sum cgroup CPU and per-PID CPU: those are alternative totals/diagnostics.

Report:

- Idle average over at least 30 s.
- Search average and operation CPU time.
- Track-load average and 1 s-window peak.
- Steady playback average over 30–60 s after start.
- Manual skip average and 1 s-window peak.
- Whole-session peak over a declared window.

A 100 ms peak and a 1 s peak answer different questions. Report the window alongside the number. Short operations should include CPU milliseconds per operation; a 1 s window overlapping idle is not a pure operation CPU average.

Measure collector CPU and memory separately. Do not subtract an arbitrary “observer overhead” estimate from the target. Compare instrumentation-on/off runs and disclose the observed perturbation.

## 8. Event instrumentation and real terminal automation

### 8.1 Minimal opt-in event channel

Add a small internal benchmark event helper, disabled by default and without new runtime dependencies. A proposed `YTM_BENCH_EVENT_FD` enables an inherited local channel supplied by the runner.

Events must have:

```json
{
  "schema_version": 1,
  "run_id": "generated-id",
  "source": "ytm-main",
  "pid": 12345,
  "monotonic_ns": 123456789000,
  "event": "search.results_rendered",
  "operation_id": "search-0007",
  "generation": 7,
  "fields": {"result_count": 20, "cache_hit": false}
}
```

That is a schema example, not a recorded event. Query and track aliases should be sufficient in public artifacts.

Use bounded messages, handle partial reads, continuously drain the channel, and keep producer work small. If multiple writers share a pipe, constrain message size to the platform's atomic pipe-write bound or use a single synchronized writer. A socket protocol is another option. Do not let mpv inherit a writer descriptor accidentally and keep EOF open forever.

A full/broken event channel must not freeze playback. Track dropped events; invalidate any latency requiring a missing event. Do not silently replace precise event times with guesses. Use the same host monotonic timebase for producer and collector; record receipt timestamps too.

### 8.2 Events and hook locations

| Event | Suggested hook / boundary |
| --- | --- |
| `launch.requested` | Runner immediately before target launch/release |
| `python.entry` | Earliest practical CLI entry; diagnostic, not full startup start |
| `player.spawn_requested` | Before actual mpv spawn |
| `player.ipc_ready` | Successful IPC connection; include private endpoint metadata |
| `ui.first_frame` | After initial Textual refresh |
| `ui.ready` | Input focus, viable backend, first interactive frame, input round-trip succeeds |
| `ui.background_ready` | Defined initial queue/library requests complete or visibly fail |
| `search.input_final` | Final query-edit event accepted |
| `search.requested` | Before backend search dispatch |
| `search.api_completed` | Backend return, including explicit cache hit/miss |
| `search.results_rendered` | Accepted generation rendered after pane update |
| `play.requested` | Song-selection/transport action accepted |
| `play.command_sent` | Before mpv command write |
| `play.command_ack` | mpv acknowledges command; not audio-ready |
| `track.start_file` | mpv observer event |
| `track.file_loaded` | mpv observer event |
| `track.playback_restarted` | mpv observer event, correlated to current load |
| `track.position_advancing` | Playback clock advances for the new unpaused audio track |
| `track.audio_detected` | Optional isolated audio-monitor detection |
| `track.end_file` | mpv observer event with reason |
| `operation.failed` | Error/timeout with sanitized reason |
| `session.shutdown_complete` | Owned processes exited; final state collected |

Use an after-refresh callback to mark rendered results, and validate its ordering with the pinned Textual version. A callback is an application rendering boundary, not proof that a human's display has physically repainted. Textual exposes app lifecycle and test facilities in its [App API](https://textual.textualize.io/api/app/) and [testing guide](https://textual.textualize.io/guide/testing/).

### 8.3 PTY driver

Launch the actual `ytm` entry point inside a PTY with fixed dimensions. Drain output continuously so backpressure does not stall the app. Feed ordinary keys/paste events; wait for correlated app events before the next action.

Use explicit deadlines, not long arbitrary sleeps, except deliberate observation windows. Verify focus before sending shortcuts: `n` typed in the search box changes the query rather than skipping a song.

Textual's headless test mode is useful for correctness tests, but it suppresses terminal output and is not the headline rendering benchmark. Keep any backend-only or headless timing clearly separate.

## 9. Startup benchmark

### 9.1 Endpoint

Start: runner requests launch of the actual console executable.  
End: backend is available, the first interactive frame is rendered, and a harmless input round-trip proves the UI accepts input.

Also record background-ready time separately. Waiting for every remote playlist or update check is a different endpoint from “interface usable.” A usable signed-out UI may have an empty library; an error screen caused by unavailable mpv is not a successful startup.

### 9.2 Cold versus warm

Do not call a fresh Python process “OS-cold” just because the previous process exited.

| Label | State |
| --- | --- |
| Fresh app state | New app profile/cache; kernel file cache unspecified |
| Warm startup | Fresh process, prepared equivalent profile, recently used program files |
| OS-cold startup | Explicit controlled boot/cache condition, procedure documented |

Run at least 10 independent startup trials per reported condition and report median, count, failures, minimum, and maximum. P95 from only 10 trials is unstable; do not emphasize it.

For OS-cold trials, use a dedicated controlled machine/VM procedure with independent resets. Do not drop system caches or reboot the user's laptop automatically. If true cold trials are unavailable, report fresh-app-state and warm medians honestly and mark OS-cold unavailable.

Stop the entire owned scope between runs and verify that no mpv is reused. Record inter-run spacing and order. Warmup trials are excluded with their count disclosed. Keep auth mode and intended startup workload equivalent across conditions.

## 10. Search benchmark

### 10.1 Measure the application's actual semantics

The current UI live-searches after typing pauses. Enter can start the first song. Therefore implement three separate measurements:

1. **Live search end-to-end:** final query edit → accepted results rendered, including debounce.
2. **Search service latency:** backend request → backend response, separating cache hit/miss.
3. **Submit-to-play path:** Enter with pending/no results → results rendered → playback endpoint.

For the user's requested Enter-to-results measurement, send Enter before live search has completed and observe the matching result event. Label the scenario “submit while search pending”; playback may immediately follow. Do not mix this with a results-only RAM window. Obtain the latter through live search without Enter.

When results already exist, Enter may do no search at all. Such trials belong to playback selection timing, not a zero-millisecond search result.

### 10.2 Workload

Prepare at least 10 queries covering artist names, exact titles, broad phrases, non-ASCII text, long queries, and a no-result case. Use a deterministic shuffled order and at least 50 trials for a useful initial latency distribution.

Separate:

- Uncached queries in fresh/equivalent backend state.
- Warm backend cache hits within its TTL.
- Repeated requests after expiry, if testing TTL behavior.
- Signed-in and signed-out behavior where relevant.

Do not append random junk solely to force cache misses; it changes the search workload. Prefer controlled session/cache resets or predeclared distinct real queries. Record actual cache classification rather than assuming it from query order.

Report median, nearest-rank P95, completed/attempted count, timeout/error rate, and result count. Include debounce in the end-to-end number and expose it separately for optimization. A stale response must never satisfy a newer query's completion event.

## 11. Playback startup and track switching

### 11.1 Playback timing ladder

```text
selection accepted
  -> command sent
  -> command acknowledged
  -> stream-resolution activity
  -> file loaded
  -> playback restart / clock advancing
  -> audio observed at output monitor (if available)
```

mpv owns resolution in this architecture. Do not insert Python-side URL resolution merely to obtain cleaner timings; that would benchmark a different program.

Use mpv JSON IPC events/properties for diagnostics. `file-loaded` is not first audible audio. `playback-restart` also occurs in contexts other than initial load, so match it to a new file/playlist-entry generation and require an active unpaused audio track with advancing playback state. Event semantics are defined in the [mpv stable manual](https://mpv.io/manual/stable/).

Headline labels:

- With IPC only: **selection-to-playback-ready (IPC proxy)**.
- With validated per-stream audio monitoring: **selection-to-first-observed-audio**, with detector method.
- Never silently rename the IPC proxy “time-to-audio.”

For URL-resolution duration, instrument a version-tested mpv hook or sanitized resolver lifecycle events. Starting a yt-dlp process and observing its exit is a useful resolver subprocess interval, but may include more than URL resolution. Log parsing is diagnostic, brittle, and potentially sensitive. If an exact stage cannot be observed, report it unavailable instead of assigning the entire load time to resolution.

### 11.2 Audio observation validation

For a stronger endpoint, monitor only the benchmark player's output stream or an isolated audio route. Keep the monitor outside the target cgroup. Record audio backend latency and whether the route differs from ordinary speakers/headphones.

A detector should have a predeclared noise threshold, minimum consecutive above-threshold frames, sample rate, and detection uncertainty. Calibrate with a generated local tone with known onset. Do not count another app's audio or the old track's tail as the new track.

For online music, leading silence changes first-nonzero-sample time. Preserve it in a user-perceived measurement or disclose a content-normalized fixture result separately. Bluetooth/output buffering also adds delay. A null audio sink or decoding-only run does not establish real audible latency.

### 11.3 Trial plan

Run at least 30 online playback-start trials across multiple predeclared tracks; 50+ improves tail estimates. Record stream format/codec/bitrate when observable, cache state, success, timeout, and retry count. Separate first player launch from track loading with an existing idle player.

Report selection-to-ready/audio median and P95, plus stage timings when valid. Do not include the human delay between browsing results and selecting a song in system playback latency.

### 11.4 Switching scenarios

| Scenario | Start | End / validation |
| --- | --- | --- |
| Normal next | Next action accepted | New expected entry reaches playback endpoint |
| Previous | Previous action accepted | Expected prior entry reaches endpoint; verify it did not merely restart current track |
| Select another result | Row selection accepted | Selected result reaches endpoint |
| Select queue entry | Queue action accepted | Requested entry reaches endpoint |
| Automatic queue transition | Natural previous EOF boundary | Next entry reaches endpoint |
| Audible queue gap | Last old-track audio sample | First next-track audio sample on validated route |

Collect at least 20 transitions per category where practical and publish each count. Do not pool the categories into one median. Record queue length, whether radio is fetching more entries, and cache state.

Natural EOF transitions should be tested with short local fixtures for automation and real online tracks for representative behavior. Seeking near the end is a separately labeled accelerated test; it can change buffering and is not equivalent to natural playback. A manual next command is never evidence of automatic queue-transition latency.

## 12. Long-running memory and endurance

Implement two profiles:

**Track-count longevity:** 50 successful starts in one app/player session, normally 30 s of playback per track before the next action. Record checkpoints after tracks 1, 5, 10, 25, and 50. Fifty failed attempts do not count as fifty played tracks.

**Time endurance:** at least 30 minutes of continuous wall-clock use, with normal queue transitions and a documented schedule of searches/navigation. Keep the app open throughout. Publish actual elapsed time and successful track count.

For growth diagnosis, run both a repeated small set and a larger distinct-track set. They exercise bounded reuse and expanding caches differently. Do not clear caches, force garbage collection, restart mpv, or shrink the queue between checkpoints unless that is the explicit experiment.

At each checkpoint record:

- Main Python, mpv, helper, total RSS and PSS.
- Comparable steady-window mean/median and peak since prior checkpoint.
- Queue length and tracks retained in visible tables.
- Search/lyrics/art cache counts and sizes when cheaply observable.
- File descriptor count, thread count, live child count.
- Cache directory logical/allocated size.
- CPU average, playback errors, and retries.

Compute end-minus-initial steady RAM and an exploratory MiB-per-successful-track slope. Plot components separately to locate growth. Repeat the long run at least three times before making a strong stability claim.

Memory retention alone does not prove a leak: Python allocators, mpv buffering, artwork, metadata, and an intentionally growing queue can all increase RSS. If growth persists with a fixed workload, use a separate diagnostic run with allocation tracing. `tracemalloc` tracks Python allocations, not all native memory or mpv, and its overhead belongs outside the headline baseline.

Do not claim “no memory leak” from 50 songs. Say what was observed over the tested duration and workload.

## 13. Network accounting

Network accounting must have a declared scope and byte definition. `/proc/<pid>/net/dev` reflects network-namespace interfaces; it is not per-process traffic. Host interface deltas on a busy laptop cannot reliably isolate YTM.

Preferred implementation is optional cgroup/socket-attributed accounting with an available OS tracer, or a benchmark-dedicated network namespace whose routing and capture effects are documented. Treat elevated tracing requirements as an optional capability; missing permissions should produce `unavailable`, not silently fall back to whole-host bytes.

Separate at least:

| Category | Notes |
| --- | --- |
| Search/API | Search, library, lyrics, radio, and related service traffic; subcategorize when attribution supports it |
| Resolver/control | yt-dlp extraction and token-provider calls |
| Artwork/update | Cover images and update checking |
| Audio stream | Media payload/transport attributed to the stream |
| Unclassified | Retain bytes that cannot be classified confidently |

Process role alone is insufficient: an mpv-associated resolver or shared HTTPS connection may carry control traffic. Encryption can prevent exact semantic classification. Use app-level byte instrumentation for useful payload counts, but label those as application bytes rather than wire bytes.

Capture TX and RX, DNS if in scope, loopback traffic, retries, and accounting layer. Explain whether headers, TLS overhead, retransmits, and shared DNS daemon traffic are included. Avoid double counting namespace loopback packets or the same bytes at multiple hooks.

Report:

- Search/API bytes per operation under declared attribution.
- Non-audio overhead over a specified session.
- Audio bytes and observed average rate over a specified playback interval.
- Unclassified byte count and classification coverage.

Do not derive exact stream bytes from bitrate × duration; buffering and transport overhead make that an estimate. Audio bandwidth depends on the selected format, bitrate, codec, buffering, and network behavior. If audio cannot be separated, publish measured total traffic and mark the requested split unavailable.

Keep packet captures private by default. Public reports need aggregates, not account traffic or signed URLs.

## 14. Installed footprint and cache growth

Use a clean non-editable installation environment for footprint measurement. An editable checkout measures source layout rather than the installed package. Do not count benchmark tooling as a YTM runtime dependency.

Report these separately:

1. YTM distribution files and metadata.
2. Direct and transitive Python dependencies, with versions.
3. Python interpreter/base environment footprint, separately labeled.
4. mpv package/binary footprint.
5. mpv's native dependency closure and already-shared dependencies.
6. JavaScript runtime and token-provider service/container, if required by the tested setup.
7. Cache before/after: audio, art/metadata where present, and downloads.
8. State/logs before/after, separately from cache.

For Python files, use distribution metadata/RECORD ownership to enumerate installed files and identify missing or shared files. If executable scripts are outside `site-packages`, include them. Measure generated bytecode separately or consistently as part of post-use footprint. Identify benchmark-created files and exclude them.

Record both apparent bytes and allocated disk blocks. Deduplicate hardlinks/shared owned paths when summing a dependency closure. Package-manager “installed size” estimates are useful but may not equal filesystem allocated size.

For mpv and native dependencies, query the actual package manager and retain the package list. Provide both standalone closure size and incremental newly installed size if available; a system library already installed for other software is not zero bytes, but it may add zero incremental installation cost.

Do not equate wheel download size, source repository size, virtualenv size, and installed runtime footprint. They answer different questions. Cache results must specify playback count/duration and whether audio was streamed or explicitly downloaded offline.

## 15. Raw data and deterministic report generation

### 15.1 Samples

A sample record should contain:

```text
schema_version, run_id, sample_id
monotonic_start_ns, monotonic_end_ns, elapsed_ns
phase_ids, active_operation_ids
membership_mode, identities_seen, identities_read, completeness
rss_bytes: {ytm_main, mpv, helpers, total}
pss_bytes: {ytm_main, mpv, helpers, total} or null
pss_timestamp_ns, pss_coverage
cgroup_cpu_usage_usec, cgroup_memory_current_bytes, cgroup_memory_peak_bytes
per_process: [{pid, start_ticks, component, rss_bytes, pss_bytes, cpu_seconds, vmhwm_bytes}]
read_errors, collector_lag_ns
```

Do not force overlapping activity into only one phase. Radio fetching can overlap steady playback, and search can overlap audio. Preserve interval events so reports can distinguish scripted scenario boundaries from concurrent background work.

### 15.2 Trial records

Each operation record needs scenario, input alias, intended target, actual target, cache classification, timestamps, endpoint method, success/failure/timeout, elapsed time if valid, and retry count.

Timeouts are censored observations, not successful samples equal to the deadline. Report success-conditioned median/P95 with explicit failure rate. If failures prevent an honest tail claim about all attempts, say so. Never drop a slow successful result as an “outlier” without a predeclared rule and retained evidence.

### 15.3 Statistics

- Use monotonic differences for durations; UTC is metadata only.
- Use `statistics.median` for latency median.
- Define P95 as sorted sample at `ceil(0.95 * n) - 1` for `n > 0`.
- With no valid samples, return `null`, never 0.
- With small sample counts, label tail estimates weak.
- RAM and CPU phase averages are time-weighted; latency distributions are per operation.
- Do not mix cold/warm, cached/uncached, streamed/local, or IPC/audio endpoints.
- Retain per-run results; across-session summaries should not let the longest session dominate accidentally.
- Round only at rendering time: e.g. RAM to 0.1 MiB, CPU to 0.1%, latency to integer ms.

### 15.4 Exit and cleanup behavior

Write partial artifacts on interruption, mark run status, collect final counters, close the UI normally when possible, then terminate only the owned scope if needed. Verify it is empty. Never run `pkill mpv` or kill all Python processes.

A report command should be read-only and deterministic for the same raw data/schema. Separate collection failures from benchmark failures in the exit status and manifest. A capability may be unsupported without making every unrelated measurement unusable.

## 16. Public README table and Reddit reply

Generate this table only after real runs. `—` means not applicable; `unavailable` means requested but unmeasured. Avoid an ambiguous blank cell.

| Scenario | Total RSS (MiB) | CPU (% of one core) | Latency (ms) |
| --- | ---: | ---: | ---: |
| Startup | `<mean>` | `<mean>` | `<median; condition>` |
| Idle | `<mean>` | `<mean>` | — |
| Search | `<mean>` | `<mean>` | `<median / P95>` |
| Starting playback | `<mean>` | `<mean>` | `<median / P95; endpoint>` |
| Steady playback | `<mean>` | `<mean>` | — |
| Track skip | `<mean>` | `<mean>` | `<median / P95; next>` |
| 30 min session | `<session mean>` | `<session mean>` | — |
| Peak observed | `<max snapshot>` | `<max 1 s window>` | — |

The memory and CPU maxima can occur at different times. State that explicitly. Include the final 30-minute checkpoint memory in the detailed report so the session mean cannot hide growth.

Under the table include:

> Memory includes YTM, mpv, and all owned helper processes. RSS sums may double-count shared pages; PSS is provided in the detailed report. Peaks are observed samples at `<interval>` cadence. CPU uses 100% per logical core. Playback latency uses `<IPC proxy or audio detector>`.

Then list Linux/kernel, Python/YTM/mpv versions, CPU/RAM, audio backend, connection type, configuration/profile, sample intervals, repetitions, date, and raw-results link. Include a component table for idle, steady playback, and peak, plus PSS if available.

Reddit template, only after replacing every placeholder with evidence:

> I measured the full YTM process tree, including mpv and temporary helpers. On `<machine>`, idle averaged `<X MB>`, steady playback averaged `<Y MB>`, and the highest observed total RSS was `<Z MB>` at `<cadence>` sampling. Playback CPU averaged `<C%>` of one core over `<duration>`. RSS can count shared pages more than once; `<PSS result/availability>`. These are `<N>` runs with `<versions/profile>`; methodology and raw results: `<link>`.

If comparing against a 100 MB player, say whether the peak and steady value each fall below that threshold under the tested conditions. Do not claim “always under 100 MB” from sampled tests or an unknown comparison method.

## 17. Implementation sequence with acceptance criteria

### Phase A — Resource collector and ownership validation

Implement manifest capture, target containment, `/proc` sampling, cgroup counters, process identity, and JSONL writing. Use synthetic child workloads before online playback.

Acceptance:

- A parent plus child that allocates and touches memory increases total RSS by the observed child allocation.
- A detached/reparented child remains accounted for.
- An unrelated mpv process is excluded.
- Short-lived CPU work appears in cgroup CPU even if PID polling misses it.
- PID reuse does not merge two identities.
- Missing PSS is not reported as zero.
- Total peak is max of simultaneous sums, not sum of lifetime peaks.
- Cleanup kills only benchmark-owned processes.

### Phase B — Minimal app events and PTY scenarios

Add opt-in events around readiness, search generation, accepted result rendering, actions, and actual mpv endpoint. Build a PTY driver using ordinary input.

Acceptance:

- Disabled event instrumentation has negligible behavior change and no open transport.
- Enabled instrumentation cannot deadlock on a slow collector.
- UI-ready requires a successful interactive frame/input check.
- Search completion belongs to the requested generation.
- Enter-after-results is classified as playback rather than instant search.
- Default idle measurement includes the session's mpv.
- Headless test measurements cannot be emitted as public PTY results.

### Phase C — mpv observation and latency

Connect a dedicated observer before the action being measured. Correlate file identity and event sequence. Implement the IPC proxy first, then optional audio monitoring.

Acceptance:

- Command acknowledgment cannot complete a playback trial.
- An old track's position changes cannot satisfy the next track's start.
- Pause, seek, failed decode, and timeout do not produce false starts.
- Natural EOF and manual skip remain separate categories.
- A local tone fixture validates event ordering and any audio detector.
- Optional resolver timing is clearly classified by method.

### Phase D — Complete scenario matrix

Implement startup, live search, results display, first playback, steady playback, queue, each switching type, longevity, and endurance.

Acceptance:

- Ten startup runs per available condition are summarized by median.
- At least 50 search trials have cache class, result status, median, and P95.
- At least 30 playback starts have endpoint type and failure rate.
- Track 1/5/10/25/50 checkpoints are from one continuous session.
- The 30-minute row requires 30 actual minutes.
- Default-profile helpers remain enabled and counted.

### Phase E — Footprint, optional network, and reports

Add installed-file inventory, dependency closure notes, cache deltas, attributed network capability, report generation, and comparisons.

Acceptance:

- Runtime package, Python dependencies, mpv/native dependencies, interpreter, optional services, and caches are separated.
- Network bytes have declared scope/layer and unclassified coverage.
- Unsupported network collection cannot produce invented zero overhead.
- Report regeneration gives identical statistics from the same artifacts.
- Generated text contains no real credentials or signed media URLs.
- Public numbers link back to exact run IDs and raw measurements.

## 18. Test plan and release validation

Use unit tests for pure accounting/statistics and integration tests for process/event behavior. Do not rely on online YouTube availability for ordinary CI correctness tests.

| Test case | Expected protection |
| --- | --- |
| Parent and child allocate at different times | Prevent sum-of-peaks bug |
| Shared mappings | Explain RSS/PSS divergence without asserting exact allocator-dependent values |
| Child lives less than sample interval | Demonstrate sampling limits and retained cgroup CPU |
| Double fork / new session | Prevent detached-helper omission |
| Process exits between discovery and read | Preserve partial status without crashing |
| Permission-denied rollup | PSS unavailable, RSS still usable if readable |
| PID identity changes | Reject reused PID sample |
| Irregular sample intervals | Correct time-weighted RAM/CPU |
| More than one CPU busy | Allow totals above 100% |
| Empty/one/many latency samples | Defined median/P95/null behavior |
| Older search completes late | Cannot count stale rows as current query |
| mpv acknowledgment without playback | Timeout/failure, not success |
| Playback-restart after seek | Cannot masquerade as new track start |
| Same media repeated in queue | Entry/generation identity disambiguates |
| Automatic EOF | Distinct transition boundary |
| Lost event or backpressure | Visible invalid trial, no UI hang |
| Abrupt terminal closure | Owned children cleaned up, partial artifacts saved |
| Another user's/session's mpv alive | Remains untouched |
| Report fixture with sentinel secrets | Sanitization and field allowlist prevent publication |

Use loose, evidence-based tolerances for memory integration tests; touching allocated pages is necessary because virtual reservation is not resident memory. Do not assert an exact RSS number across kernels/Python builds.

CI smoke profile can use short generated audio and deterministic fake API responses. Mark it `fixture`, never publish its speed as live YouTube performance. Live benchmarks should be deliberate, rate-conscious runs with actual audio and recorded failures.

Before release, compare the same machine/profile/dependency set on baseline and candidate, alternating run order where practical. A configurable relative regression threshold plus an absolute floor avoids noisy percentage alarms near zero; establish these thresholds after collecting variance. Do not adopt the 100 MB aspiration as a test that encourages hiding processes.

## 19. Common mistakes to reject during review

- Measuring only Python with `memory_profiler`, `tracemalloc`, or a single PID.
- Assuming idle mpv usage is zero because no song is playing.
- Counting mpv but missing its yt-dlp, JavaScript, radio, or mixer helpers.
- Using one recursive child list collected at startup forever.
- Losing reparented children or accidentally adopting unrelated PIDs.
- Reporting cgroup charged memory as RSS.
- Reporting `/usr/bin/time`'s maximum resident set as the simultaneous tree peak.
- Adding all process lifetime high-water marks.
- Dropping unreadable processes from a total without flagging partial coverage.
- Measuring startup until imports finish, mount fires, or a socket exists.
- Calling ordinary fresh-process startup OS-cold.
- Timing a headless UI as if it rendered terminal output.
- Timing Enter as search when the current UI uses Enter to play.
- Treating `file-loaded`, command success, or playback position alone as verified sound.
- Letting a previous track or another audio stream trigger the new-track detector.
- Reporting only successful requests without disclosing timeouts and retries.
- Timing every operation on the same cached song/query and generalizing to online use.
- Advertising accelerated skips as natural queue transitions or a 30-minute session.
- Calling host network-interface counters per-process traffic.
- Excluding large external runtime dependencies from installation claims.
- Comparing default YTM behavior with a stripped-down benchmark configuration.
- Publishing placeholder/example numbers as actual findings.

## 20. Implementation reference links

Re-check the installed versions before selecting exact APIs or kernel fields. The linked references support measurement primitives; the proposed workload, thresholds, and orchestration in this handout are project design choices.

- [Linux cgroup v2 reference](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html): containment and controller accounting interfaces.
- [mpv stable reference manual](https://mpv.io/manual/stable/): JSON IPC, events, properties, scripts, audio output behavior.
- [Textual App API](https://textual.textualize.io/api/app/): application lifecycle and rendering hooks.
- [Textual testing guide](https://textual.textualize.io/guide/testing/): deterministic UI tests and headless-mode limitations.

## 21. Handoff checklist

- [ ] Confirm current source architecture and preserve unrelated working-tree edits.
- [ ] Implement owned-scope resource collection and synthetic accounting tests.
- [ ] Add opt-in events with bounded transport and explicit loss handling.
- [ ] Drive the real console app through a PTY.
- [ ] Correlate search, selection, queue entry, and mpv load generations.
- [ ] Publish playback-ready proxy until real audio observation is validated.
- [ ] Implement all startup/search/playback/switching scenarios with deadlines.
- [ ] Complete 50-track and 30-minute profiles separately.
- [ ] Inventory package/dependency/cache footprint.
- [ ] Add network attribution or explicit unavailable status.
- [ ] Calibrate observer overhead and record effective sampling resolution.
- [ ] Run real workloads, retain failures, and validate artifact privacy.
- [ ] Generate the README table and four-number Reddit reply from evidence.
