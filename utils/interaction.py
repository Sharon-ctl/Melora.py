"""Safe helpers for answering interactions exactly once.

Every call checks whether a response was already sent, applies a timeout, and
tolerates expired tokens, double acknowledgements, and missing permissions.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import discord

log = logging.getLogger(__name__)

DEFER_TIMEOUT = 2.5
SEND_TIMEOUT = 10.0
MAX_CONTENT = 1900
_NO_MENTIONS = discord.AllowedMentions.none()
_PUBLIC_DEFERRED = "public_deferred"


def _record_ack(interaction: discord.Interaction) -> None:
    extras = getattr(interaction, "extras", None)
    if not isinstance(extras, dict):
        return
    start_time = extras.get("_start_time")
    if isinstance(start_time, (int, float)) and "_ack_time" not in extras:
        ack_latency = time.monotonic() - start_time
        extras["_ack_time"] = time.monotonic()
        cmd = interaction.command.qualified_name if getattr(interaction, "command", None) else "unknown"
        from utils.timing import record_ack_latency

        record_ack_latency(cmd, ack_latency)


async def safe_defer(interaction: discord.Interaction, *, ephemeral: bool = False) -> bool:
    """Acknowledge the interaction. Returns False if it can no longer be answered."""
    if interaction.response.is_done():
        _record_ack(interaction)
        return True
    if interaction.is_expired():
        log.info("Interaction expired before it could be deferred")
        return False
    try:
        await asyncio.wait_for(
            interaction.response.defer(ephemeral=ephemeral, thinking=True),
            timeout=DEFER_TIMEOUT,
        )
        _record_ack(interaction)
    except discord.InteractionResponded:
        _record_ack(interaction)
        return True
    except (discord.NotFound, discord.Forbidden):
        log.info("Interaction expired or forbidden before it could be deferred")
        return False
    except discord.HTTPException as exc:
        if exc.code == 40060:
            log.debug("Interaction was already acknowledged")
            return True
        if exc.code == 10062:
            log.info("Interaction expired before it could be deferred (10062)")
            return False
        log.warning("Could not defer interaction (status %s, code %s): %s", exc.status, exc.code, exc.text)
        return False
    except asyncio.TimeoutError:
        log.info("Interaction defer timed out (expired)")
        return False
    if not ephemeral:
        interaction.extras[_PUBLIC_DEFERRED] = True
    return True


async def reply(
    interaction: discord.Interaction,
    content: str,
    *,
    ephemeral: bool = False,
    view: Any | None = None,
) -> bool:
    """Send one response, choosing between the initial response and a follow-up.

    An ephemeral error after a public defer replaces the public "thinking"
    message with a private one. Returns True if the message was delivered.
    """
    content = content[:MAX_CONTENT] if content else ""
    if interaction.is_expired() and not interaction.response.is_done():
        log.info("Interaction expired before reply could be sent")
        return False

    if view is None and content and len(content.strip().splitlines()) > 1:
        from utils.components_v2 import BaseCardView, TextDisplay, create_card_container, reply_card

        text_disp = TextDisplay(content)
        container = create_card_container(text_disp)
        user = getattr(interaction, "user", None)
        user_id = getattr(user, "id", 0) if user else 0
        card_view = BaseCardView(timeout=60.0, author_id=user_id)
        card_view.add_item(container)
        return await reply_card(interaction, card_view, ephemeral=ephemeral)

    kwargs: dict[str, Any] = {"allowed_mentions": _NO_MENTIONS, "ephemeral": ephemeral}
    if content:
        kwargs["content"] = content
    if view is not None:
        kwargs["view"] = view

    try:
        if not interaction.response.is_done():
            try:
                await asyncio.wait_for(
                    interaction.response.send_message(**kwargs),
                    timeout=SEND_TIMEOUT,
                )
                _record_ack(interaction)
                return True
            except discord.InteractionResponded:
                log.debug("Interaction was responded concurrently; falling back to followup")
            except discord.HTTPException as exc:
                if exc.code == 40060:
                    log.debug("Interaction was already acknowledged; falling back to followup")
                else:
                    raise

        thinking = bool(interaction.extras.pop(_PUBLIC_DEFERRED, False))
        if ephemeral and thinking:
            try:
                await asyncio.wait_for(interaction.delete_original_response(), timeout=SEND_TIMEOUT)
            except (discord.NotFound, discord.HTTPException, asyncio.TimeoutError) as exc:
                log.debug("Could not delete original thinking response: %s", exc)
                try:
                    await asyncio.wait_for(
                        interaction.edit_original_response(content=content, view=view),
                        timeout=SEND_TIMEOUT,
                    )
                    return True
                except (discord.NotFound, discord.HTTPException, asyncio.TimeoutError) as edit_exc:
                    log.debug("Could not edit original thinking response: %s", edit_exc)

        await asyncio.wait_for(
            interaction.followup.send(**kwargs),
            timeout=SEND_TIMEOUT,
        )
        return True
    except discord.NotFound:
        log.info("Interaction token expired; reply dropped")
    except discord.Forbidden:
        log.info("Missing permission to reply to an interaction")
    except (discord.HTTPException, asyncio.TimeoutError) as exc:
        log.warning("Failed to deliver an interaction reply: %s", exc)
    return False
