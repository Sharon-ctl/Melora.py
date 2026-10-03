from utils.text import MAX_MESSAGE_CHARS, clean, format_duration, format_queue, format_uptime, truncate


def test_clean_strips_controls_and_collapses_space():
    assert clean("a\n\n b\t\tc\x00d") == "a b c d"
    assert clean(None) == ""


def test_truncate_adds_dots_and_respects_limit():
    assert truncate("short", 10) == "short"
    out = truncate("x" * 100, 20)
    assert len(out) == 20 and out.endswith("...")
    assert truncate("abcdef", 3) == "abc"


def test_format_duration():
    assert format_duration(0) == "0:00"
    assert format_duration(65_000) == "1:05"
    assert format_duration(3_725_000) == "1:02:05"
    assert format_duration(-5) == "0:00"
    assert format_duration(2**62) == "live"


def test_format_uptime():
    assert format_uptime(59) == "0m 59s"
    assert format_uptime(3700) == "1h 1m 40s"
    assert format_uptime(90_000) == "1d 1h 0m"


def test_format_queue_never_exceeds_message_limit():
    rows = [(n, "T" * 5000, 10**9) for n in range(1, 11)]
    text = format_queue(rows, 1, 5, 50)
    assert len(text) <= MAX_MESSAGE_CHARS
    assert text.startswith("Up next (page 1/5, 50 tracks):")


def test_format_queue_lists_positions():
    text = format_queue([(11, "Song", 61_000)], 2, 2, 11)
    assert "11. Song [1:01]" in text
