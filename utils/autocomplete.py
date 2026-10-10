"""Autocomplete choice prefixes, data structures, and formatting helpers.

The Unicode characters U+1F50E (magnifying glass) and U+1F55B (twelve o'clock)
are allowed ONLY as display prefixes of autocomplete choice names in this module.
"""
from __future__ import annotations

from dataclasses import dataclass

from utils.text import clean

SEARCH_PREFIX = "\U0001F50E"
HISTORY_PREFIX = "\U0001F55B"


@dataclass(frozen=True, slots=True)
class UserHistoryEntry:
    title: str
    artist: str = ""
    uri: str = ""
    played_at: float = 0.0


def format_history_choice_name(title: str, artist: str = "") -> str:
    """Format history choice display name with history prefix (at most 100 chars)."""
    if artist:
        label = f"{title} - {artist}"
    else:
        label = title
    clean_label = clean(label)
    return f"{HISTORY_PREFIX} {clean_label}"[:100]


def format_history_choice_value(title: str, artist: str = "", uri: str = "") -> str:
    """Format history choice value without emojis (stored URI if <= 100 chars, else title artist)."""
    if uri and len(uri) <= 100:
        return uri
    text = f"{title} {artist}".strip() if artist else title
    return clean(text)[:100]


def format_search_choice_name(label: str) -> str:
    """Format search suggestion choice display name with search prefix (at most 100 chars)."""
    clean_label = clean(label)
    return f"{SEARCH_PREFIX} {clean_label}"[:100]
