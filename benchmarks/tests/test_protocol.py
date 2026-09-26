"""Tests for trace parsing and event protocol handling (handout section 18)."""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from driver import parse_trace_line, TraceTail  # noqa: E402


def test_request_done_duration():
    record = parse_trace_line("request search done in 1.2s")
    assert record == {"kind": "request_done", "cmd": "search", "duration_s": 1.2}


def test_request_failed_keeps_reason():
    record = parse_trace_line("request lyrics failed after 30.0s: timed out")
    assert record["kind"] == "request_failed"
    assert record["cmd"] == "lyrics"
    assert record["error"] == "timed out"


def test_request_start_args_are_preserved_for_correlation():
    record = parse_trace_line("request search {'query': 'daft punk'}")
    assert record["kind"] == "request_start"
    assert "daft punk" in record["args"]


def test_key_and_focus_records():
    assert parse_trace_line("key 'escape' focus=search-input") == {
        "kind": "key", "key": "escape", "focus": "search-input"}
    assert parse_trace_line("focus -> search-results") == {
        "kind": "focus", "widget": "search-results"}


def test_long_query_with_quotes_does_not_break_parsing():
    record = parse_trace_line("request search {'query': 'i\\'m o\\'k'}")
    assert record["kind"] == "request_start"


def test_unrecognized_line_is_kept_not_dropped():
    record = parse_trace_line("resize 120x40")
    assert record["kind"] == "other"
    assert "resize" in record["text"]


def test_trace_tail_handles_truncation(tmp_path):
    path = tmp_path / "tui.log"
    path.write_text("17:00:00 ytm 0.9.3 started, size 120x40\n"
                    "17:00:01 key 'x' focus=search-input\n"
                    "17:00:02 focus -> search-results\n")
    tail = TraceTail(path)
    assert len(tail.poll()) == 3
    # the app truncates with mode="w" at the next start
    path.write_text("17:00:00 ytm 0.9.3 started, size 120x40\n"
                    "17:00:01 focus -> search-input\n")
    second = tail.poll()
    assert [r["kind"] for r in second] == ["started", "focus"]
    assert tail.rotations == 1  # same inode, size shrank: a real truncation


def test_trace_tail_reads_appended_lines(tmp_path):
    path = tmp_path / "tui.log"
    path.write_text("17:00:00 ytm 0.9.3 started\n")
    tail = TraceTail(path)
    assert len(tail.poll()) == 1
    with open(path, "a") as handle:
        handle.write("17:00:01 focus -> search-input\n")
    assert [r["kind"] for r in tail.poll()] == ["focus"]
    assert tail.rotations == 0
