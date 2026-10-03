"""Error types, the global app command error handler, and the listener guard."""
from __future__ import annotations

import asyncio
import functools
import logging
import math
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import discord
from discord import app_commands

from utils import messages
from utils.interaction import reply

log = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True)
class RecordedError:
    error_id: str
    command: str
    summary: str
    timestamp: float


class ErrorRingBuffer:
    def __init__(self, maxlen: int = 50) -> None:
        self._buffer: deque[RecordedError] = deque(maxlen=maxlen)

    def record(self, error_id: str, command: str, summary: str) -> None:
        self._buffer.append(
            RecordedError(
                error_id=error_id,
                command=command,
                summary=summary,
                timestamp=time.time(),
            )
        )

    def get_recent(self, count: int = 10) -> list[RecordedError]:
        items = list(self._buffer)
        items.reverse()
        return items[:count]

    def clear(self) -> None:
        self._buffer.clear()

    def __len__(self) -> int:
        return len(self._buffer)


ERROR_BUFFER = ErrorRingBuffer(maxlen=50)


class BotUserError(app_commands.AppCommandError):
    """An expected problem that is explained to the user in one short sentence."""

    default_message = "**Something went wrong**\nAn unexpected error occurred."

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.default_message
        super().__init__(self.message)


class NotInGuild(BotUserError):
    default_message = messages.not_in_guild()

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.default_message)


class NotInVoice(BotUserError):
    default_message = messages.not_in_voice()

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.default_message)


class WrongChannel(BotUserError):
    default_message = messages.wrong_channel()

    def __init__(self, message: str | None = None, channel_id: int | None = None) -> None:
        super().__init__(message or (messages.wrong_channel(channel_id) if channel_id else self.default_message))


class NothingPlaying(BotUserError):
    default_message = messages.nothing_playing()

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.default_message)


class QueueFull(BotUserError):
    default_message = messages.queue_full()

    def __init__(self, message: str | None = None, max_size: int | None = None) -> None:
        super().__init__(message or (messages.queue_full(max_size) if max_size else self.default_message))


class UserLimitReached(BotUserError):
    def __init__(self, limit: int) -> None:
        super().__init__(messages.user_limit_reached(limit))


class TrackTooLong(BotUserError):
    default_message = messages.track_too_long()

    def __init__(self, message: str | None = None, max_minutes: int | None = None) -> None:
        super().__init__(message or (messages.track_too_long(max_minutes) if max_minutes else self.default_message))


class StageUnsupported(BotUserError):
    default_message = messages.stage_unsupported()

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.default_message)


class MissingVoicePermissions(BotUserError):
    default_message = messages.missing_voice_permissions()

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.default_message)


class DJRequired(BotUserError):
    default_message = messages.dj_required()

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.default_message)


class OwnerOnly(BotUserError):
    default_message = messages.owner_only()

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.default_message)


class OnCooldown(BotUserError):
    def __init__(self, retry_after: float) -> None:
        super().__init__(messages.cooldown_hit(math.ceil(retry_after)))


class NoMatches(BotUserError):
    default_message = messages.no_matches()

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.default_message)


class LoadFailed(BotUserError):
    default_message = messages.load_failed()

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.default_message)


class NodeOffline(BotUserError):
    default_message = messages.node_offline()

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.default_message)


class VoiceConnectFailed(BotUserError):
    default_message = messages.voice_connect_failed()

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.default_message)


class ServerBusy(BotUserError):
    default_message = messages.server_busy()

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.default_message)


class TookTooLong(BotUserError):
    default_message = messages.took_too_long()

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.default_message)


def new_error_id() -> str:
    """Short identifier that links a user-facing message to a log entry."""
    return secrets.token_hex(4)


def guarded(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T | None]]:
    """Wrap an event listener so exceptions are logged and never propagate."""

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> T | None:
        try:
            return await func(*args, **kwargs)
        except Exception:
            log.exception("Unhandled error in listener %s", func.__qualname__)
            return None

    return wrapper


def _permission_names(names: list[str]) -> str:
    return ", ".join(name.replace("_", " ") for name in names)


def format_interaction_error(interaction: discord.Interaction, error: BaseException) -> str | None:
    """Map any interaction error to a user-facing string or None if it should be swallowed."""
    original: BaseException = error
    if isinstance(error, app_commands.CommandInvokeError):
        original = error.original

    # 1. Swallowed errors (unknown/expired interaction or double acknowledgment)
    if isinstance(original, discord.NotFound) and getattr(original, "code", None) == 10062:
        log.debug("Interaction expired/unknown (10062); swallowing quietly")
        return None
    if isinstance(original, discord.HTTPException) and getattr(original, "code", None) == 40060:
        log.debug("Interaction already acknowledged (40060); swallowing quietly")
        return None

    # 2. Rate limits and cooldowns
    from utils.ratelimit import RateLimited

    if isinstance(original, RateLimited) or isinstance(error, RateLimited):
        retry_val = getattr(original, "retry_seconds", getattr(error, "retry_seconds", 1))
        return messages.rate_limited(retry_val)
    if isinstance(error, app_commands.CommandOnCooldown) or isinstance(original, app_commands.CommandOnCooldown):
        retry = math.ceil(getattr(original, "retry_after", getattr(error, "retry_after", 1)))
        return messages.rate_limited(max(1, retry))
    if isinstance(original, discord.HTTPException) and getattr(original, "status", None) == 429:
        retry_http = math.ceil(getattr(original, "retry_after", 2) or 2)
        return messages.rate_limited(max(1, retry_http))

    # 3. Outdated command / signature mismatch
    if (
        isinstance(error, (app_commands.CommandSignatureMismatch, app_commands.CommandNotFound))
        or isinstance(original, (app_commands.CommandSignatureMismatch, app_commands.CommandNotFound))
    ):
        cmd = interaction.command.qualified_name if interaction.command else "unknown"
        log.warning("App command outdated / signature mismatch for %s; slash command sync recommended", cmd)
        return messages.command_outdated()

    # 4. Expected bot errors
    if isinstance(original, BotUserError):
        return original.message

    # 5. Timeouts
    if isinstance(original, (TimeoutError, asyncio.TimeoutError)):
        return messages.took_too_long()

    # 6. Discord Forbidden and permissions
    if isinstance(original, discord.Forbidden):
        return messages.bot_forbidden()
    if isinstance(error, app_commands.MissingPermissions):
        return messages.missing_permissions()
    if isinstance(error, app_commands.BotMissingPermissions):
        return messages.bot_missing_permissions(_permission_names(error.missing_permissions))
    if isinstance(error, app_commands.NoPrivateMessage):
        return messages.not_in_guild()
    if isinstance(error, app_commands.CheckFailure):
        return messages.check_failure()
    if isinstance(error, app_commands.TransformerError):
        return messages.invalid_input()

    # 7. Unhandled / unexpected errors
    error_id = new_error_id()
    command = interaction.command.qualified_name if interaction.command else "unknown"
    log.error(
        "Unhandled command error id=%s command=%s guild=%s",
        error_id,
        command,
        interaction.guild_id,
        exc_info=(type(original), original, original.__traceback__),
    )
    ERROR_BUFFER.record(error_id, command, f"{type(original).__name__}: {str(original)[:100]}")
    alerter = getattr(interaction.client, "alerter", None)
    if alerter is not None:
        alerter.submit(f"Unhandled error in /{command}, error ID {error_id}.")
    return messages.unhandled_error(error_id)


async def handle_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    """The single error handler for every slash command."""
    # Record handler timing for error paths
    extras = getattr(interaction, "extras", None)
    if isinstance(extras, dict):
        start_time = extras.get("_start_time")
        if isinstance(start_time, (int, float)):
            duration = time.monotonic() - start_time
            cmd_name = interaction.command.qualified_name if getattr(interaction, "command", None) else "unknown"
            from utils.timing import record_handler_time

            record_handler_time(cmd_name, duration)

    message = format_interaction_error(interaction, error)
    if message is not None:
        try:
            await reply(interaction, message, ephemeral=True)
        except Exception as exc:
            log.debug("Failed delivering error response: %s", exc)
