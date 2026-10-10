"""Configuration loading and validation.

All settings come from environment variables, optionally loaded from a .env
file next to this module. Every problem is collected and reported together so
the operator can fix them in one pass.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_SOURCE_RE = re.compile(r"^[a-z0-9]{2,20}$")
_WEBHOOK_PREFIXES = (
    "https://discord.com/api/webhooks/",
    "https://discordapp.com/api/webhooks/",
)
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


class ConfigError(Exception):
    """Raised when the configuration is missing or invalid."""


@dataclass(frozen=True)
class NodeConfig:
    name: str
    host: str
    port: int
    password: str = field(repr=False)
    region: str = "us"
    ssl: bool = False
    secure: bool = False

    def __post_init__(self) -> None:
        if self.secure and not self.ssl:
            object.__setattr__(self, "ssl", True)
        elif self.ssl and not self.secure:
            object.__setattr__(self, "secure", True)


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    owner_id: int
    nodes: tuple[NodeConfig, ...]
    sync_on_start: bool
    default_search_source: str
    fallback_search_source: str
    dj_role_id: int
    max_queue_size: int
    max_per_user: int
    max_playlist_tracks: int
    max_track_seconds: int
    default_volume: int
    idle_timeout: int
    alone_timeout: int
    load_timeout: float
    max_concurrent_loads: int
    per_guild_concurrent_loads: int
    failure_breaker: int
    watchdog_interval: int
    heartbeat_interval: int
    autocomplete_enabled: bool
    play_cooldown: float
    node_loss_grace: int
    log_level: str
    log_dir: str
    alert_webhook_url: str = field(repr=False)
    alert_min_interval: int
    spotify_enabled: bool = True
    vote_skip_enabled: bool = True
    vote_skip_min_listeners: int = 3
    max_favorites_per_user: int = 0
    max_playlists_per_user: int = 0
    max_tracks_per_playlist: int = 0
    history_size: int = 50
    autoplay_batch_size: int = 5
    backup_count: int = 7
    db_path: str = "data/bot.db"
    mention_reply_cooldown: float = 10.0
    autocomplete_search_enabled: bool = True
    mention_reply_enabled: bool = True
    voice_status_enabled: bool = True
    voice_status_use_emoji: bool = True
    user_history_enabled: bool = True
    user_history_max: int = 50

    @property
    def owner_ids(self) -> tuple[int, ...]:
        return (self.owner_id,)

    def secrets(self) -> tuple[str, ...]:
        """Every value that must never appear in logs."""
        values = [self.token, self.alert_webhook_url]
        values.extend(node.password for node in self.nodes)
        return tuple(v for v in values if v)


class _Reader:
    """Typed accessors over an environment mapping that collect errors."""

    def __init__(self, env: Mapping[str, str]) -> None:
        self._env = env
        self.errors: list[str] = []

    def text(self, key: str, default: str | None = None, *, required: bool = False) -> str:
        raw = self._env.get(key)
        value = raw.strip() if raw is not None else ""
        if value:
            return value
        if required:
            self.errors.append(f"{key} is required")
            return ""
        return default if default is not None else ""

    def integer(self, key: str, default: int, minimum: int, maximum: int) -> int:
        raw = self.text(key, str(default))
        try:
            value = int(raw)
        except ValueError:
            self.errors.append(f"{key} must be an integer (got {raw!r})")
            return default
        if not minimum <= value <= maximum:
            self.errors.append(f"{key} must be between {minimum} and {maximum} (got {value})")
            return default
        return value

    def number(self, key: str, default: float, minimum: float, maximum: float) -> float:
        raw = self.text(key, str(default))
        try:
            value = float(raw)
        except ValueError:
            self.errors.append(f"{key} must be a number (got {raw!r})")
            return default
        if not minimum <= value <= maximum:
            self.errors.append(f"{key} must be between {minimum} and {maximum} (got {value})")
            return default
        return value

    def boolean(self, key: str, default: bool) -> bool:
        raw = self.text(key, "true" if default else "false").lower()
        if raw in ("1", "true", "yes", "on"):
            return True
        if raw in ("0", "false", "no", "off"):
            return False
        self.errors.append(f"{key} must be true or false (got {raw!r})")
        return default

    def source(self, key: str, default: str) -> str:
        value = self.text(key, default).lower()
        if not _SOURCE_RE.match(value):
            self.errors.append(f"{key} must be a short lowercase source prefix such as ytsearch (got {value!r})")
            return default
        return value


def _parse_nodes_list(reader: _Reader, raw: Any, default_region: str = "us") -> list[NodeConfig]:
    if not raw:
        return []
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            reader.errors.append(f"LAVALINK_NODES is not valid JSON ({exc.msg})")
            return []
    elif isinstance(raw, list):
        data = raw
    else:
        reader.errors.append("LAVALINK_NODES must be a JSON list of objects")
        return []

    if not isinstance(data, list):
        reader.errors.append("LAVALINK_NODES must be a JSON list of objects")
        return []
    nodes: list[NodeConfig] = []
    for index, entry in enumerate(data):
        label = f"LAVALINK_NODES[{index}]"
        if not isinstance(entry, dict):
            reader.errors.append(f"{label} must be an object")
            continue
        host = str(entry.get("host", "")).strip()
        password = str(entry.get("password", ""))
        try:
            port = int(entry.get("port", 0))
        except (TypeError, ValueError):
            port = 0
        if not host or not password or not 1 <= port <= 65535:
            reader.errors.append(f"{label} needs host, port (1-65535) and password")
            continue
        secure = bool(entry.get("secure", entry.get("ssl", False)))
        nodes.append(
            NodeConfig(
                name=str(entry.get("name") or f"node-{index + 1}"),
                host=host,
                port=port,
                password=password,
                region=str(entry.get("region") or default_region),
                ssl=secure,
                secure=secure,
            )
        )
    return nodes


def _parse_extra_nodes(reader: _Reader, raw: str, default_region: str) -> list[NodeConfig]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        reader.errors.append(f"LAVALINK_EXTRA_NODES is not valid JSON ({exc.msg})")
        return []
    if not isinstance(data, list):
        reader.errors.append("LAVALINK_EXTRA_NODES must be a JSON list of objects")
        return []
    nodes: list[NodeConfig] = []
    for index, entry in enumerate(data):
        label = f"LAVALINK_EXTRA_NODES[{index}]"
        if not isinstance(entry, dict):
            reader.errors.append(f"{label} must be an object")
            continue
        host = str(entry.get("host", "")).strip()
        password = str(entry.get("password", ""))
        try:
            port = int(entry.get("port", 0))
        except (TypeError, ValueError):
            port = 0
        if not host or not password or not 1 <= port <= 65535:
            reader.errors.append(f"{label} needs host, port (1-65535) and password")
            continue
        secure = bool(entry.get("secure", entry.get("ssl", False)))
        nodes.append(
            NodeConfig(
                name=str(entry.get("name") or f"extra-{index + 1}"),
                host=host,
                port=port,
                password=password,
                region=str(entry.get("region") or default_region),
                ssl=secure,
                secure=secure,
            )
        )
    return nodes


def load_config(env: Mapping[str, str] | None = None, dotenv_path: str | Path | None = None) -> Config:
    """Load and validate configuration.

    Pass ``env`` to validate a mapping directly (used by tests). Otherwise the
    .env file is loaded into the process environment first.
    """
    if env is None:
        load_dotenv(dotenv_path or Path(__file__).with_name(".env"))
        env = os.environ
    r = _Reader(env)

    token = r.text("DISCORD_TOKEN", required=True)
    if token and (" " in token or len(token) < 30):
        r.errors.append("DISCORD_TOKEN does not look like a bot token")
    owner_raw = r.text("OWNER_ID", required=True)
    owner_id = 0
    if owner_raw:
        try:
            owner_id = int(owner_raw)
            if owner_id < 1:
                raise ValueError
        except ValueError:
            r.errors.append("OWNER_ID must be a positive integer Discord user ID")

    raw_nodes = env.get("LAVALINK_NODES")
    nodes = _parse_nodes_list(r, raw_nodes)
    if not nodes:
        host = r.text("LAVALINK_HOST", required=True)
        port = r.integer("LAVALINK_PORT", 2333, 1, 65535)
        password = r.text("LAVALINK_PASSWORD", required=True)
        region = r.text("LAVALINK_REGION", "us")
        secure = r.boolean("LAVALINK_SECURE", r.boolean("LAVALINK_SSL", False))
        nodes = [
            NodeConfig(
                name=r.text("LAVALINK_NAME", "main"),
                host=host,
                port=port,
                password=password,
                region=region,
                ssl=secure,
                secure=secure,
            )
        ]
        nodes.extend(_parse_extra_nodes(r, r.text("LAVALINK_EXTRA_NODES"), region))
    else:
        extra = r.text("LAVALINK_EXTRA_NODES")
        if extra:
            nodes.extend(_parse_extra_nodes(r, extra, "us"))

    names = [n.name for n in nodes]
    if len(set(names)) != len(names):
        r.errors.append("Lavalink node names must be unique")

    max_queue = r.integer("MAX_QUEUE_SIZE", 0, 0, 500000)
    raw_per_user = r.integer("MAX_PER_USER", 0, 0, 500000)
    per_user = min(raw_per_user, max_queue) if (max_queue > 0 and raw_per_user > 0) else raw_per_user
    log_level = r.text("LOG_LEVEL", "INFO").upper()
    if log_level not in _LOG_LEVELS:
        r.errors.append(f"LOG_LEVEL must be one of {', '.join(_LOG_LEVELS)}")
        log_level = "INFO"
    webhook = r.text("ALERT_WEBHOOK_URL")
    if webhook and not webhook.startswith(_WEBHOOK_PREFIXES):
        r.errors.append("ALERT_WEBHOOK_URL must be a Discord webhook URL")
        webhook = ""

    config = Config(
        token=token,
        owner_id=owner_id,
        nodes=tuple(nodes),
        sync_on_start=r.boolean("SYNC_ON_START", False),
        default_search_source=r.source("DEFAULT_SEARCH_SOURCE", "ytsearch"),
        fallback_search_source=r.source("FALLBACK_SEARCH_SOURCE", "scsearch"),
        dj_role_id=r.integer("DJ_ROLE_ID", 0, 0, 2**63 - 1),
        max_queue_size=max_queue,
        max_per_user=per_user,
        max_playlist_tracks=r.integer("MAX_PLAYLIST_TRACKS", 0, 0, 500000),
        max_track_seconds=r.integer("MAX_TRACK_SECONDS", 0, 0, 864000),
        default_volume=r.integer("DEFAULT_VOLUME", 100, 0, 100),
        idle_timeout=r.integer("IDLE_TIMEOUT", 300, 10, 86400),
        alone_timeout=r.integer("ALONE_TIMEOUT", 60, 5, 3600),
        load_timeout=r.number("LOAD_TIMEOUT", 15.0, 2.0, 120.0),
        max_concurrent_loads=r.integer("MAX_CONCURRENT_LOADS", 64, 1, 1024),
        per_guild_concurrent_loads=r.integer("PER_GUILD_CONCURRENT_LOADS", 4, 1, 64),
        failure_breaker=r.integer("FAILURE_BREAKER", 3, 1, 20),
        watchdog_interval=r.integer("WATCHDOG_INTERVAL", 60, 10, 3600),
        heartbeat_interval=r.integer("HEARTBEAT_INTERVAL", 300, 30, 86400),
        autocomplete_enabled=r.boolean("AUTOCOMPLETE_ENABLED", False),
        play_cooldown=r.number("PLAY_COOLDOWN", 0.0, 0.0, 120.0),
        node_loss_grace=r.integer("NODE_LOSS_GRACE", 30, 0, 600),
        log_level=log_level,
        log_dir=r.text("LOG_DIR", "logs"),
        alert_webhook_url=webhook,
        alert_min_interval=r.integer("ALERT_MIN_INTERVAL", 600, 30, 86400),
        spotify_enabled=r.boolean("SPOTIFY_ENABLED", True),
        vote_skip_enabled=r.boolean("VOTE_SKIP_ENABLED", True),
        vote_skip_min_listeners=r.integer("VOTE_SKIP_MIN_LISTENERS", 3, 1, 100),
        max_favorites_per_user=r.integer("MAX_FAVORITES_PER_USER", 0, 0, 500000),
        max_playlists_per_user=r.integer("MAX_PLAYLISTS_PER_USER", 0, 0, 500000),
        max_tracks_per_playlist=r.integer("MAX_TRACKS_PER_PLAYLIST", 0, 0, 500000),
        history_size=r.integer("HISTORY_SIZE", 50, 5, 500),
        autoplay_batch_size=r.integer("AUTOPLAY_BATCH_SIZE", 5, 1, 25),
        backup_count=r.integer("BACKUP_COUNT", 7, 1, 100),
        db_path=r.text("DB_PATH", "data/bot.db"),
        mention_reply_cooldown=r.number("MENTION_REPLY_COOLDOWN", 10.0, 0.0, 3600.0),
        autocomplete_search_enabled=r.boolean("AUTOCOMPLETE_SEARCH_ENABLED", True),
        mention_reply_enabled=r.boolean("MENTION_REPLY_ENABLED", True),
        voice_status_enabled=r.boolean("VOICE_STATUS_ENABLED", True),
        voice_status_use_emoji=r.boolean("VOICE_STATUS_USE_EMOJI", True),
        user_history_enabled=r.boolean("USER_HISTORY_ENABLED", True),
        user_history_max=r.integer("USER_HISTORY_MAX", 50, 1, 500),
    )
    if r.errors:
        details = "\n".join(f" - {line}" for line in r.errors)
        raise ConfigError(f"Invalid configuration:\n{details}")
    return config
