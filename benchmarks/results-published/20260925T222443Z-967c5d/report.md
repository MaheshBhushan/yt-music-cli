# YTM full-process benchmark report

Run ID: `20260925T222443Z-967c5d`  
Created (UTC): 2026-09-25T22:24:43Z  
Source: ytm 0.9.3 at commit `742f32f93eea45155337807de4995374eb506eaf` (24 dirty file(s), install: editable)  
Host: NANI — CachyOS, kernel 7.1.1-2-cachyos, 11th Gen Intel(R) Core(TM) i5-1135G7 @ 2.40GHz, 8 logical CPUs  
Audio: PulseAudio (on PipeWire 1.6.7) / alsa_output.pci-0000_00_1f.3-platform-skl_hda_dsp_generic.HiFi__Headphones__sink  
Terminal in PTY: 40x120, TERM=xterm-256color  
Samples: 19530 at 100 ms RSS / 1000 ms PSS cadence

## Public table

| Scenario | Total RSS (MiB) | CPU (% of one core) | Latency (ms) |
| --- | ---: | ---: | ---: |
| Startup | 128.3 | 10.5 | 1246; fresh app state |
| Idle | 138.8 | 2.7 | — |
| Search | 145.2 | 6.4 | 470 / 916 |
| Starting playback | 198.1 | 14.7 | 1911 / 3450; IPC proxy |
| Steady playback | 187.5 | 5.5 | — |
| Track skip | 242.6 | 32.5 | 1739 / 3934; next |
| 30 min session | 183.7 | 7.0 | — |
| Peak observed | 292.7 | 248.8 | — |

Memory and CPU maxima can occur at different times. RSS sums may count shared pages more than once; PSS is reported below. Peaks are observed samples at the recorded cadence. CPU uses 100% per logical core. Playback latency is the IPC proxy: a new file is loaded, playback is unpaused, and the position clock is advancing.

> Memory benchmarks include the YTM CLI process, mpv, and child processes, including temporary stream-resolution and radio helpers.

All artifact process membership is from a dedicated cgroup v2 scope, so detached children (mpv, yt-dlp, JavaScript runtimes, the radio helper, mixer utilities) are included even when they leave the process group. Trial failures are reported where they occurred; run result: some trials failed, see trials.jsonl.

## Component breakdown

| Window | ytm_main (MiB) | mpv (MiB) | helpers (MiB) | total (MiB) | PSS total (MiB) | CPU % |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Idle | 67.8 | 63.6 | 7.4 | 138.8 | 103.2 | 2.7 |
| Steady playback | 81.0 | 99.1 | 7.4 | 187.5 | 149.6 | 5.5 |
| Track switching | 89.5 | 104.1 | 49.0 | 242.6 | 193.8 | 32.5 |
| Longevity | 80.5 | 90.5 | 13.3 | 184.2 | 144.8 | 7.0 |
| Peak sample | 82.6 | 108.3 | 101.9 | 292.7 | unavailable | 248.8 |

Peak sample: session `endurance`, phases ['longevity'], sample #4172 at 1069017044845658 (monotonic ns).

## Latency and success detail

- **startup to usable UI** (startup): attempts 12, completed 12, failures 0, median 1246 ms, P95 7239 ms, min 1121, max 7239.
- **live search end-to-end** (search_all): attempts 67, completed 67, failures 0, median 470 ms, P95 916 ms, min 358, max 1440.
- **live search end-to-end (hit)** (search_hit): attempts 29, completed 29, failures 0, median 370 ms, P95 460 ms, min 358, max 461.
- **live search end-to-end (miss)** (search_miss): attempts 38, completed 38, failures 0, median 747 ms, P95 949 ms, min 435, max 1440.
- **app-measured search duration** (search_app): attempts 67, completed 67, failures 0, median 100 ms, P95 600 ms, min 0, max 1100.
- **selection to playback-ready (IPC proxy)** (playback): attempts 3, completed 3, failures 0, median 1911 ms, P95 3450 ms, min 1847, max 3450.
- **switch: next** (switch_next): attempts 63, completed 62, failures 1, median 1739 ms, P95 3934 ms, min 1416, max 22829.
- **switch: prev** (switch_prev): attempts 12, completed 12, failures 0, median 1574 ms, P95 1930 ms, min 1366, max 1930.
- **switch: select-row** (switch_select-row): attempts 5, completed 5, failures 0, median 1581 ms, P95 1738 ms, min 1446, max 1738.

Playback-start sample count is below 30; treat P95 as weak.

## Longevity checkpoints (same session)

| Checkpoint | total RSS mean (MiB) | PSS mean (MiB) | mpv (MiB) | helpers (MiB) | CPU % |
| --- | ---: | ---: | ---: | ---: | ---: |
| checkpoint-1 | 191.1 | 149.7 | 102.9 | 10.8 | 7.6 |
| checkpoint-5 | 197.5 | 159.5 | 107.5 | 7.4 | 5.2 |
| checkpoint-10 | 201.6 | 163.6 | 110.3 | 7.4 | 5.2 |
| checkpoint-25 | 178.5 | 140.9 | 93.2 | 6.2 | 6.0 |
| checkpoint-50 | 197.7 | 158.4 | 108.6 | 6.2 | 5.7 |

## Footprint

- Note: ytm is installed editable in this checkout, so distribution metadata covers only the console script and link files; the source tree is the real installation. A clean non-editable install is required for a distribution-size claim (handout section 14).
- ytm distribution: 13 files, 0.0 MiB apparent, 0.0 MiB allocated
- Virtual environment (interpreter + dependencies): 114.6 MiB apparent, 128.4 MiB allocated
- mpv package: 7.4 MiB installed (package manager), binary 3.4 MiB
- App cache in run profile: 0.0 MiB apparent after playback (0.0 MiB before)

## Method notes and deviations

- Process membership and CPU come from a dedicated cgroup v2 scope; per-process RSS/PSS/VmHWM come from `/proc` at the recorded cadence.
- Search boundaries: the app's own trace records the request duration; end-to-end adds the collector's receipt of the trace line, so it includes the measured debounce and has a small polling uncertainty. Cache class is classified from the app-measured duration (< 50 ms = backend cache hit).
- The handout proposes an opt-in `YTM_BENCH_EVENT_FD` event channel. This run used the existing TUI trace file plus a read-only second mpv IPC connection instead, so no application code was modified. Consequence: `results_rendered` is not emitted; accepted-generation correlation is via the single-query scripted flow, and result counts are not measured.
- Startup latency ends at an accepted input round-trip (escape received and focus moved), not at the first drawn cell.
- Endurance and longevity were measured in one continuous session; each advance was confirmed by the IPC proxy before the next one. This differs from the handout's separation of the 50-track and 30-minute experiments.
- Network byte accounting and real audio-output detection were not collected; both are reported as unavailable rather than estimated.

## Raw artifacts

- `manifest.json`
- `events.jsonl`
- `samples.jsonl`
- `processes.jsonl`
- `trials.jsonl`
- `summary.json`
- `memory-by-track.csv`
- `report.md`
