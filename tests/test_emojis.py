"""Unit tests for data/emojis.json loader and fallback mechanisms."""
from __future__ import annotations

import json
from pathlib import Path
import discord

from core.data_loader import DEFAULT_EMOJI_LABELS, get_emoji_markup, load_emojis


def test_load_emojis_valid(tmp_path: Path):
    data = {
        "pause": 1526024157526229082,
        "resume": 1526024227881353296,
        "previous": 1525998374921175090,
        "skip": 1525998356529156137,
        "loop": 1526006850531754004,
        "stop": 1526006888498466947,
        "music": 1526006747603406869,
        "addmusic": 1526007757826691232,
    }
    p = tmp_path / "emojis.json"
    p.write_text(json.dumps(data), encoding="utf-8")

    emojis = load_emojis(p, force_reload=True)
    assert len(emojis) == 8
    for key, expected_id in data.items():
        emoji = emojis[key]
        assert isinstance(emoji, discord.PartialEmoji)
        assert emoji.name == key
        assert emoji.id == expected_id
        assert emoji.animated is False


def test_load_emojis_default_file():
    emojis = load_emojis(force_reload=True)
    assert len(emojis) == 8
    assert isinstance(emojis["pause"], discord.PartialEmoji)
    assert emojis["pause"].id == 1526024157526229082
    assert emojis["resume"].id == 1526024227881353296
    assert emojis["previous"].id == 1525998374921175090
    assert emojis["skip"].id == 1525998356529156137
    assert emojis["loop"].id == 1526006850531754004
    assert emojis["stop"].id == 1526006888498466947
    assert emojis["music"].id == 1558305498531631155
    assert emojis["addmusic"].id == 1526007757826691232


def test_load_emojis_missing_entries(tmp_path: Path):
    # Only pause and stop are present
    data = {
        "pause": 1526024157526229082,
        "stop": 1526006888498466947,
    }
    p = tmp_path / "emojis_partial.json"
    p.write_text(json.dumps(data), encoding="utf-8")

    emojis = load_emojis(p, force_reload=True)
    assert emojis["pause"] is not None
    assert emojis["stop"] is not None
    assert emojis["resume"] is None
    assert emojis["previous"] is None
    assert emojis["skip"] is None
    assert emojis["loop"] is None
    assert emojis["music"] is None
    assert emojis["addmusic"] is None

    # Fallback labels exist for missing buttons
    for key in ("resume", "previous", "skip", "loop"):
        assert DEFAULT_EMOJI_LABELS[key] in ("Resume", "Previous", "Skip", "Loop")


def test_load_emojis_invalid_entries(tmp_path: Path):
    data = {
        "pause": "not_a_number",
        "resume": -1,
        "previous": {"invalid": "dict"},
        "skip": None,
        "loop": 1526006850531754004,
        "stop": 1526006888498466947,
        "music": "abc",
        "addmusic": 1526007757826691232,
    }
    p = tmp_path / "emojis_invalid.json"
    p.write_text(json.dumps(data), encoding="utf-8")

    emojis = load_emojis(p, force_reload=True)
    assert emojis["pause"] is None
    assert emojis["resume"] is None
    assert emojis["previous"] is None
    assert emojis["skip"] is None
    assert emojis["loop"] is not None
    assert emojis["stop"] is not None
    assert emojis["music"] is None
    assert emojis["addmusic"] is not None


def test_load_emojis_missing_file(tmp_path: Path):
    p = tmp_path / "nonexistent.json"
    emojis = load_emojis(p, force_reload=True)
    for key in DEFAULT_EMOJI_LABELS:
        assert emojis[key] is None


def test_get_emoji_markup_rendering_and_fallbacks(tmp_path: Path):
    # Default file rendering
    assert get_emoji_markup("music") == "<:music:1558305498531631155>"
    assert get_emoji_markup("addmusic") == "<:addmusic:1526007757826691232>"
    assert get_emoji_markup("nonexistent") == ""

    # Custom file with missing/invalid entries
    p = tmp_path / "emojis_custom.json"
    p.write_text(
        json.dumps({
            "music": 1526006747603406869,
            "addmusic": "invalid",
        }),
        encoding="utf-8",
    )
    assert get_emoji_markup("music", path=p) == "<:music:1526006747603406869>"
    assert get_emoji_markup("addmusic", path=p) == ""
    assert get_emoji_markup("pause", path=p) == ""
