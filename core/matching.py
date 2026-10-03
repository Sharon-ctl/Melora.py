"""Audio candidate matching engine for Spotify tracks.

Scores candidate search results from YouTube Music and YouTube against Spotify metadata
using rules and weights defined in data/matching_rules.json.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from core.data_loader import load_matching_rules
from utils.cache import TTLCache
from utils.text import clean

log = logging.getLogger(__name__)

_WORD_RE = re.compile(r"\w+")


def clean_spotify_title(title: str, strip_patterns: list[re.Pattern[str]]) -> str:
    """Strip featuring artists and remaster/edition text from track titles."""
    cleaned = title
    for pat in strip_patterns:
        cleaned = pat.sub("", cleaned)
    return clean(cleaned)


def build_search_queries(title: str, artists: list[str], strip_patterns: list[re.Pattern[str]]) -> list[str]:
    """Generate search queries in priority order."""
    c_title = clean_spotify_title(title, strip_patterns)
    primary_artist = artists[0] if artists else ""
    all_artists = " ".join(artists) if artists else ""

    queries: list[str] = []
    if primary_artist:
        queries.append(f"{c_title} {primary_artist}")
    if len(artists) > 1 and all_artists:
        queries.append(f"{c_title} {all_artists}")
    if primary_artist:
        queries.append(f"{primary_artist} - {c_title}")
    queries.append(c_title)
    return queries


@dataclass(frozen=True)
class CandidateScore:
    candidate: Any
    score: float
    duration_diff_s: float
    passed_threshold: bool


class CandidateScorer:
    """Scores audio candidates against Spotify metadata."""

    def __init__(self, rules: dict[str, Any] | None = None) -> None:
        self.rules = rules or load_matching_rules()
        self.duration_tol = float(self.rules.get("duration_tolerance_seconds", 3.0))
        self.threshold = float(self.rules.get("match_threshold", 55.0))
        self.weights = self.rules.get("weights", {})
        self.w_duration = float(self.weights.get("duration", 50.0))
        self.w_artist = float(self.weights.get("artist", 30.0))
        self.w_title = float(self.weights.get("title", 20.0))
        self.w_topic = float(self.weights.get("topic_bonus", 10.0))
        self.w_ytm = float(self.weights.get("ytm_bonus", 10.0))
        self.penalty = float(self.weights.get("penalty", 40.0))
        self.penalized_keywords = [k.lower() for k in self.rules.get("penalized_keywords", [])]
        # Pre-compile strip patterns and penalized keyword regexes
        self.strip_patterns: list[re.Pattern[str]] = [re.compile(p) for p in self.rules.get("strip_patterns", [])]
        self._kw_patterns: list[tuple[str, re.Pattern[str]]] = [
            (kw, re.compile(rf"\b{re.escape(kw)}\b")) for kw in self.penalized_keywords
        ]

    def score_candidate(
        self,
        target_title: str,
        target_artists: list[str],
        target_duration_ms: int,
        candidate: Any,
    ) -> CandidateScore:
        c_title_clean = clean_spotify_title(target_title, self.strip_patterns).lower()
        cand_title = (getattr(candidate, "title", "") or "").lower()
        cand_author = (getattr(candidate, "author", "") or "").lower()
        cand_dur = int(getattr(candidate, "duration", 0) or 0)
        cand_uri = (getattr(candidate, "uri", "") or "").lower()

        # 1. Duration score (max w_duration, e.g. 50 pts)
        if target_duration_ms > 0 and cand_dur > 0:
            diff_s = abs(cand_dur - target_duration_ms) / 1000.0
            if diff_s <= 1.0:
                score_dur = self.w_duration
            elif diff_s <= self.duration_tol:
                score_dur = self.w_duration * 0.8
            elif diff_s <= 5.0:
                score_dur = self.w_duration * 0.3
            else:
                score_dur = 0.0
        else:
            diff_s = 0.0
            score_dur = self.w_duration * 0.5  # Neutral if duration unknown

        # 2. Title similarity (max w_title, e.g. 20 pts)
        target_words = set(_WORD_RE.findall(c_title_clean))
        if target_words:
            cand_words = set(_WORD_RE.findall(cand_title))
            matched_words = target_words.intersection(cand_words)
            overlap_ratio = len(matched_words) / len(target_words)
            score_title = self.w_title * overlap_ratio
        else:
            score_title = self.w_title if c_title_clean in cand_title else 0.0

        # 3. Artist match (max w_artist, e.g. 30 pts)
        score_artist = 0.0
        primary_artist = target_artists[0].lower() if target_artists else ""
        if primary_artist:
            if primary_artist in cand_author:
                score_artist = self.w_artist
            elif primary_artist in cand_title:
                score_artist = self.w_artist * 0.8
            elif any(a.lower() in cand_author or a.lower() in cand_title for a in target_artists[1:]):
                score_artist = self.w_artist * 0.6
        else:
            score_artist = self.w_artist * 0.5

        # 4. Topic and YTM bonuses
        bonus_topic = self.w_topic if ("topic" in cand_author or cand_title.endswith("- topic")) else 0.0
        bonus_ytm = (
            self.w_ytm
            if (
                getattr(candidate, "source_name", "") == "ytmsearch"
                or "music.youtube.com" in cand_uri
                or getattr(candidate, "is_ytm", False)
            )
            else 0.0
        )

        # 5. Penalties for live, remix, cover, etc.
        target_lower = target_title.lower()
        penalties = 0.0
        for kw, kw_pat in self._kw_patterns:
            # If keyword is in candidate title or author but not in the Spotify target title
            kw_match = bool(kw_pat.search(cand_title) or kw_pat.search(cand_author))
            if kw_match and not kw_pat.search(target_lower):
                penalties += self.penalty

        total_score = max(
            0.0,
            min(
                100.0,
                score_dur + score_title + score_artist + bonus_topic + bonus_ytm - penalties,
            ),
        )
        passed = total_score >= self.threshold

        return CandidateScore(
            candidate=candidate,
            score=total_score,
            duration_diff_s=diff_s,
            passed_threshold=passed,
        )

    def rank_candidates(
        self,
        target_title: str,
        target_artists: list[str],
        target_duration_ms: int,
        candidates: list[Any],
    ) -> list[CandidateScore]:
        scored = [
            self.score_candidate(target_title, target_artists, target_duration_ms, c)
            for c in candidates
        ]
        # Sort primarily by score descending
        return sorted(scored, key=lambda s: s.score, reverse=True)

    def best_match(
        self,
        target_title: str,
        target_artists: list[str],
        target_duration_ms: int,
        candidates: list[Any],
    ) -> Any | None:
        """Select the highest-scoring candidate, prioritizing duration within tolerance."""
        if not candidates:
            return None

        ranked = self.rank_candidates(target_title, target_artists, target_duration_ms, candidates)
        # 1. Prefer candidates strictly within duration tolerance (e.g. <= 3.0s)
        strict_candidates = [
            cs for cs in ranked if cs.passed_threshold and cs.duration_diff_s <= self.duration_tol
        ]
        if strict_candidates:
            return strict_candidates[0].candidate

        # 2. If nothing qualified within tolerance, allow looser candidates above threshold
        loose_candidates = [cs for cs in ranked if cs.passed_threshold]
        if loose_candidates:
            return loose_candidates[0].candidate

        return None


class SpotifyResolver:
    """Resolves Spotify metadata to audio tracks with tiered search, scoring, and caching."""

    def __init__(self, rules: dict[str, Any] | None = None, cache_size: int = 1000, ttl: float = 86400.0) -> None:
        self.scorer = CandidateScorer(rules)
        self.cache: TTLCache[str, Any] = TTLCache(max_size=cache_size, ttl=ttl)

    async def resolve(
        self,
        target_title: str,
        target_artists: list[str],
        target_duration_ms: int,
        spotify_uri: str,
        loader: Any,
        guild_id: int,
    ) -> Any | None:
        """Resolve a Spotify track lazily using ytmsearch, fallback ytsearch, and candidate scoring."""
        # 1. Check cache
        if spotify_uri:
            cached = self.cache.get(spotify_uri)
            if cached is not None:
                return cached

        queries = build_search_queries(target_title, target_artists, self.scorer.strip_patterns)
        if not queries:
            return None

        # Step 1: ytmsearch with primary query
        try:
            res = await loader.load_with_source(guild_id, "ytmsearch", queries[0])
            tracks = getattr(res, "tracks", []) or []
            match = self.scorer.best_match(target_title, target_artists, target_duration_ms, tracks)
            if match is not None:
                if spotify_uri:
                    self.cache.set(spotify_uri, match)
                return match
        except Exception as exc:
            log.debug("ytmsearch step 1 failed: %s", exc)

        # Step 2: ytmsearch with alternate query (if available)
        if len(queries) > 1:
            try:
                res = await loader.load_with_source(guild_id, "ytmsearch", queries[1])
                tracks = getattr(res, "tracks", []) or []
                match = self.scorer.best_match(target_title, target_artists, target_duration_ms, tracks)
                if match is not None:
                    if spotify_uri:
                        self.cache.set(spotify_uri, match)
                    return match
            except Exception as exc:
                log.debug("ytmsearch step 2 failed: %s", exc)

        # Step 3: ytsearch fallback
        try:
            res = await loader.load_with_source(guild_id, "ytsearch", queries[0])
            tracks = getattr(res, "tracks", []) or []
            match = self.scorer.best_match(target_title, target_artists, target_duration_ms, tracks)
            if match is not None:
                if spotify_uri:
                    self.cache.set(spotify_uri, match)
                return match
        except Exception as exc:
            log.debug("ytsearch step 3 failed: %s", exc)

        log.info("No matching audio candidate found for Spotify track: %s - %s", target_title, target_artists)
        return None
