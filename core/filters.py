"""Lavalink audio filters and EQ preset validation and building.

Validates all values strictly before constructing Lavalink Filter objects.
"""
from __future__ import annotations

import logging
from typing import Any

from lavalink.filters import Equalizer, Karaoke, Rotation, Timescale, Tremolo, Vibrato

log = logging.getLogger(__name__)


class FilterValidationError(Exception):
    """Raised when filter parameters fail validation."""


def validate_and_build_eq(bands: list[dict[str, Any]]) -> Equalizer:
    """Validate 15-band EQ settings and build an Equalizer filter."""
    if not isinstance(bands, list):
        raise FilterValidationError("EQ preset must be a list of band objects.")

    band_pairs: list[tuple[int, float]] = []
    for entry in bands:
        if not isinstance(entry, dict) or "band" not in entry or "gain" not in entry:
            raise FilterValidationError(f"Invalid band entry: {entry}")
        try:
            band_idx = int(entry["band"])
            gain_val = float(entry["gain"])
        except (ValueError, TypeError) as exc:
            raise FilterValidationError(f"Invalid band/gain format in {entry}: {exc}") from exc

        if not 0 <= band_idx <= 14:
            raise FilterValidationError(f"EQ band {band_idx} out of range (must be 0-14).")
        if not -0.25 <= gain_val <= 1.0:
            raise FilterValidationError(f"EQ gain {gain_val} out of range (must be -0.25 to 1.0).")
        band_pairs.append((band_idx, gain_val))

    eq = Equalizer()
    eq.update(bands=band_pairs)
    return eq


def validate_and_build_filters(config: dict[str, Any]) -> list[Any]:
    """Validate preset configuration and build Lavalink Filter instances."""
    if not isinstance(config, dict):
        raise FilterValidationError("Filter configuration must be a dictionary.")

    instances: list[Any] = []

    if "equalizer" in config:
        instances.append(validate_and_build_eq(config["equalizer"]))

    if "timescale" in config:
        ts_cfg = config["timescale"]
        if not isinstance(ts_cfg, dict):
            raise FilterValidationError("Timescale configuration must be a dictionary.")
        try:
            speed = float(ts_cfg.get("speed", 1.0))
            pitch = float(ts_cfg.get("pitch", 1.0))
            rate = float(ts_cfg.get("rate", 1.0))
        except (ValueError, TypeError) as exc:
            raise FilterValidationError(f"Invalid timescale numerical parameters: {exc}") from exc

        if speed < 0.1:
            raise FilterValidationError(f"Timescale speed {speed} must be >= 0.1.")
        if pitch <= 0.0:
            raise FilterValidationError(f"Timescale pitch {pitch} must be > 0.0.")
        if rate <= 0.0:
            raise FilterValidationError(f"Timescale rate {rate} must be > 0.0.")

        ts = Timescale()
        ts.update(speed=speed, pitch=pitch, rate=rate)
        instances.append(ts)

    if "rotation" in config:
        rot_cfg = config["rotation"]
        if not isinstance(rot_cfg, dict):
            raise FilterValidationError("Rotation configuration must be a dictionary.")
        try:
            hz = float(rot_cfg.get("rotation_hz", 0.0))
        except (ValueError, TypeError) as exc:
            raise FilterValidationError(f"Invalid rotation_hz: {exc}") from exc
        if hz < 0.0:
            raise FilterValidationError(f"Rotation frequency {hz} must be >= 0.0.")

        rot = Rotation()
        rot.update(rotation_hz=hz)
        instances.append(rot)

    if "karaoke" in config:
        k_cfg = config["karaoke"]
        if not isinstance(k_cfg, dict):
            raise FilterValidationError("Karaoke configuration must be a dictionary.")
        try:
            level = float(k_cfg.get("level", 1.0))
            mono_level = float(k_cfg.get("mono_level", 1.0))
            filter_band = float(k_cfg.get("filter_band", 220.0))
            filter_width = float(k_cfg.get("filter_width", 100.0))
        except (ValueError, TypeError) as exc:
            raise FilterValidationError(f"Invalid karaoke numerical parameters: {exc}") from exc

        k = Karaoke()
        k.update(level=level, mono_level=mono_level, filter_band=filter_band, filter_width=filter_width)
        instances.append(k)

    if "tremolo" in config:
        tr_cfg = config["tremolo"]
        if not isinstance(tr_cfg, dict):
            raise FilterValidationError("Tremolo configuration must be a dictionary.")
        try:
            freq = float(tr_cfg.get("frequency", 2.0))
            depth = float(tr_cfg.get("depth", 0.5))
        except (ValueError, TypeError) as exc:
            raise FilterValidationError(f"Invalid tremolo numerical parameters: {exc}") from exc

        if freq <= 0.0:
            raise FilterValidationError(f"Tremolo frequency {freq} must be > 0.0.")
        if not 0.0 < depth <= 1.0:
            raise FilterValidationError(f"Tremolo depth {depth} must be between 0.0 and 1.0.")

        trem = Tremolo()
        trem.update(frequency=freq, depth=depth)
        instances.append(trem)

    if "vibrato" in config:
        vib_cfg = config["vibrato"]
        if not isinstance(vib_cfg, dict):
            raise FilterValidationError("Vibrato configuration must be a dictionary.")
        try:
            freq = float(vib_cfg.get("frequency", 2.0))
            depth = float(vib_cfg.get("depth", 0.5))
        except (ValueError, TypeError) as exc:
            raise FilterValidationError(f"Invalid vibrato numerical parameters: {exc}") from exc

        if not 0.0 < freq <= 14.0:
            raise FilterValidationError(f"Vibrato frequency {freq} must be between 0.0 and 14.0.")
        if not 0.0 < depth <= 1.0:
            raise FilterValidationError(f"Vibrato depth {depth} must be between 0.0 and 1.0.")

        vib = Vibrato()
        vib.update(frequency=freq, depth=depth)
        instances.append(vib)

    return instances


def get_filter_types_for_preset(config: dict[str, Any]) -> list[Any]:
    """Return filter classes that are set by the given preset dictionary."""
    mapping: dict[str, Any] = {
        "equalizer": Equalizer,
        "timescale": Timescale,
        "rotation": Rotation,
        "karaoke": Karaoke,
        "tremolo": Tremolo,
        "vibrato": Vibrato,
    }
    return [mapping[key] for key in config if key in mapping]
