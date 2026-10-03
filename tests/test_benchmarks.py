"""Micro-benchmarks for critical hot paths.

Tests throughput and latency of on_message early-exit, candidate scoring,
TTLCache operations, and precomputed help page lookups.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cogs.admin import precompute_help_pages
from config import load_config
from core.alerts import Alerter
from core.matching import CandidateScorer, build_search_queries, clean_spotify_title
from main import MusicBot
from utils.cache import TTLCache


def _make_bot() -> MusicBot:
    cfg = load_config({
        "DISCORD_TOKEN": "A" * 35,
        "OWNER_ID": "123456789",
        "LAVALINK_HOST": "127.0.0.1",
        "LAVALINK_PORT": "2333",
        "LAVALINK_PASSWORD": "secret_password",
    })
    bot = MusicBot(cfg, Alerter(cfg))
    bot_user = SimpleNamespace(id=999999, bot=True)
    bot._connection.user = bot_user
    bot._bot_user_id = bot_user.id
    return bot


def _make_fake_message(bot_uid: int, *, is_bot: bool = False, mentions_bot: bool = False) -> Any:
    guild = SimpleNamespace(id=777777)
    author = SimpleNamespace(id=111111, bot=is_bot)
    raw_mentions = [bot_uid] if mentions_bot else [12345, 67890]
    return SimpleNamespace(
        id=555555,
        author=author,
        guild=guild,
        raw_mentions=raw_mentions,
        raw_role_mentions=[],
        reference=None,
        reply=AsyncMock(),
    )


@pytest.mark.anyio
async def test_benchmark_on_message_non_mention_throughput():
    bot = _make_bot()
    bot_uid = bot._bot_user_id
    assert bot_uid is not None

    # Pre-generate 5,000 non-mention messages
    msgs = [_make_fake_message(bot_uid, mentions_bot=False) for _ in range(5000)]

    start = time.perf_counter()
    for msg in msgs:
        await bot.on_message(msg)
    elapsed = time.perf_counter() - start

    # 5,000 messages should finish well under 0.25 seconds (< 50 us per message)
    avg_us = (elapsed / len(msgs)) * 1_000_000
    assert elapsed < 0.25, f"on_message too slow: {elapsed:.3f}s for {len(msgs)} messages ({avg_us:.1f}us/msg)"


@pytest.mark.anyio
async def test_benchmark_on_message_bot_author_rejection():
    bot = _make_bot()
    bot_uid = bot._bot_user_id
    assert bot_uid is not None

    msgs = [_make_fake_message(bot_uid, is_bot=True) for _ in range(5000)]

    start = time.perf_counter()
    for msg in msgs:
        await bot.on_message(msg)
    elapsed = time.perf_counter() - start

    assert elapsed < 0.15, f"bot author rejection too slow: {elapsed:.3f}s for {len(msgs)} messages"


def test_benchmark_spotify_title_clean_and_queries():
    scorer = CandidateScorer()
    raw_titles = [
        "Track Name (feat. Famous Artist)",
        "Classic Hit - 2011 Remaster",
        "Rock Anthem [ft. Guest]",
        "Album Cut - Deluxe Edition",
        "Standard Song Title",
    ]
    artists = ["Lead Artist", "Featured Artist"]

    start = time.perf_counter()
    for i in range(5000):
        t = raw_titles[i % len(raw_titles)]
        cleaned = clean_spotify_title(t, scorer.strip_patterns)
        queries = build_search_queries(t, artists, scorer.strip_patterns)
        assert cleaned is not None
        assert len(queries) > 0
    elapsed = time.perf_counter() - start

    assert elapsed < 0.2, f"title clean and queries too slow: {elapsed:.3f}s for 5000 iterations"


def test_benchmark_candidate_scoring():
    scorer = CandidateScorer()
    candidates = [
        SimpleNamespace(title="Track Name - Lead Artist", author="Lead Artist - Topic", duration=182000, uri="https://youtube.com/watch?v=1"),
        SimpleNamespace(title="Track Name Official Video", author="Lead Artist", duration=210000, uri="https://youtube.com/watch?v=2"),
        SimpleNamespace(title="Unrelated Track", author="Other Artist", duration=180000, uri="https://youtube.com/watch?v=3"),
    ]

    start = time.perf_counter()
    for i in range(5000):
        cand = candidates[i % len(candidates)]
        score = scorer.score_candidate("Track Name", ["Lead Artist"], 180000, cand)
        assert score is not None
    elapsed = time.perf_counter() - start

    assert elapsed < 0.2, f"candidate scoring too slow: {elapsed:.3f}s for 5000 iterations"


def test_benchmark_ttl_cache():
    cache: TTLCache[int, str] = TTLCache(max_size=200, ttl=60.0)

    start = time.perf_counter()
    for i in range(10000):
        cache[i % 300] = f"value_{i}"
        _ = cache.get((i - 1) % 300)
        _ = (i % 300) in cache
    elapsed = time.perf_counter() - start

    assert elapsed < 0.15, f"TTLCache too slow: {elapsed:.3f}s for 10000 operations"


def test_benchmark_help_pages_precomputation_and_retrieval():
    categories = ["Overview", "Playback", "Queue", "Library", "Filters", "Settings", "Info", "Owner"]
    descriptions = {c: f"Category {c}" for c in categories}
    commands_by_cat = {c: [f"/{c.lower()}_{i} - description for command {i}" for i in range(25)] for c in categories}

    pages = precompute_help_pages(categories, descriptions, commands_by_cat)
    assert len(pages) == len(categories)

    start = time.perf_counter()
    for i in range(5000):
        cat = categories[i % len(categories)]
        cat_pages = pages[cat]
        page_idx = i % len(cat_pages)
        content = cat_pages[page_idx]
        assert content is not None
    elapsed = time.perf_counter() - start

    assert elapsed < 0.05, f"Help page retrieval too slow: {elapsed:.3f}s for 5000 lookups"
