"""The timed-lyrics contract: what the provider may send versus what the
pane is allowed to index on every playback tick."""

import math

from ytm.timed_lyrics import normalize_timed_lines


def line(text="x", start=1000, end=2000, **extra):
    return {"text": text, "start_time": start, "end_time": end, **extra}


def test_valid_lines_pass_through_with_ints_and_keep_id():
    assert normalize_timed_lines([line(start=1000.0, end=2500, id=7)]) == [
        {"text": "x", "start_time": 1000, "end_time": 2500, "id": 7}
    ]


def test_missing_or_wrong_type_fields_are_dropped():
    bad = [
        {"text": "missing end", "start_time": 1000},
        {"text": "missing start", "end_time": 1000},
        {"start_time": 1, "end_time": 2},
        line(text=None),
        line(text=12),
        line(start="1000"),           # numeric strings are not numbers
        line(end=None),
        line(start=True),             # bool is not a timestamp
        line(start=math.nan),
        line(end=math.inf),
        "not a record",
        None,
    ]
    assert normalize_timed_lines(bad) == []


def test_interval_rule_is_half_open_and_strict():
    assert normalize_timed_lines([line(start=2000, end=1000)]) == []   # reversed
    assert normalize_timed_lines([line(start=1000, end=1000)]) == []   # zero-length marker
    assert normalize_timed_lines([line(start=-5, end=1000)]) == []     # before the song
    assert normalize_timed_lines([line(start=0, end=1)]) == [line(start=0, end=1)]


def test_blank_text_is_an_instrumental_gap_not_an_error():
    assert normalize_timed_lines([line(text="")]) == [line(text="")]


def test_unsorted_and_duplicate_starts_sort_stably():
    lines = [line("c", 3000, 4000), line("a1", 1000, 2000), line("a2", 1000, 1500), line("b", 2000, 3000)]
    assert [record["text"] for record in normalize_timed_lines(lines)] == ["a1", "a2", "b", "c"]


def test_mixed_input_keeps_only_the_valid_lines():
    lines = [line("ok1", 0, 500), {"text": "broken"}, line("ok2", 500, 900), line("rev", 900, 800)]
    assert [record["text"] for record in normalize_timed_lines(lines)] == ["ok1", "ok2"]


def test_non_list_input_is_nothing():
    assert normalize_timed_lines(None) == []
    assert normalize_timed_lines("plain words") == []
    assert normalize_timed_lines({"text": "x"}) == []
