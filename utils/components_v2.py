"""Components V2 helper module for rich cards using discord.py 2.7.x LayoutView.

Rules enforced:
- Use LayoutView with Container, TextDisplay, and ActionRow.
- Consistent neutral gray accent colour (no bright/custom colours).
- No divider/separator component anywhere.
- All buttons use ButtonStyle.secondary, EXCEPT Stop button on nowplaying card (ButtonStyle.danger).
- A Components V2 message cannot carry normal content or embeds.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import math
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import discord
from discord import ButtonStyle
from discord.ui import ActionRow, Button, Container, LayoutView, Section, Select, TextDisplay, Thumbnail

from core.data_loader import load_emojis
from utils import messages
from utils.text import format_duration

log = logging.getLogger(__name__)


def _interaction_is_done(interaction: Any) -> bool:
    resp = getattr(interaction, "response", None)
    if resp is None:
        return False
    is_done_fn = getattr(resp, "is_done", None)
    if is_done_fn is None:
        return False
    try:
        done = is_done_fn()
        if inspect.iscoroutine(done):
            done.close()
            return False
        return bool(done)
    except Exception:
        return False

__all__ = (
    "ACTIVE_VIEWS",
    "ActiveViewsRegistry",
    "ActionRow",
    "Button",
    "Container",
    "LayoutView",
    "Section",
    "Select",
    "TextDisplay",
    "Thumbnail",
    "BaseCardView",
    "NowPlayingView",
    "PaginatedPage",
    "PaginatedView",
    "NEUTRAL_GRAY_ACCENT",
    "build_nowplaying_action_row",
    "build_nowplaying_container",
    "create_card_container",
    "create_info_card",
    "escape_link_title",
    "reply_card",
    "secondary_button",
)

# Consistent neutral gray accent colour
NEUTRAL_GRAY_ACCENT = discord.Colour(0x4A4D53)
CARD_SEND_TIMEOUT = 10.0
_NO_MENTIONS = discord.AllowedMentions.none()
_IN_FLIGHT_MESSAGES: set[int] = set()


def secondary_button(
    label: str,
    *,
    custom_id: str | None = None,
    disabled: bool = False,
    callback: Any | None = None,
) -> Button[Any]:
    """Create a button guaranteed to use ButtonStyle.secondary."""
    btn = Button(
        style=ButtonStyle.secondary,
        label=label,
        disabled=disabled,
        custom_id=custom_id,
    )
    if callback is not None:
        btn.callback = callback
    return btn


def create_card_container(*items: Any, accent: discord.Colour | None = NEUTRAL_GRAY_ACCENT) -> Container[Any]:
    """Create a Container with neutral gray accent colour. No divider component allowed."""
    return Container(*items, accent_colour=accent)


class ActiveViewsRegistry:
    """Bounded registry of active Components V2 views keyed by (guild_id, user_id, kind)."""

    def __init__(self, max_size: int = 1000) -> None:
        self._max_size = max_size
        self._views: OrderedDict[tuple[int, int, str], BaseCardView] = OrderedDict()

    async def register(self, view: BaseCardView) -> None:
        user_id = view.author_id or view.owner_id or 0
        if not user_id or not view.kind:
            return

        key = (view.guild_id, user_id, view.kind)
        existing = self._views.get(key)
        if existing is not None and existing is not view:
            try:
                await existing.disable_and_stop()
            except Exception as exc:
                log.debug("Failed stopping previous active view: %s", exc)

        self._views[key] = view
        self._views.move_to_end(key)

        while len(self._views) > self._max_size:
            _, old_view = self._views.popitem(last=False)
            try:
                await old_view.disable_and_stop()
            except Exception as exc:
                log.debug("Active views eviction cleanup failed: %s", exc)

    def remove(self, view: BaseCardView) -> None:
        user_id = view.author_id or view.owner_id or 0
        key = (view.guild_id, user_id, view.kind)
        if self._views.get(key) is view:
            self._views.pop(key, None)

    async def close_all(self) -> None:
        views = list(self._views.values())
        self._views.clear()
        for v in views:
            try:
                await v.disable_and_stop()
            except Exception as exc:
                log.debug("Active views close_all cleanup failed: %s", exc)


ACTIVE_VIEWS = ActiveViewsRegistry(max_size=1000)


class BaseCardView(LayoutView):
    """Base LayoutView for Components V2 cards."""

    def __init__(
        self,
        *,
        timeout: float = 120.0,
        author_id: int | None = None,
        guild_id: int = 0,
        kind: str = "generic",
    ) -> None:
        super().__init__(timeout=timeout)
        self.author_id: int | None = author_id
        self.owner_id: int | None = author_id
        self.guild_id: int = guild_id
        self.kind: str = kind
        self.channel_id: int | None = None
        self.message_id: int | None = None
        self.interaction: discord.Interaction | None = None
        self.is_ephemeral: bool = False
        self._client: discord.Client | None = None
        self._stopped_or_timed_out: bool = False

    async def _scheduled_task(self, item: Any, interaction: discord.Interaction) -> None:
        """Silent per-message button guard: while one press is running, quietly defer subsequent presses."""
        msg = getattr(interaction, "message", None)
        msg_id = getattr(msg, "id", None) if msg else None
        if msg_id is not None:
            if msg_id in _IN_FLIGHT_MESSAGES:
                if hasattr(interaction, "response") and not _interaction_is_done(interaction):
                    try:
                        await interaction.response.defer()
                    except Exception as defer_exc:
                        log.debug("Silent defer failed: %s", defer_exc)
                return
            _IN_FLIGHT_MESSAGES.add(msg_id)
        try:
            await super()._scheduled_task(item, interaction)
        finally:
            if msg_id is not None:
                _IN_FLIGHT_MESSAGES.discard(msg_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        client = getattr(interaction, "client", None)
        rate_limiter = getattr(client, "rate_limiter", None)
        cfg = getattr(client, "cfg", None)
        owner_id = getattr(cfg, "owner_id", 0) if cfg else 0
        user_id = getattr(getattr(interaction, "user", None), "id", 0)

        # 1. Rate limiter check for buttons and selects (3 per 3s)
        if rate_limiter is not None:
            allowed, retry_after = rate_limiter.acquire_component(user_id, owner_id)
            if not allowed:
                sec = max(1, math.ceil(retry_after))
                if hasattr(interaction, "response") and not _interaction_is_done(interaction):
                    try:
                        await interaction.response.send_message(
                            messages.rate_limited(sec),
                            ephemeral=True,
                            allowed_mentions=_NO_MENTIONS,
                         )
                    except Exception as exc:
                        log.debug("Failed sending component rate limit: %s", exc)
                return False

        # 2. Owner check
        effective_author = self.author_id if self.author_id is not None else self.owner_id
        if effective_author is not None and user_id != effective_author:
            if hasattr(interaction, "response") and not _interaction_is_done(interaction):
                try:
                    await interaction.response.send_message(
                        messages.not_your_menu(),
                        ephemeral=True,
                        allowed_mentions=_NO_MENTIONS,
                    )
                except Exception as exc:
                    log.debug("Failed sending not_your_menu: %s", exc)
            return False

        return True

    def disable_all_items(self) -> None:
        """Disable every button and select across all containers and action rows."""
        for item in self.walk_children():
            if isinstance(item, (Button, Select)):
                item.disabled = True

    disable_all_buttons = disable_all_items

    async def disable_and_stop(self) -> None:
        """Rebuild content with every item disabled, edit message, and release references."""
        if self._stopped_or_timed_out:
            return
        self._stopped_or_timed_out = True

        self.disable_all_items()

        # Edit message to show disabled items
        if not self.is_ephemeral:
            if hasattr(self, "message") and self.message is not None:
                try:
                    await self.message.edit(view=self, allowed_mentions=_NO_MENTIONS)
                except (discord.NotFound, discord.Forbidden):
                    log.debug("View message already gone or forbidden on timeout")
                except Exception as exc:
                    log.debug("Failed editing timed-out view via message: %s", exc)
            elif self.channel_id and self.message_id and self._client:
                try:
                    channel = self._client.get_channel(self.channel_id)
                    if channel is None and hasattr(self._client, "fetch_channel"):
                        try:
                            channel = await self._client.fetch_channel(self.channel_id)
                        except Exception as exc:
                            log.debug("Failed fetching channel on timeout: %s", exc)
                    if channel and hasattr(channel, "get_partial_message"):
                        partial_msg = channel.get_partial_message(self.message_id)
                        await partial_msg.edit(view=self, allowed_mentions=_NO_MENTIONS)
                except (discord.NotFound, discord.Forbidden):
                    log.debug("Partial message already gone or forbidden on timeout")
                except Exception as exc:
                    log.debug("Failed editing timed-out normal view: %s", exc)
        elif self.is_ephemeral and self.interaction:
            try:
                await self.interaction.edit_original_response(view=self)
            except (discord.NotFound, discord.Forbidden):
                log.debug("Ephemeral interaction expired or forbidden on timeout")
            except Exception as exc:
                log.debug("Failed editing timed-out ephemeral view: %s", exc)

        ACTIVE_VIEWS.remove(self)
        self.release_references()
        self.stop()

    def release_references(self) -> None:
        """Drop all references to Discord objects and interactions."""
        self.interaction = None
        self._client = None
        if hasattr(self, "message"):
            self.message = None

    def stop(self) -> None:
        ACTIVE_VIEWS.remove(self)
        super().stop()

    async def on_timeout(self) -> None:
        await self.disable_and_stop()

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: Any) -> None:
        log.warning("View %s on_error: %s", self.kind, error, exc_info=error)
        from utils.errors import format_interaction_error

        msg = format_interaction_error(interaction, error)
        if msg is not None:
            if hasattr(interaction, "response") and not _interaction_is_done(interaction):
                try:
                    await interaction.response.send_message(
                        msg,
                        ephemeral=True,
                        allowed_mentions=_NO_MENTIONS,
                    )
                except Exception as exc:
                    log.debug("Failed sending view error response: %s", exc)
            elif hasattr(interaction, "followup"):
                try:
                    await interaction.followup.send(
                        msg,
                        ephemeral=True,
                        allowed_mentions=_NO_MENTIONS,
                    )
                except Exception as exc:
                    log.debug("Failed sending view error followup: %s", exc)


@dataclass(frozen=True)
class PaginatedPage:
    title: str
    items: list[str]
    current_page: int
    total_pages: int
    extra_header: str | None = None
    empty_message: str | None = None


class PaginatedView(BaseCardView):
    """Generic Components V2 paginated list view (10 items per page max).

    Holds no copy of the underlying data; fetches fresh data on every button press.
    """

    def __init__(
        self,
        items_provider: Callable[[int], Any],
        author_id: int | None = None,
        guild_id: int = 0,
        kind: str = "paginated",
        initial_page: int = 1,
        timeout: float = 120.0,
    ) -> None:
        super().__init__(timeout=timeout, author_id=author_id, guild_id=guild_id, kind=kind)
        self.items_provider: Callable[[int], Any] | None = items_provider
        self.current_page = initial_page
        self.total_pages = 1
        self.message: discord.Message | None = None
        self._prev_btn: Button[Any] | None = None
        self._next_btn: Button[Any] | None = None

    def release_references(self) -> None:
        super().release_references()
        self.items_provider = None
        self.message = None

    async def render(self, page_num: int | None = None) -> Container[Any]:
        if page_num is not None:
            self.current_page = page_num
        if self.items_provider is None:
            container = create_card_container(TextDisplay("**Content Expired**\nThis view has timed out."))
            self.clear_items()
            self.add_item(container)
            return container

        page_result = self.items_provider(self.current_page)
        if asyncio.iscoroutine(page_result):
            page_data = await page_result
        else:
            page_data = page_result

        self.current_page = getattr(page_data, "current_page", 1)
        self.total_pages = max(1, getattr(page_data, "total_pages", 1))

        lines: list[str] = [page_data.title]
        if getattr(page_data, "extra_header", None):
            lines.append(page_data.extra_header)

        items = getattr(page_data, "items", [])
        if not items:
            empty = getattr(page_data, "empty_message", None) or "No items to display."
            lines.append(empty)
        else:
            lines.extend(items[:10])

        lines.append(f"Page {self.current_page} of {self.total_pages}")

        text_display = TextDisplay("\n".join(lines))
        self._prev_btn = secondary_button("<", disabled=(self.current_page <= 1), callback=self._on_prev)
        self._next_btn = secondary_button(">", disabled=(self.current_page >= self.total_pages), callback=self._on_next)
        row = ActionRow(self._prev_btn, self._next_btn)

        self.clear_items()
        container = create_card_container(text_display, row)
        self.add_item(container)
        return container

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        if self.current_page > 1:
            await self.render(self.current_page - 1)
            await interaction.response.edit_message(view=self, allowed_mentions=_NO_MENTIONS)
        else:
            await interaction.response.defer()

    async def _on_next(self, interaction: discord.Interaction) -> None:
        if self.current_page < self.total_pages:
            await self.render(self.current_page + 1)
            await interaction.response.edit_message(view=self, allowed_mentions=_NO_MENTIONS)
        else:
            await interaction.response.defer()

    async def on_timeout(self) -> None:
        await self.disable_and_stop()


def create_info_card(title: str, text: str) -> BaseCardView:
    """Build a standard info card view with a single neutral gray container."""
    content = f"**{title}**\n\n{text}" if not title.startswith("**") else f"{title}\n\n{text}"
    disp = TextDisplay(content)
    container = create_card_container(disp)
    view = BaseCardView()
    view.add_item(container)
    return view


async def reply_card(
    interaction: discord.Interaction,
    view: LayoutView,
    *,
    ephemeral: bool = False,
    kind: str | None = None,
) -> bool:
    """Send a Components V2 card message.

    Components V2 messages cannot carry normal content or embeds.
    """
    if isinstance(view, BaseCardView):
        view.is_ephemeral = ephemeral
        view.owner_id = view.author_id or interaction.user.id
        view.author_id = view.owner_id
        view.guild_id = view.guild_id or (interaction.guild_id or 0)
        if kind:
            view.kind = kind
        view._client = interaction.client
        if ephemeral:
            view.interaction = interaction

    sent_msg: discord.Message | None = None
    try:
        if not interaction.response.is_done():
            await asyncio.wait_for(
                interaction.response.send_message(view=view, ephemeral=ephemeral),
                timeout=CARD_SEND_TIMEOUT,
            )
            if not ephemeral:
                try:
                    sent_msg = await interaction.original_response()
                except Exception as exc:
                    log.debug("Failed to store message on card view: %s", exc)
        else:
            # If already deferred:
            try:
                sent_msg = await asyncio.wait_for(
                    interaction.edit_original_response(view=view),
                    timeout=CARD_SEND_TIMEOUT,
                )
            except (discord.NotFound, discord.HTTPException, asyncio.TimeoutError):
                sent_msg = await asyncio.wait_for(
                    interaction.followup.send(view=view, ephemeral=ephemeral),
                    timeout=CARD_SEND_TIMEOUT,
                )

        if isinstance(view, BaseCardView):
            if sent_msg is not None:
                view.channel_id = getattr(getattr(sent_msg, "channel", None), "id", None)
                view.message_id = getattr(sent_msg, "id", None)
                if isinstance(view, PaginatedView):
                    view.message = sent_msg
            await ACTIVE_VIEWS.register(view)

        return True
    except discord.NotFound:
        log.info("Interaction token expired; card reply dropped")
    except discord.Forbidden:
        log.info("Missing permission to reply with a card")
    except (discord.HTTPException, asyncio.TimeoutError) as exc:
        log.warning("Failed to deliver interaction card reply: %s", exc)
    return False


# -------------------------------------------------------------- Now Playing Card


def escape_link_title(title: str, max_len: int = 80) -> str:
    """Escape markdown special characters in a title for use in markdown links.

    Truncates to max_len characters with '...' if needed.
    """
    cleaned = " ".join((title or "").split())
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 3] + "..."
    escaped = discord.utils.escape_markdown(cleaned)
    escaped = escaped.replace("[", r"\[").replace("]", r"\]")
    return escaped


def build_nowplaying_action_row(
    *,
    is_paused: bool = False,
    has_history: bool = False,
    loop_callback: Any | None = None,
    prev_callback: Any | None = None,
    pause_resume_callback: Any | None = None,
    skip_callback: Any | None = None,
    stop_callback: Any | None = None,
) -> ActionRow:
    """Build the ActionRow with the 5 now playing buttons in exact order:
    1. Loop (emoji loop, secondary)
    2. Previous (emoji previous, secondary, disabled if not has_history)
    3. Pause/Resume (shows pause emoji while playing, resume emoji while paused, secondary)
    4. Skip (emoji skip, secondary)
    5. Stop (emoji stop, ButtonStyle.danger)
    """
    emojis = load_emojis()

    # 1. Loop
    loop_emoji = emojis.get("loop")
    btn_loop = Button(
        style=ButtonStyle.secondary,
        emoji=loop_emoji,
        label=None if loop_emoji else "Loop",
        custom_id="np:loop",
    )
    if loop_callback is not None:
        btn_loop.callback = loop_callback

    # 2. Previous
    prev_emoji = emojis.get("previous")
    btn_prev = Button(
        style=ButtonStyle.secondary,
        emoji=prev_emoji,
        label=None if prev_emoji else "Previous",
        disabled=not has_history,
        custom_id="np:previous",
    )
    if prev_callback is not None:
        btn_prev.callback = prev_callback

    # 3. Pause/Resume
    if is_paused:
        pr_emoji = emojis.get("resume")
        pr_label = None if pr_emoji else "Resume"
    else:
        pr_emoji = emojis.get("pause")
        pr_label = None if pr_emoji else "Pause"
    btn_pause_resume = Button(
        style=ButtonStyle.secondary,
        emoji=pr_emoji,
        label=pr_label,
        custom_id="np:pause_resume",
    )
    if pause_resume_callback is not None:
        btn_pause_resume.callback = pause_resume_callback

    # 4. Skip
    skip_emoji = emojis.get("skip")
    btn_skip = Button(
        style=ButtonStyle.secondary,
        emoji=skip_emoji,
        label=None if skip_emoji else "Skip",
        custom_id="np:skip",
    )
    if skip_callback is not None:
        btn_skip.callback = skip_callback

    # 5. Stop (stop button allows danger style)
    stop_emoji = emojis.get("stop")
    btn_stop = Button(
        style=ButtonStyle.danger,  # stop button
        emoji=stop_emoji,
        label=None if stop_emoji else "Stop",
        custom_id="np:stop",
    )
    if stop_callback is not None:
        btn_stop.callback = stop_callback

    return ActionRow(btn_loop, btn_prev, btn_pause_resume, btn_skip, btn_stop)


def build_nowplaying_container(
    bot_name: str = "Melora",
    title: str = "Unknown title",
    uri: str = "",
    requester_id: int = 0,
    duration_str: str = "0:00",
    requester_avatar_url: str = "",
    action_row: ActionRow | None = None,
    *,
    is_paused: bool = False,
    has_history: bool = False,
    artwork_url: str | None = None,
    **kwargs: Any,
) -> Container[Any]:
    """Build the Components V2 container for the now playing card.

    Layout (one Container, neutral gray accent, no divider component):
      A Section whose accessory is a Thumbnail of the song artwork.
      The Section holds two text displays:
        1. The bot's display name in bold (`**{bot_name}**`).
        2. Three lines:
           ### [Song Title](track uri)
           **Requested by:** <@requester_id>
           **Duration:** `3:42`
      An ActionRow with 5 buttons in exact order:
        Loop, Previous, Pause/Resume, Skip, Stop (danger).
    """
    art_url = (
        artwork_url
        or kwargs.get("artwork_url")
        or kwargs.get("bot_avatar_url")
        or "https://cdn.discordapp.com/embed/avatars/0.png"
    ).strip()
    if not art_url:
        art_url = "https://cdn.discordapp.com/embed/avatars/0.png"

    if action_row is None:
        action_row = build_nowplaying_action_row(is_paused=is_paused, has_history=has_history)

    escaped_title = escape_link_title(title, max_len=80)
    uri_clean = (uri or "").strip() or "https://discord.com"
    content_lines = [
        f"### [{escaped_title}]({uri_clean})",
        f"**Requested by:** <@{requester_id}>",
        f"**Duration:** `{duration_str}`",
    ]
    section = Section(
        TextDisplay(f"**{bot_name}**"),
        TextDisplay("\n".join(content_lines)),
        accessory=Thumbnail(art_url),
    )
    return create_card_container(section, action_row)


class NowPlayingView(LayoutView):
    """Persistent Components V2 view for the now playing card.

    Buttons: Loop, Previous, Pause/Resume, Skip, Stop.
    Only Stop uses ButtonStyle.danger; all others use ButtonStyle.secondary.
    timeout is None so the view lives as long as the player.
    The view must be stopped explicitly via stop() on player destroy.
    """

    def __init__(self, player_ref: Any) -> None:
        super().__init__(timeout=None)
        self._player_ref = player_ref
        self.container: Container[Any] | None = None
        if getattr(player_ref, "current", None) is not None:
            self.render()

    async def _scheduled_task(self, item: Any, interaction: discord.Interaction) -> None:
        """Silent per-message button guard: while one press is running, quietly defer subsequent presses."""
        msg = getattr(interaction, "message", None)
        msg_id = getattr(msg, "id", None) if msg else None
        if msg_id is not None:
            if msg_id in _IN_FLIGHT_MESSAGES:
                if hasattr(interaction, "response") and not _interaction_is_done(interaction):
                    try:
                        await interaction.response.defer()
                    except Exception as defer_exc:
                        log.debug("Silent defer failed: %s", defer_exc)
                return
            _IN_FLIGHT_MESSAGES.add(msg_id)
        try:
            await super()._scheduled_task(item, interaction)
        finally:
            if msg_id is not None:
                _IN_FLIGHT_MESSAGES.discard(msg_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        client = interaction.client
        rate_limiter = getattr(client, "rate_limiter", None)
        cfg = getattr(client, "cfg", None)
        owner_id = getattr(cfg, "owner_id", 0) if cfg else 0

        if rate_limiter is not None:
            allowed, retry_after = rate_limiter.acquire_component(interaction.user.id, owner_id)
            if not allowed:
                sec = max(1, math.ceil(retry_after))
                if not interaction.response.is_done():
                    try:
                        await interaction.response.send_message(
                            messages.rate_limited(sec),
                            ephemeral=True,
                            allowed_mentions=_NO_MENTIONS,
                        )
                    except Exception as exc:
                        log.debug("Failed sending nowplaying component rate limit: %s", exc)
                return False
        return True

    def _get_player(self) -> Any:
        return self._player_ref

    def render(self, *, is_paused: bool | None = None, has_history: bool | None = None) -> Container[Any]:
        """Rebuild container and buttons to reflect the real player state."""
        player = self._get_player()
        if player is None or getattr(player, "destroyed", False):
            dummy = create_card_container(TextDisplay("**Melora**"))
            self.container = dummy
            return dummy

        guild_id = player.guild_id
        backend = getattr(player.services, "backend", None)
        bot_name = backend.bot_display_name(guild_id) if backend and hasattr(backend, "bot_display_name") else "Melora"
        bot_avatar = (
            backend.bot_avatar_url() if backend and hasattr(backend, "bot_avatar_url") else None
        ) or "https://cdn.discordapp.com/embed/avatars/0.png"
        bot_id = backend.bot_id() if backend and hasattr(backend, "bot_id") else 0

        item = player.current
        if item is not None:
            title = item.title
            uri = item.uri
            req_id = item.requester_id or bot_id
            dur_str = "LIVE" if item.is_stream else format_duration(item.duration_ms)
            artwork_url = getattr(item, "artwork_url", None)
        else:
            title = "Nothing playing"
            uri = "https://discord.com"
            req_id = bot_id
            dur_str = "0:00"
            artwork_url = None

        paused = player.paused if is_paused is None else is_paused
        if has_history is not None:
            history = has_history
        else:
            q = getattr(player, "queue", None)
            history = bool(getattr(q, "has_history", False) or (getattr(q, "history_len", lambda: 0)() > 0))

        action_row = build_nowplaying_action_row(
            is_paused=paused,
            has_history=history,
            loop_callback=self._on_loop,
            prev_callback=self._on_previous,
            pause_resume_callback=self._on_pause_resume,
            skip_callback=self._on_skip,
            stop_callback=self._on_stop,
        )

        container = build_nowplaying_container(
            bot_name=bot_name,
            title=title,
            uri=uri,
            requester_id=req_id,
            duration_str=dur_str,
            action_row=action_row,
            is_paused=paused,
            has_history=history,
            artwork_url=artwork_url or bot_avatar,
        )
        self.clear_items()
        self.add_item(container)
        self.container = container
        return container

    async def _check_gate(self, interaction: discord.Interaction, player: Any, *, dj_required: bool = False) -> bool:
        """Verify user is in voice channel and DJ rules (Pattern B on failure)."""
        if player is None or getattr(player, "destroyed", False):
            await interaction.response.send_message(
                messages.nothing_playing(), ephemeral=True, allowed_mentions=_NO_MENTIONS
            )
            return False

        member = interaction.user
        voice = getattr(member, "voice", None)
        channel = getattr(voice, "channel", None)
        if voice is None or channel is None:
            await interaction.response.send_message(
                messages.not_in_voice(), ephemeral=True, allowed_mentions=_NO_MENTIONS
            )
            return False

        if channel.id != player.voice_channel_id:
            await interaction.response.send_message(
                messages.different_voice_channel(), ephemeral=True, allowed_mentions=_NO_MENTIONS
            )
            return False

        # DJ check
        cfg = getattr(player, "cfg", None)
        storage = getattr(getattr(player, "services", None), "storage", None)
        dj_only = False
        dj_role_id = getattr(cfg, "dj_role_id", 0) if cfg else 0
        if storage is not None:
            try:
                settings = await storage.get_guild_settings(player.guild_id)
                if getattr(settings, "dj_role_id", 0):
                    dj_role_id = settings.dj_role_id
                dj_only = getattr(settings, "dj_only", False)
            except Exception as exc:
                log.debug("guild=%s failed checking dj settings: %s", player.guild_id, exc)

        if dj_required or dj_only:
            is_admin = hasattr(member, "guild_permissions") and member.guild_permissions.administrator
            is_owner = member.id == getattr(cfg, "owner_id", 0) if cfg else False
            has_role = False
            if dj_role_id and hasattr(member, "roles"):
                has_role = any(r.id == dj_role_id for r in member.roles)
            if dj_role_id or dj_only:
                if not (is_admin or is_owner or has_role):
                    await interaction.response.send_message(
                        messages.dj_required(), ephemeral=True, allowed_mentions=_NO_MENTIONS
                    )
                    return False

        return True

    async def _on_loop(self, interaction: discord.Interaction) -> None:
        player = self._get_player()
        if not await self._check_gate(interaction, player):
            return
        from core.queue import LoopMode

        curr = player.queue.loop
        if curr == LoopMode.OFF:
            nxt = LoopMode.TRACK
            mode_name = "Track"
        elif curr == LoopMode.TRACK:
            nxt = LoopMode.QUEUE
            mode_name = "Queue"
        else:
            nxt = LoopMode.OFF
            mode_name = "Off"
        player.set_loop(nxt)
        await interaction.response.send_message(
            messages.loop_mode_set(mode_name), ephemeral=True, allowed_mentions=_NO_MENTIONS
        )

    async def _on_previous(self, interaction: discord.Interaction) -> None:
        player = self._get_player()
        if not await self._check_gate(interaction, player):
            return
        if not player.queue.has_history:
            await interaction.response.send_message(
                messages.history_empty(), ephemeral=True, allowed_mentions=_NO_MENTIONS
            )
            return
        await interaction.response.defer()
        try:
            await player.previous()
        except Exception as exc:
            log.debug("Previous button error: %s", exc)

    async def _on_pause_resume(self, interaction: discord.Interaction) -> None:
        player = self._get_player()
        if not await self._check_gate(interaction, player):
            return
        try:
            if player.paused:
                await player.resume()
            else:
                await player.pause()
            # Update card in place by editing the message the button was pressed on
            self.render(is_paused=player.paused, has_history=player.queue.has_history)
            import time

            player._last_card_edit = time.monotonic()
            if getattr(player, "_card_coalesce_task", None) is not None and not player._card_coalesce_task.done():
                player._card_coalesce_task.cancel()
                player._card_coalesce_task = None
            await interaction.response.edit_message(view=self, allowed_mentions=_NO_MENTIONS)
        except Exception as exc:
            log.debug("Pause/Resume button error: %s", exc)
            if not interaction.response.is_done():
                await interaction.response.defer()

    async def _on_skip(self, interaction: discord.Interaction) -> None:
        player = self._get_player()
        if not await self._check_gate(interaction, player):
            return
        member = interaction.user
        cfg = player.cfg
        is_requester = player.current is not None and player.current.requester_id == member.id
        privileged = (
            hasattr(member, "guild_permissions") and member.guild_permissions.administrator
        ) or (member.id == getattr(cfg, "owner_id", 0))
        has_dj = False
        if getattr(cfg, "dj_role_id", 0) and hasattr(member, "roles"):
            has_dj = any(r.id == cfg.dj_role_id for r in member.roles)

        humans = 1
        backend = getattr(player.services, "backend", None)
        if backend is not None:
            h = backend.humans_in_channel(player.guild_id, player.voice_channel_id)
            if h is not None:
                humans = h

        if (
            is_requester
            or privileged
            or has_dj
            or not getattr(cfg, "vote_skip_enabled", True)
            or humans < getattr(cfg, "vote_skip_min_listeners", 1)
        ):
            await interaction.response.defer()
            try:
                await player.skip()
            except Exception as exc:
                log.debug("Skip button error: %s", exc)
        else:
            try:
                skipped, votes, needed = await player.vote_skip(member.id, humans)
                if skipped:
                    await interaction.response.defer()
                else:
                    await interaction.response.send_message(
                        messages.vote_skip_registered(votes, needed),
                        ephemeral=True,
                        allowed_mentions=_NO_MENTIONS,
                    )
            except Exception as exc:
                log.debug("Vote skip error: %s", exc)
                if not interaction.response.is_done():
                    await interaction.response.defer()

    async def _on_stop(self, interaction: discord.Interaction) -> None:
        player = self._get_player()
        if not await self._check_gate(interaction, player, dj_required=True):
            return
        await interaction.response.defer()
        try:
            await player.stop()
        except Exception as exc:
            log.debug("Stop button error: %s", exc)

