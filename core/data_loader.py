"""Data loader for static configuration in the data/ directory.

Provides validated access to EQ presets, filter presets, and matching rules.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import discord

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


class DataValidationError(Exception):
    """Raised when static configuration files are malformed."""


def load_eq_presets(path: Path | None = None) -> dict[str, list[dict[str, float]]]:
    file_path = path or (DATA_DIR / "eq_presets.json")
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        log.error("Failed to read eq_presets.json: %s", exc)
        raise DataValidationError(f"Could not load EQ presets: {exc}") from exc

    if not isinstance(data, dict):
        raise DataValidationError("EQ presets must be a JSON object mapping name to bands")

    validated: dict[str, list[dict[str, float]]] = {}
    for name, bands in data.items():
        if not isinstance(bands, list):
            raise DataValidationError(f"EQ preset {name!r} must be a list of band objects")
        validated_bands: list[dict[str, float]] = []
        for band_entry in bands:
            if not isinstance(band_entry, dict) or "band" not in band_entry or "gain" not in band_entry:
                raise DataValidationError(f"Invalid band entry in preset {name!r}: {band_entry!r}")
            band_idx = int(band_entry["band"])
            gain_val = float(band_entry["gain"])
            if not 0 <= band_idx <= 14:
                raise DataValidationError(f"EQ band index {band_idx} out of range (0-14)")
            if not -0.25 <= gain_val <= 1.0:
                raise DataValidationError(f"EQ gain {gain_val} out of range (-0.25 to 1.0)")
            validated_bands.append({"band": float(band_idx), "gain": gain_val})
        validated[name.lower()] = validated_bands

    return validated


def load_filter_presets(path: Path | None = None) -> dict[str, dict[str, Any]]:
    file_path = path or (DATA_DIR / "filter_presets.json")
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        log.error("Failed to read filter_presets.json: %s", exc)
        raise DataValidationError(f"Could not load filter presets: {exc}") from exc

    if not isinstance(data, dict):
        raise DataValidationError("Filter presets must be a JSON object mapping name to filter configs")

    validated: dict[str, dict[str, Any]] = {}
    for name, config in data.items():
        if not isinstance(config, dict):
            raise DataValidationError(f"Filter preset {name!r} must be a JSON object")
        validated[name.lower()] = config

    return validated


def load_matching_rules(path: Path | None = None) -> dict[str, Any]:
    file_path = path or (DATA_DIR / "matching_rules.json")
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        log.error("Failed to read matching_rules.json: %s", exc)
        raise DataValidationError(f"Could not load matching rules: {exc}") from exc

    if not isinstance(data, dict):
        raise DataValidationError("Matching rules must be a JSON object")

    required = ("duration_tolerance_seconds", "match_threshold", "weights", "penalized_keywords", "strip_patterns")
    for field in required:
        if field not in data:
            raise DataValidationError(f"Missing required matching rule field: {field}")

    return data


def load_help_categories(path: Path | None = None) -> dict[str, Any]:
    file_path = path or (DATA_DIR / "help_categories.json")
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        log.error("Failed to read help_categories.json: %s", exc)
        return {
            "categories": ["Overview", "Playback", "Queue", "Library", "Filters", "Settings", "Info", "Owner"],
            "descriptions": {},
            "mapping": {},
        }
    return data


def load_rate_limits(path: Path | None = None) -> dict[str, Any]:
    file_path = path or (DATA_DIR / "rate_limits.json")
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        log.error("Failed to read rate_limits.json: %s", exc)
        raise DataValidationError(f"Could not load rate limits: {exc}") from exc

    if not isinstance(data, dict):
        raise DataValidationError("Rate limits must be a JSON object")

    buckets = data.get("buckets")
    if not isinstance(buckets, dict):
        raise DataValidationError("Rate limits missing 'buckets' object")

    validated_buckets: dict[str, dict[str, float]] = {}
    for name, b_cfg in buckets.items():
        if not isinstance(b_cfg, dict) or "rate" not in b_cfg or "per" not in b_cfg:
            raise DataValidationError(f"Invalid bucket config for {name!r}: {b_cfg!r}")
        try:
            rate = int(b_cfg["rate"])
            per = float(b_cfg["per"])
        except (ValueError, TypeError) as exc:
            raise DataValidationError(f"Bucket {name!r} rate/per must be numeric") from exc
        if rate <= 0 or per <= 0:
            raise DataValidationError(f"Bucket {name!r} rate and per must be positive")
        validated_buckets[str(name).lower()] = {"rate": float(rate), "per": per}

    max_keys = int(data.get("max_keys", 10000))
    idle_ttl = float(data.get("idle_ttl", 60.0))
    if max_keys <= 0 or idle_ttl <= 0:
        raise DataValidationError("max_keys and idle_ttl must be positive")

    return {
        "buckets": validated_buckets,
        "max_keys": max_keys,
        "idle_ttl": idle_ttl,
    }


DEFAULT_EMOJI_LABELS: dict[str, str] = {
    "pause": "Pause",
    "resume": "Resume",
    "previous": "Previous",
    "skip": "Skip",
    "loop": "Loop",
    "stop": "Stop",
}

_EMOJI_CACHE: dict[str, discord.PartialEmoji | None] | None = None


def load_emojis(path: Path | None = None, force_reload: bool = False) -> dict[str, discord.PartialEmoji | None]:
    """Validate data/emojis.json and build discord.PartialEmoji objects with text fallback."""
    global _EMOJI_CACHE
    if _EMOJI_CACHE is not None and path is None and not force_reload:
        return _EMOJI_CACHE

    file_path = path or (DATA_DIR / "emojis.json")
    raw_data: dict[str, Any] = {}
    if file_path.exists():
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    raw_data = loaded
                else:
                    log.warning("data/emojis.json is not a JSON object; falling back to labels")
        except Exception as exc:
            log.warning("Failed to load emojis from %s: %s; falling back to labels", file_path, exc)
    else:
        log.warning("Emoji config file not found at %s; falling back to labels", file_path)

    emojis: dict[str, discord.PartialEmoji | None] = {}
    for key, fallback in DEFAULT_EMOJI_LABELS.items():
        val = raw_data.get(key)
        emoji_obj: discord.PartialEmoji | None = None
        if val is not None:
            try:
                if isinstance(val, int) and val > 0:
                    emoji_obj = discord.PartialEmoji(name=key, id=val, animated=False)
                elif isinstance(val, str) and val.strip().isdigit() and int(val.strip()) > 0:
                    emoji_obj = discord.PartialEmoji(name=key, id=int(val.strip()), animated=False)
                elif isinstance(val, dict) and "id" in val and str(val["id"]).isdigit():
                    emoji_obj = discord.PartialEmoji(
                        name=str(val.get("name", key)),
                        id=int(val["id"]),
                        animated=bool(val.get("animated", False)),
                    )
                else:
                    log.warning("Invalid emoji entry for %r: %r; falling back to label %r", key, val, fallback)
            except Exception as exc:
                log.warning("Failed constructing PartialEmoji for %r: %s; falling back to label %r", key, exc, fallback)
        else:
            log.warning("Missing emoji entry for %r; falling back to label %r", key, fallback)

        emojis[key] = emoji_obj

    if path is None and not force_reload:
        _EMOJI_CACHE = emojis
    return emojis


