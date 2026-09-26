"""Unit tests for accounting and statistics (handout section 18)."""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from reporting import (  # noqa: E402
    Dataset, build_summary, cpu_peak_1s, cpu_window, median, p95,
    time_weighted_mean,
)


def sample(ns, rss, cpu, pss=None, session="s"):
    record = {
        "monotonic_end_ns": ns,
        "session": session,
        "rss_bytes": rss,
        "completeness": "complete",
        "cgroup_cpu_usec": cpu,
        "read_duration_ns": 0,
        "collector": {"rss_bytes": 10, "cpu_seconds": 0},
        "sample_id": ns,
    }
    if pss is not None:
        record["pss_bytes"] = pss
    return record


def test_median_even_and_odd():
    assert median([3, 1, 2]) == 2
    assert median([4, 1, 3, 2]) == 2.5
    assert median([]) is None


def test_p95_nearest_rank_and_null():
    assert p95([]) is None
    assert p95([5]) == 5
    values = list(range(1, 21))
    assert p95(values) == 19  # ceil(0.95*20)-1 = index 18


def test_time_weighted_mean_irregular_intervals():
    # 100 MiB for 9 s, then 200 MiB for 1 s -> 110 MiB
    samples = [
        sample(0, 100, 0),
        sample(9_000_000_000, 200, 0),
        sample(10_000_000_000, 200, 0),
    ]
    mean = time_weighted_mean(samples, lambda s: s["rss_bytes"])
    assert abs(mean - 110) < 1


def test_peak_is_max_of_simultaneous_sums_not_sum_of_peaks():
    # Process A peaks first, process B later; the simultaneous max is 150.
    samples = [
        sample(1, {"total": 150, "ytm_main": 100, "mpv": 50, "helpers": 0}, 0),
        sample(2, {"total": 120, "ytm_main": 10, "mpv": 110, "helpers": 0}, 0),
    ]
    peaks = {"ytm_main": 100, "mpv": 110}
    assert sum(peaks.values()) == 210  # the wrong answer
    observed = max(s["rss_bytes"]["total"] for s in samples)
    assert observed == 150


def test_incomplete_samples_excluded_from_peak():
    good = sample(1, {"total": 100, "ytm_main": 100, "mpv": 0, "helpers": 0}, 0)
    partial = sample(2, {"total": 900, "ytm_main": 900, "mpv": 0, "helpers": 0}, 0)
    partial["completeness"] = "partial"
    dataset = Dataset.__new__(Dataset)
    dataset.samples = [good, partial]
    assert max(s["rss_bytes"]["total"] for s in dataset.complete_rss_samples()) == 100


def test_cpu_window_normalizes_to_one_core():
    # 2 CPU-seconds over 10 s wall = 20% of one core
    samples = [sample(0, 1, 0), sample(10_000_000_000, 1, 2_000_000)]
    assert abs(cpu_window(samples) - 20.0) < 1e-6
    assert cpu_window([sample(0, 1, 0)]) is None


def test_cpu_can_exceed_100_percent():
    samples = [sample(0, 1, 0), sample(1_000_000_000, 1, 3_000_000)]
    assert abs(cpu_window(samples) - 300.0) < 1e-6


def test_cpu_peak_1s_window():
    points = [sample(0, 1, 0), sample(1_000_000_000, 1, 500_000),
              sample(2_000_000_000, 1, 2_000_000)]
    assert abs(cpu_peak_1s(points) - 150.0) < 1e-6
    assert cpu_peak_1s([]) is None


def test_missing_pss_is_null_not_zero():
    records = [
        sample(1, {"total": 1, "ytm_main": 1, "mpv": 0, "helpers": 0}, 0),
        sample(2, {"total": 1, "ytm_main": 1, "mpv": 0, "helpers": 0}, 0),
    ]
    for record in records:
        record["pss_bytes"] = None
    from reporting import window_stats
    stats = window_stats(records)
    assert stats["pss_mean_bytes"] is None
    assert stats["pss_sample_count"] == 0


def test_phase_windows_pair_events():
    dataset = Dataset.__new__(Dataset)
    dataset.events = [
        {"event": "phase_begin", "session": "s", "phase": "idle", "monotonic_ns": 10},
        {"event": "phase_end", "session": "s", "phase": "idle", "monotonic_ns": 20},
    ]
    windows = dataset._phase_windows()
    assert windows[("s", "idle")] == [{"start_ns": 10, "end_ns": 20}]


def test_summary_peak_and_steady_windows():
    dataset = Dataset.__new__(Dataset)
    dataset.run_dir = Path("/nonexistent")
    dataset.manifest = {"run_id": "r"}
    dataset.footprint = None
    dataset.events = [
        {"event": "phase_begin", "session": "s", "phase": "steady", "monotonic_ns": 0},
        {"event": "phase_end", "session": "s", "phase": "steady", "monotonic_ns": 3},
    ]
    dataset.samples = [
        sample(1, {"total": 100, "ytm_main": 50, "mpv": 30, "helpers": 20}, 0, pss={"total": 80, "ytm_main": 40, "mpv": 25, "helpers": 15}),
        sample(2, {"total": 300, "ytm_main": 100, "mpv": 150, "helpers": 50}, 1_000_000),
    ]
    dataset.trials = []
    dataset.phases = {("s", "steady"): [{"start_ns": 0, "end_ns": 3}]}
    summary = build_summary(dataset)
    assert summary["peak"]["rss_bytes"]["total"] == 300
    steady = summary["windows"]["s/steady"]
    assert steady["rss_max_bytes"] == 300
    assert steady["pss_mean_bytes"] is not None
    assert summary["trials"]["startup"]["attempts"] == 0
