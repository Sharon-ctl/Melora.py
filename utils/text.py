"""Text formatting helpers. Pure functions, no Discord imports."""
from __future__ import annotations

import re
from collections.abc import Iterable

MAX_MESSAGE_CHARS = 1900
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_SPACE_RE = re.compile(r"\s+")
_LIVE_THRESHOLD_MS = 100 * 3600 * 1000


def clean(text: object) -> str:
    """Strip control characters and collapse whitespace."""
    value = "" if text is None else str(text)
    value = _CONTROL_RE.sub(" ", value)
    return _SPACE_RE.sub(" ", value).strip()


def truncate(text: object, limit: int = 60) -> str:
    """Clean text and cut it to at most ``limit`` characters, adding dots if cut."""
    value = clean(text)
    if limit < 4:
        return value[: max(limit, 0)]
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def format_duration(milliseconds: int) -> str:
    """Format a duration as m:ss or h:mm:ss."""
    if milliseconds < 0:
        milliseconds = 0
    if milliseconds >= _LIVE_THRESHOLD_MS:
        return "live"
    total = milliseconds // 1000
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def format_uptime(seconds: float) -> str:
    total = int(max(seconds, 0))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    return f"{minutes}m {secs}s"


def format_queue(entries: Iterable[tuple[int, str, int]], page: int, pages: int, total: int) -> str:
    """Render one queue page. Output never exceeds MAX_MESSAGE_CHARS."""
    header = f"Up next (page {page}/{pages}, {total} tracks):"
    lines = [f"{index}. {truncate(title, 60)} [{format_duration(duration)}]" for index, title, duration in entries]
    text = "\n".join([header, *lines])
    while len(text) > MAX_MESSAGE_CHARS and lines:
        lines.pop()
        text = "\n".join([header, *lines])
    return text[:MAX_MESSAGE_CHARS]


def format_history(entries: Iterable[tuple[int, str, int]], total: int) -> str:
    """Render track history. Output never exceeds MAX_MESSAGE_CHARS."""
    header = f"Playback history (last {total} tracks):"
    lines = [f"{index}. {truncate(title, 60)} [{format_duration(duration)}]" for index, title, duration in entries]
    text = "\n".join([header, *lines])
    while len(text) > MAX_MESSAGE_CHARS and lines:
        lines.pop()
        text = "\n".join([header, *lines])
    return text[:MAX_MESSAGE_CHARS]


clean_track_title = clean


def parse_time_string(val: str) -> int | None:
    """Parse a time string like '90', '1:30', or '1:02:15' into seconds, or None if invalid."""
    text = clean(val).rstrip("s").strip()
    if not text:
        return None
    parts = text.split(":")
    try:
        if len(parts) == 1:
            sec = int(parts[0])
            return sec if sec >= 0 else None
        if len(parts) == 2:
            minutes = int(parts[0])
            seconds = int(parts[1])
            if minutes < 0 or seconds < 0 or seconds >= 60:
                return None
            return minutes * 60 + seconds
        if len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = int(parts[2])
            if hours < 0 or minutes < 0 or minutes >= 60 or seconds < 0 or seconds >= 60:
                return None
            return hours * 3600 + minutes * 60 + seconds
    except (ValueError, TypeError):
        return None
    return None

