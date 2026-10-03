import json
import re
from pathlib import Path
from types import SimpleNamespace

from core.matching import CandidateScorer, clean_spotify_title
from core.spotify import parse_spotify_url
from utils.cache import TTLCache

GOLDEN_PATH = Path(__file__).parent / "golden_spotify_matches.json"


def test_parse_spotify_url():
    # Track URLs
    assert parse_spotify_url("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT") == (
        "track",
        "4cOdK2wGLETKBW3PvgPWqT",
    )
    assert parse_spotify_url("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT?si=abc123xyz") == (
        "track",
        "4cOdK2wGLETKBW3PvgPWqT",
    )
    assert parse_spotify_url("https://open.spotify.com/intl-de/track/4cOdK2wGLETKBW3PvgPWqT") == (
        "track",
        "4cOdK2wGLETKBW3PvgPWqT",
    )
    assert parse_spotify_url("spotify:track:4cOdK2wGLETKBW3PvgPWqT") == (
        "track",
        "4cOdK2wGLETKBW3PvgPWqT",
    )

    # Album URLs
    assert parse_spotify_url("https://open.spotify.com/album/4m2880jivSbbyEGAKfITCa") == (
        "album",
        "4m2880jivSbbyEGAKfITCa",
    )
    assert parse_spotify_url("spotify:album:4m2880jivSbbyEGAKfITCa") == (
        "album",
        "4m2880jivSbbyEGAKfITCa",
    )

    # Playlist URLs
    assert parse_spotify_url("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M") == (
        "playlist",
        "37i9dQZF1DXcBWIGoYBM5M",
    )
    assert parse_spotify_url("spotify:playlist:37i9dQZF1DXcBWIGoYBM5M") == (
        "playlist",
        "37i9dQZF1DXcBWIGoYBM5M",
    )

    # Non-Spotify queries
    assert parse_spotify_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ") is None
    assert parse_spotify_url("never gonna give you up") is None


def test_clean_spotify_title():
    strip_patterns = [
        re.compile(r"(?i)\s*\((?:feat\.|featuring|ft\.)\s*[^)]*\)"),
        re.compile(r"(?i)\s*\[(?:feat\.|featuring|ft\.)\s*[^\]]*\]"),
        re.compile(
            r"(?i)\s*-\s*(?:\d{4}\s+)?"
            r"(?:remaster(?:ed)?|anniversary|deluxe|bonus|re-recorded|edit|radio edit|mono|stereo|expanded).*$"
        ),
    ]

    assert clean_spotify_title("Song Name (feat. Artist B)", strip_patterns) == "Song Name"
    assert clean_spotify_title("Another Song [ft. Someone]", strip_patterns) == "Another Song"
    assert clean_spotify_title("Classic Rock - 2011 Remaster", strip_patterns) == "Classic Rock"
    assert clean_spotify_title("Track - Deluxe Edition", strip_patterns) == "Track"
    assert clean_spotify_title("Normal Title", strip_patterns) == "Normal Title"


def test_golden_spotify_matches():
    with open(GOLDEN_PATH, "r", encoding="utf-8") as f:
        cases = json.load(f)

    scorer = CandidateScorer()

    for case in cases:
        scenario = case["scenario"]
        target = case["target"]
        candidates = [
            SimpleNamespace(
                id=c["id"],
                title=c["title"],
                author=c["author"],
                duration=c["duration"],
                uri=f"https://music.youtube.com/watch?v={c['id']}" if c.get("is_ytm") else f"https://youtube.com/watch?v={c['id']}",
                is_ytm=c.get("is_ytm", False),
            )
            for c in case["candidates"]
        ]

        winner = scorer.best_match(
            target_title=target["title"],
            target_artists=target["artists"],
            target_duration_ms=target["duration_ms"],
            candidates=candidates,
        )

        assert winner is not None, f"Scenario '{scenario}' expected a winner but got None"
        assert (
            winner.id == case["expected_match_id"]
        ), f"Scenario '{scenario}': expected {case['expected_match_id']}, got {winner.id}"


def test_matching_engine_rejection_when_no_candidate_qualifies():
    scorer = CandidateScorer()
    # Target: "Bohemian Rhapsody" by Queen (5:55 = 355000ms)
    # Candidate: Completely different song and artist
    candidates = [
        SimpleNamespace(
            id="bad_cand",
            title="Smells Like Teen Spirit",
            author="Nirvana",
            duration=301000,
            uri="https://youtube.com/watch?v=bad",
            is_ytm=False,
        )
    ]
    winner = scorer.best_match(
        target_title="Bohemian Rhapsody",
        target_artists=["Queen"],
        target_duration_ms=355000,
        candidates=candidates,
    )
    assert winner is None


def test_matching_cache_ttl():
    cache = TTLCache(max_size=10, ttl=60.0)
    cache.set("spotify:track:123", "matched_audio_track")
    assert cache.get("spotify:track:123") == "matched_audio_track"
    assert cache.get("spotify:track:nonexistent") is None
