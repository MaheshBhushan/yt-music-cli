# YTM benchmark harness

Measures the application users actually run: the YTM TUI process, mpv, and
every owned child (yt-dlp, JavaScript runtimes, the radio helper, mixer
utilities). The full measurement contract this harness implements is the
benchmark handout kept with the project's local design notes
(`docs/BENCHMARK_IMPLEMENTATION_HANDOUT.md`).

## Requirements

- Linux with cgroup v2 and a delegated user cgroup (`systemd --user`)
- `mpv`, the YTM console entry point, a working audio server
- Python 3.11+ (the stdlib only; the benchmark adds no runtime dependency)

## Commands

```bash
# capability + synthetic accounting check (spawns a detached 64 MiB child)
.venv/bin/python benchmarks/benchmark.py preflight

# quick end-to-end sanity run (~5 min)
.venv/bin/python benchmarks/benchmark.py run --profile smoke

# the headline profile: 10 startups, 50 searches, 30 switches
.venv/bin/python benchmarks/benchmark.py run --profile public

# 30 minutes continuous, 50 confirmed track advances, checkpoints
.venv/bin/python benchmarks/benchmark.py run --profile endurance

# everything, sequentially
.venv/bin/python benchmarks/benchmark.py run --profile full

# regenerate report.md/summary.json from raw artifacts (deterministic)
.venv/bin/python benchmarks/benchmark.py report benchmarks/results/<run-id>
```

Global options (`--output`, `--sample-ms`, `--pss-ms`, `--seed`,
`--terminal-size`, `--dwell-seconds`, `--ytm-executable`) go **before** the
subcommand; profile overrides (`--startup-runs`, `--search-runs`,
`--switches`, `--endurance-minutes`, `--longevity-tracks`) after it.

## What a run does

1. Stages your real YTM credentials into a private temporary home (never
   published; refreshed before each session because Google rotates tokens).
2. Creates a fresh cgroup v2 scope and launches the real console entry point
   inside a 120x40 PTY. The scope is the authoritative process membership:
   detached mpv, yt-dlp, node, and the radio helper are all included even
   though mpv creates its own session.
3. Samples RSS every 100 ms and PSS every 1 s from `/proc`, plus cgroup CPU
   and memory counters; writes `samples.jsonl`, `processes.jsonl`, `events.jsonl`.
4. Drives the app through the PTY: live searches, Enter-to-play, steady
   playback, next/prev/select-another-result switches, 50-track longevity,
   30-minute endurance. All trial records land in `trials.jsonl`.
5. Verifies the scope is empty after each session; removes it.
6. Generates `summary.json`, `report.md`, and `memory-by-track.csv`.

## Method notes / known deviations from the handout

- **Events.** The handout proposes an opt-in `YTM_BENCH_EVENT_FD` channel.
  This harness does not modify application code. Search boundaries use the
  app's existing TUI trace (`YTM_TUI_LOG`) durations plus the collector's
  receipt time; playback uses a second, read-only mpv JSON IPC connection
  (`file-loaded`, `playback-restart`, `playlist-pos`, `path`, `time-pos`).
  Search result counts and a true "results rendered" event are not emitted.
- **Startup endpoint** is an accepted input round-trip (escape echoed and
  focus moved), not a painted cell.
- **Playback latency** is labeled `selection-to-playback-ready (IPC proxy)`:
  new file loaded, unpaused, position clock advancing.
- **Audio.** Output goes to the real default sink; there is no isolated
  loopback detector, so "first audible sample" is not measured.
- **Network.** Byte accounting is unavailable in this implementation; no
  host-wide counters are substituted.
- **Endurance/longevity** run in one continuous session, with each advance
  confirmed before the next one. Not two separate experiments.
- CPU is cgroup CPU (retains exited helpers), normalized to 100% = one
  logical core.

## Isolation boundaries

Included in totals: the YTM Python process, owned mpv processes, and every
other process in the target cgroup.

Not included, by design: the collector and report tooling, PipeWire/PulseAudio,
the terminal emulator, the compositor, DNS, the OS, and any PO-token provider
that was already running as a shared service. If a benchmark-dedicated token
provider is ever started, measure it separately.

## Privacy

`benchmarks/results/` is git-ignored. Staged auth files live under
`results/<run-id>/private/` with mode 600; delete them when you are done with
a run. Reports carry no credentials, cookies, or signed media URLs; track
paths are reduced to video IDs.
