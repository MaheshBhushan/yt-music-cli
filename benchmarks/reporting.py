"""Statistics and deterministic report generation (handout sections 15-16).

Reads only the raw artifacts in a run directory. Regenerating a report for
the same artifacts produces the same numbers.
"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path

MIB = 1_048_576


def p95(values):
    """Nearest-rank P95: sorted sample at ceil(0.95*n)-1 (handout 15.3)."""
    if not values:
        return None
    ordered = sorted(values)
    index = math.ceil(0.95 * len(ordered)) - 1
    return ordered[max(0, min(index, len(ordered) - 1))]


def median(values):
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def time_weighted_mean(samples, key):
    """Mean of samples[key], each weighted by the gap to the next sample.

    The final sample closes only the last interval and carries no duration
    of its own, so it gets no weight (a single sample is its own window).
    """
    points = [(s["monotonic_end_ns"], s) for s in samples if key(s) is not None]
    if not points:
        return None
    if len(points) == 1:
        return key(points[0][1])
    total = 0.0
    weight_sum = 0.0
    for index, (stamp, sample) in enumerate(points[:-1]):
        weight = max(1, points[index + 1][0] - stamp)
        total += key(sample) * weight
        weight_sum += weight
    return total / weight_sum if weight_sum else None


def component_totals(samples, key):
    totals = {"ytm_main": [], "mpv": [], "helpers": [], "total": []}
    for sample in samples:
        values = key(sample)
        if not values:
            continue
        for name in totals:
            if values.get(name) is not None:
                totals[name].append(values[name])
    return {name: (median(vals) if vals else None) for name, vals in totals.items()}


def cpu_window(samples):
    """cgroup CPU average over a window, in percent of one logical core."""
    counters = [(s["monotonic_end_ns"], s.get("cgroup_cpu_usec"))
                for s in samples if s.get("cgroup_cpu_usec") is not None]
    if len(counters) < 2:
        return None
    (t0, c0), (t1, c1) = counters[0], counters[-1]
    if c1 < c0 or t1 <= t0:
        return None
    delta_wall_ns = t1 - t0
    return 100.0 * (c1 - c0) * 1000.0 / delta_wall_ns  # usec -> ns wall


def cpu_peak_1s(samples):
    """Max CPU over any consecutive pair <= 1.5 s apart (1 s window)."""
    points = [(s["monotonic_end_ns"], s.get("cgroup_cpu_usec")) for s in samples]
    peaks = []
    for (t0, c0), (t1, c1) in zip(points, points[1:]):
        if c0 is None or c1 is None:
            continue
        delta_t = t1 - t0
        if delta_t <= 0 or delta_t > 1_500_000_000:
            continue
        delta_c = c1 - c0
        if delta_c < 0:
            continue
        peaks.append(100.0 * delta_c * 1000.0 / delta_t)  # usec -> ns wall
    return max(peaks) if peaks else None


class Dataset:
    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.manifest = self._load("manifest.json") or {}
        self.footprint = self._load("footprint.json")
        self.samples = self._load_jsonl("samples.jsonl")
        self.events = self._load_jsonl("events.jsonl")
        self.trials = self._load_jsonl("trials.jsonl")
        self.processes = self._load_jsonl("processes.jsonl")
        self.phases = self._phase_windows()

    def _load(self, name):
        path = self.run_dir / name
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def _load_jsonl(self, name):
        path = self.run_dir / name
        records = []
        if not path.exists():
            return records
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records

    def _phase_windows(self):
        windows: dict[tuple[str, str], dict] = {}
        active: dict[tuple[str, str], int] = {}
        for event in self.events:
            if event.get("event") == "phase_begin":
                key = (event.get("session"), event.get("phase"))
                active[key] = event["monotonic_ns"]
            elif event.get("event") == "phase_end":
                key = (event.get("session"), event.get("phase"))
                if key in active:
                    windows.setdefault(key, []).append(
                        {"start_ns": active.pop(key), "end_ns": event["monotonic_ns"]})
        return windows

    def samples_for(self, session, start_ns=None, end_ns=None):
        out = []
        for sample in self.samples:
            if sample.get("session") != session:
                continue
            stamp = sample["monotonic_end_ns"]
            if start_ns is not None and stamp < start_ns:
                continue
            if end_ns is not None and stamp > end_ns:
                continue
            out.append(sample)
        return out

    def phase_samples(self, session, phase):
        out = []
        for window in self.phases.get((session, phase), []):
            out.extend(self.samples_for(session, window["start_ns"], window["end_ns"]))
        return out

    def sessions(self):
        seen = []
        for sample in self.samples:
            if sample.get("session") not in seen:
                seen.append(sample.get("session"))
        return seen

    def complete_rss_samples(self, session=None):
        out = []
        for sample in self.samples:
            if sample.get("completeness") != "complete":
                continue
            if not sample.get("rss_bytes", {}).get("total"):
                continue
            if session is not None and sample.get("session") != session:
                continue
            out.append(sample)
        return out


def window_stats(samples):
    if not samples:
        return None
    rss_totals = [s["rss_bytes"]["total"] for s in samples if s.get("rss_bytes")]
    pss_samples = [s for s in samples if s.get("pss_bytes")]
    return {
        "sample_count": len(samples),
        "elapsed_s": (samples[-1]["monotonic_end_ns"] - samples[0]["monotonic_end_ns"]) / 1e9,
        "rss_mean_bytes": time_weighted_mean(samples, lambda s: s["rss_bytes"]["total"]),
        "rss_median_bytes": median(rss_totals),
        "rss_p95_bytes": p95(rss_totals),
        "rss_max_bytes": max(rss_totals) if rss_totals else None,
        "rss_components_mean": {
            name: time_weighted_mean(samples, lambda s, n=name: s["rss_bytes"].get(n))
            for name in ("ytm_main", "mpv", "helpers")
        },
        "pss_mean_bytes": (time_weighted_mean(pss_samples, lambda s: s["pss_bytes"]["total"])
                           if pss_samples else None),
        "pss_components_mean": (
            {name: time_weighted_mean(pss_samples, lambda s, n=name: s["pss_bytes"].get(n))
             for name in ("ytm_main", "mpv", "helpers")} if pss_samples else None),
        "pss_sample_count": len(pss_samples),
        "cpu_percent_one_core": cpu_window(samples),
        "cpu_peak_1s_percent": cpu_peak_1s(samples),
    }


def _trial_stats(trials, key, label):
    values = [t[key] for t in trials if t.get(key) is not None and t.get("success", True)]
    failures = sum(1 for t in trials if not t.get("success", True))
    return {
        "label": label,
        "attempts": len(trials),
        "completed": len(values),
        "failures": failures,
        "median_ms": median(values),
        "p95_ms": p95(values),
        "min_ms": min(values) if values else None,
        "max_ms": max(values) if values else None,
    }


def build_summary(dataset: Dataset) -> dict:
    samples = dataset.samples
    summary: dict = {
        "schema_version": 1,
        "run_id": dataset.manifest.get("run_id"),
        "sessions": {},
        "windows": {},
        "trials": {},
        "peak": None,
        "cpu_peak_1s_percent": None,
    }

    for session in dataset.sessions():
        session_samples = dataset.samples_for(session)
        summary["sessions"][session] = {
            "rss_mean_bytes": time_weighted_mean(session_samples, lambda s: s["rss_bytes"]["total"]),
            "rss_max_bytes": max((s["rss_bytes"]["total"] for s in session_samples), default=None),
            "cpu_percent_one_core": cpu_window(session_samples),
            "cpu_peak_1s_percent": cpu_peak_1s(session_samples),
            "elapsed_s": ((session_samples[-1]["monotonic_end_ns"]
                           - session_samples[0]["monotonic_end_ns"]) / 1e9
                          if session_samples else None),
            "sample_count": len(session_samples),
            "peak_breakdown": _peak_breakdown(session_samples),
        }
        for (session_key, phase), _windows in dataset.phases.items():
            if session_key != session:
                continue
            stats = window_stats(dataset.phase_samples(session, phase))
            if stats:
                summary["windows"][f"{session}/{phase}"] = stats

    # Peak simultaneous total RSS and 1 s CPU peak across complete samples
    complete = dataset.complete_rss_samples()
    if complete:
        peak = max(complete, key=lambda s: s["rss_bytes"]["total"])
        summary["peak"] = {
            "monotonic_ns": peak["monotonic_end_ns"],
            "session": peak.get("session"),
            "phases": peak.get("phases"),
            "rss_bytes": peak["rss_bytes"],
            "pss_bytes": peak.get("pss_bytes"),
            "sample_id": peak.get("sample_id"),
        }
    all_peaks = [cpu_peak_1s(dataset.samples_for(session)) for session in dataset.sessions()]
    all_peaks = [p for p in all_peaks if p is not None]
    summary["cpu_peak_1s_percent"] = max(all_peaks) if all_peaks else None

    # Collector overhead
    overhead = _collector_overhead(samples)
    summary["collector"] = overhead

    # Trial summaries
    startup = [t for t in dataset.trials if t.get("kind") == "startup"]
    search = [t for t in dataset.trials if t.get("kind") == "search"]
    playback = [t for t in dataset.trials if t.get("kind") == "playback"]
    switches = [t for t in dataset.trials if t.get("kind") == "switch"]
    summary["trials"]["startup"] = _trial_stats(startup, "ready_ms", "startup to usable UI")
    summary["trials"]["search_all"] = _trial_stats(search, "elapsed_ms", "live search end-to-end")
    for cache_class in ("hit", "miss"):
        subset = [t for t in search if t.get("cache_class") == cache_class]
        summary["trials"][f"search_{cache_class}"] = _trial_stats(
            subset, "elapsed_ms", f"live search end-to-end ({cache_class})")
    summary["trials"]["search_app"] = _trial_stats(search, "app_duration_ms",
                                                  "app-measured search duration")
    summary["trials"]["playback"] = _trial_stats(playback, "elapsed_ms",
                                                 "selection to playback-ready (IPC proxy)")
    summary["trials"].setdefault("startup", _trial_stats([], "ready_ms", "startup to usable UI"))
    summary["trials"].setdefault("search_all", _trial_stats([], "elapsed_ms",
                                                            "live search end-to-end"))
    summary["trials"].setdefault("playback", _trial_stats([], "elapsed_ms",
                                                          "selection to playback-ready (IPC proxy)"))
    for direction in ("next", "prev", "select-row"):
        subset = [t for t in switches if (t.get("direction") == direction
                                          or (direction == "select-row" and t.get("selected_row") is not None))]
        if subset:
            summary["trials"][f"switch_{direction}"] = _trial_stats(
                subset, "elapsed_ms", f"switch: {direction}")

    # Startup windows: RSS/CPU around each ready moment
    startup_windows = []
    for trial in startup:
        session = trial.get("session")
        start = trial.get("ready_ns")
        if start is None:
            continue
        window = dataset.samples_for(session, start, start + 5_000_000_000)
        if window:
            startup_windows.append(window_stats(window))
    summary["windows"]["startup_aggregate"] = _aggregate_windows(startup_windows)

    # Playback-start windows: RSS/CPU around each playback trial
    playback_windows = []
    for trial in playback:
        start = trial.get("start_ns")
        ready = trial.get("ready_ns") or trial.get("advancing_ns")
        if start is None or ready is None:
            continue
        window = dataset.samples_for(trial.get("session"), start, ready + 5_000_000_000)
        if window:
            playback_windows.append(window_stats(window))
    summary["windows"]["playback_start_aggregate"] = _aggregate_windows(playback_windows)

    # Longevity checkpoints
    checkpoints = {}
    for key, stats in summary["windows"].items():
        if "/checkpoint-" in key:
            checkpoints[key.split("/", 1)[1]] = stats
    summary["checkpoints"] = checkpoints
    return summary


def _peak_breakdown(samples):
    if not samples:
        return None
    peak = max(samples, key=lambda s: s["rss_bytes"]["total"])
    return {"monotonic_ns": peak["monotonic_end_ns"], "phases": peak.get("phases"),
            "rss_bytes": peak["rss_bytes"], "pss_bytes": peak.get("pss_bytes")}


def _aggregate_windows(windows):
    windows = [w for w in windows if w]
    if not windows:
        return None
    def agg(key):
        values = [w[key] for w in windows if w.get(key) is not None]
        return (sum(values) / len(values)) if values else None
    return {
        "windows": len(windows),
        "rss_mean_bytes": agg("rss_mean_bytes"),
        "rss_median_bytes": agg("rss_median_bytes"),
        "rss_p95_bytes": agg("rss_p95_bytes"),
        "rss_max_bytes": max((w["rss_max_bytes"] for w in windows
                              if w.get("rss_max_bytes")), default=None),
        "pss_mean_bytes": agg("pss_mean_bytes"),
        "cpu_percent_one_core": agg("cpu_percent_one_core"),
        "cpu_peak_1s_percent": max((w["cpu_peak_1s_percent"] for w in windows
                                    if w.get("cpu_peak_1s_percent") is not None), default=None),
    }


def _collector_overhead(samples):
    intervals = []
    rss_values = []
    for previous, current in zip(samples, samples[1:]):
        if previous.get("collector", {}).get("cpu_seconds") is None:
            continue
        if current.get("collector", {}).get("cpu_seconds") is None:
            continue
        delta_t = current["monotonic_end_ns"] - previous["monotonic_end_ns"]
        delta_c = (current["collector"]["cpu_seconds"]
                   - previous["collector"]["cpu_seconds"])
        if delta_t > 0 and delta_c >= 0:
            intervals.append(100.0 * delta_c / (delta_t / 1e9))
    for sample in samples:
        value = sample.get("collector", {}).get("rss_bytes")
        if value:
            rss_values.append(value)
    read_durations = [s.get("read_duration_ns", 0) for s in samples]
    return {
        "cpu_percent_one_core_mean": (sum(intervals) / len(intervals)) if intervals else None,
        "cpu_percent_one_core_max": max(intervals) if intervals else None,
        "rss_mean_bytes": (sum(rss_values) / len(rss_values)) if rss_values else None,
        "sample_read_duration_ms_mean": (sum(read_durations) / len(read_durations) / 1e6
                                         if read_durations else None),
        "sample_read_duration_ms_max": (max(read_durations) / 1e6 if read_durations else None),
        "sample_count": len(samples),
    }


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def mib(value):
    return None if value is None else value / MIB


def fmt_mib(value, digits=1):
    return "unavailable" if value is None else f"{value / MIB:.{digits}f}"


def fmt_ms(value):
    return "unavailable" if value is None else f"{value:.0f}"


def fmt_pct(value):
    return "unavailable" if value is None else f"{value:.1f}"


def render_report(dataset: Dataset, summary: dict) -> str:
    manifest = dataset.manifest
    machine = manifest.get("machine", {})
    runtime = manifest.get("runtime", {})
    options = manifest.get("options", {})
    lines: list[str] = []
    add = lines.append

    def window(key):
        return summary["windows"].get(key)

    idle = window("interactive/idle")
    steady = window("interactive/steady")
    endurance = summary["sessions"].get("endurance", {})
    peak = summary["peak"]
    startup = summary["windows"].get("startup_aggregate")
    playback = summary["windows"].get("playback_start_aggregate")
    startup_trials = summary["trials"]["startup"]

    add("# YTM full-process benchmark report")
    add("")
    source_version = runtime.get("ytm_repo_version") or runtime.get("ytm_version")
    add(f"Run ID: `{manifest.get('run_id')}`  ")
    add(f"Created (UTC): {manifest.get('created_utc')}  ")
    add(f"Source: ytm {source_version} at commit `{manifest.get('source_revision', {}).get('commit')}` "
        f"({manifest.get('source_revision', {}).get('dirty_files')} dirty file(s), "
        f"install: {runtime.get('install_mode')})  ")
    add(f"Host: {machine.get('hostname')} — {machine.get('os')}, kernel {machine.get('kernel')}, "
        f"{machine.get('cpu_model')}, {machine.get('logical_cpus')} logical CPUs  ")
    add(f"Audio: {manifest.get('audio', {}).get('Server Name')} / {manifest.get('audio', {}).get('Default Sink')}  ")
    add(f"Terminal in PTY: {options.get('terminal', '120x40')}, TERM={options.get('term', 'xterm-256color')}  ")
    add(f"Samples: {summary['collector']['sample_count']} at {options.get('sample_ms')} ms RSS / "
        f"{options.get('pss_ms')} ms PSS cadence")
    add("")
    add("## Public table")
    add("")
    add("| Scenario | Total RSS (MiB) | CPU (% of one core) | Latency (ms) |")
    add("| --- | ---: | ---: | ---: |")
    add(f"| Startup | {fmt_mib(startup['rss_mean_bytes'] if startup else None)} | "
        f"{fmt_pct(startup['cpu_percent_one_core'] if startup else None)} | "
        f"{fmt_ms(startup_trials['median_ms'])}; fresh app state |")
    add(f"| Idle | {fmt_mib(idle['rss_mean_bytes'] if idle else None)} | "
        f"{fmt_pct(idle['cpu_percent_one_core'] if idle else None)} | — |")
    search_all = summary["trials"]["search_all"]
    search_window = window("interactive/search")
    add(f"| Search | {fmt_mib(search_window['rss_mean_bytes'] if search_window else None)} | "
        f"{fmt_pct(search_window['cpu_percent_one_core'] if search_window else None)} | "
        f"{fmt_ms(search_all['median_ms'])} / {fmt_ms(search_all['p95_ms'])} |")
    add(f"| Starting playback | {fmt_mib(playback['rss_mean_bytes'] if playback else None)} | "
        f"{fmt_pct(playback['cpu_percent_one_core'] if playback else None)} | "
        f"{fmt_ms(summary['trials']['playback']['median_ms'])} / "
        f"{fmt_ms(summary['trials']['playback']['p95_ms'])}; IPC proxy |")
    add(f"| Steady playback | {fmt_mib(steady['rss_mean_bytes'] if steady else None)} | "
        f"{fmt_pct(steady['cpu_percent_one_core'] if steady else None)} | — |")
    skip = summary["trials"].get("switch_next")
    skip_window = window("interactive/switching") or window("endurance/longevity")
    add(f"| Track skip | {fmt_mib(skip_window['rss_mean_bytes'] if skip_window else None)} | "
        f"{fmt_pct(skip_window['cpu_percent_one_core'] if skip_window else None)} | "
        f"{fmt_ms(skip['median_ms'] if skip else None)} / {fmt_ms(skip['p95_ms'] if skip else None)}; next |")
    add(f"| {'30 min session' if options.get('endurance_minutes') else 'Long session'} | "
        f"{fmt_mib(endurance.get('rss_mean_bytes'))} | {fmt_pct(endurance.get('cpu_percent_one_core'))} | — |")
    peak_trials = summary["trials"]["playback"] if playback else None
    add(f"| Peak observed | {fmt_mib(peak['rss_bytes']['total'] if peak else None)} | "
        f"{fmt_pct(summary.get('cpu_peak_1s_percent'))} | — |")
    add("")
    add("Memory and CPU maxima can occur at different times. RSS sums may count shared pages more than "
        "once; PSS is reported below. Peaks are observed samples at the recorded cadence. CPU uses "
        "100% per logical core. Playback latency is the IPC proxy: a new file is loaded, playback is "
        "unpaused, and the position clock is advancing.")
    add("")
    add("> Memory benchmarks include the YTM CLI process, mpv, and child processes, including temporary "
        "stream-resolution and radio helpers.")
    add("")
    method_shutdown = all(t.get("success") for t in dataset.trials)
    add(f"All artifact process membership is from a dedicated cgroup v2 scope, so detached children "
        f"(mpv, yt-dlp, JavaScript runtimes, the radio helper, mixer utilities) are included even when "
        f"they leave the process group. Trial failures are reported where they occurred; "
        f"run result: {'all trials completed' if method_shutdown else 'some trials failed, see trials.jsonl'}.")
    add("")
    add("## Component breakdown")
    add("")
    add("| Window | ytm_main (MiB) | mpv (MiB) | helpers (MiB) | total (MiB) | PSS total (MiB) | CPU % |")
    add("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for key, label in (("interactive/idle", "Idle"), ("interactive/steady", "Steady playback"),
                       ("interactive/playback_start_aggregate", "Starting playback"),
                       ("interactive/switching", "Track switching"),
                       ("endurance/longevity", "Longevity")):
        stats = summary["windows"].get(key)
        if not stats:
            continue
        comp = stats.get("rss_components_mean") or {}
        add(f"| {label} | {fmt_mib(comp.get('ytm_main'))} | {fmt_mib(comp.get('mpv'))} | "
            f"{fmt_mib(comp.get('helpers'))} | {fmt_mib(stats['rss_mean_bytes'])} | "
            f"{fmt_mib(stats.get('pss_mean_bytes'))} | {fmt_pct(stats.get('cpu_percent_one_core'))} |")
    if peak:
        comp = peak["rss_bytes"]
        add(f"| Peak sample | {fmt_mib(comp.get('ytm_main'))} | {fmt_mib(comp.get('mpv'))} | "
            f"{fmt_mib(comp.get('helpers'))} | {fmt_mib(comp.get('total'))} | "
            f"{fmt_mib(peak.get('pss_bytes', {}).get('total') if peak.get('pss_bytes') else None)} | "
            f"{fmt_pct(summary.get('cpu_peak_1s_percent'))} |")
    add("")
    if peak:
        add(f"Peak sample: session `{peak['session']}`, phases {peak['phases']}, sample "
            f"#{peak['sample_id']} at {peak['monotonic_ns']} (monotonic ns).")
        add("")

    add("## Latency and success detail")
    add("")
    for name in ("startup", "search_all", "search_hit", "search_miss", "search_app", "playback",
                 "switch_next", "switch_prev", "switch_select-row"):
        stats = summary["trials"].get(name)
        if not stats:
            continue
        add(f"- **{stats['label']}** ({name}): attempts {stats['attempts']}, completed "
            f"{stats['completed']}, failures {stats['failures']}, median "
            f"{fmt_ms(stats['median_ms'])} ms, P95 {fmt_ms(stats['p95_ms'])} ms, "
            f"min {fmt_ms(stats['min_ms'])}, max {fmt_ms(stats['max_ms'])}.")
    add("")
    if summary["trials"]["search_all"]["attempts"] < 30:
        add("Search sample count is below 30, so P95 is a weak tail estimate.")
        add("")
    if summary["trials"]["playback"]["attempts"] < 30:
        add("Playback-start sample count is below 30; treat P95 as weak.")
        add("")

    if summary.get("checkpoints"):
        add("## Longevity checkpoints (same session)")
        add("")
        add("| Checkpoint | total RSS mean (MiB) | PSS mean (MiB) | mpv (MiB) | helpers (MiB) | CPU % |")
        add("| --- | ---: | ---: | ---: | ---: | ---: |")
        for name, stats in summary["checkpoints"].items():
            comp = stats.get("rss_components_mean") or {}
            add(f"| {name} | {fmt_mib(stats['rss_mean_bytes'])} | {fmt_mib(stats.get('pss_mean_bytes'))} | "
                f"{fmt_mib(comp.get('mpv'))} | {fmt_mib(comp.get('helpers'))} | "
                f"{fmt_pct(stats.get('cpu_percent_one_core'))} |")
        add("")

    add("## Footprint")
    add("")
    footprint = dataset.footprint
    if footprint:
        install_mode = (dataset.manifest.get("runtime") or {}).get("install_mode")
        ytm = footprint.get("ytm_distribution", {})
        if install_mode == "editable":
            add("- Note: ytm is installed editable in this checkout, so distribution "
                "metadata covers only the console script and link files; the source "
                "tree is the real installation. A clean non-editable install is "
                "required for a distribution-size claim (handout section 14).")
        add(f"- ytm distribution: {ytm.get('files', '?')} files, "
            f"{fmt_mib(ytm.get('apparent_bytes'))} MiB apparent, "
            f"{fmt_mib(ytm.get('allocated_bytes'))} MiB allocated")
        venv = footprint.get("venv", {})
        add(f"- Virtual environment (interpreter + dependencies): "
            f"{fmt_mib(venv.get('apparent_bytes'))} MiB apparent, "
            f"{fmt_mib(venv.get('allocated_bytes'))} MiB allocated")
        mpv = footprint.get("mpv", {})
        if mpv:
            add(f"- mpv package: {fmt_mib(mpv.get('installed_bytes'))} MiB installed (package manager), "
                f"binary {fmt_mib(mpv.get('binary_bytes'))} MiB")
        caches = footprint.get("cache", {})
        before = (caches.get("before") or {}).get("apparent_bytes")
        after = (caches.get("after") or {}).get("apparent_bytes")
        add(f"- App cache in run profile: {fmt_mib(after)} MiB apparent after playback "
            f"({fmt_mib(before)} MiB before)")
    else:
        add("Footprint was not collected for this run.")
    add("")

    add("## Method notes and deviations")
    add("")
    add("- Process membership and CPU come from a dedicated cgroup v2 scope; per-process RSS/PSS/VmHWM "
        "come from `/proc` at the recorded cadence.")
    add("- Search boundaries: the app's own trace records the request duration; end-to-end adds the "
        "collector's receipt of the trace line, so it includes the measured debounce and has a small "
        "polling uncertainty. Cache class is classified from the app-measured duration "
        "(< 50 ms = backend cache hit).")
    add("- The handout proposes an opt-in `YTM_BENCH_EVENT_FD` event channel. This run used the existing "
        "TUI trace file plus a read-only second mpv IPC connection instead, so no application code was "
        "modified. Consequence: `results_rendered` is not emitted; accepted-generation correlation is "
        "via the single-query scripted flow, and result counts are not measured.")
    add("- Startup latency ends at an accepted input round-trip (escape received and focus moved), "
        "not at the first drawn cell.")
    add("- Endurance and longevity were measured in one continuous session; each advance was confirmed "
        "by the IPC proxy before the next one. This differs from the handout's separation of the "
        "50-track and 30-minute experiments.")
    add("- Network byte accounting and real audio-output detection were not collected; both are reported "
        "as unavailable rather than estimated.")
    add("")
    add("## Raw artifacts")
    add("")
    for name in ("manifest.json", "events.jsonl", "samples.jsonl", "processes.jsonl",
                 "trials.jsonl", "summary.json", "memory-by-track.csv", "report.md"):
        add(f"- `{name}`")
    (dataset.run_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return "\n".join(lines)


def write_memory_csv(dataset: Dataset, summary: dict) -> None:
    path = dataset.run_dir / "memory-by-track.csv"
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["checkpoint", "elapsed_s", "sample_count", "rss_mean_bytes",
                         "rss_max_bytes", "pss_mean_bytes", "cpu_percent"])
        for name, stats in summary.get("checkpoints", {}).items():
            writer.writerow([name, f"{stats['elapsed_s']:.3f}", stats["sample_count"],
                             stats["rss_mean_bytes"], stats["rss_max_bytes"],
                             stats["pss_mean_bytes"], stats["cpu_percent_one_core"]])


def generate(run_dir: Path) -> dict:
    dataset = Dataset(run_dir)
    summary = build_summary(dataset)
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")
    render_report(dataset, summary)
    write_memory_csv(dataset, summary)
    return summary
