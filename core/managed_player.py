"""The Lavalink player class used by this bot."""
from __future__ import annotations

from lavalink.events import Event
from lavalink.player import DefaultPlayer


class ManagedPlayer(DefaultPlayer):
    """DefaultPlayer without its built-in auto-advance.

    DefaultPlayer.handle_event starts the next track from lavalink.py's own
    queue when a track ends or gets stuck, and stops the player when that
    queue is empty. This bot keeps its queue in GuildPlayer, so that
    behaviour would race with ours. DefaultPlayer.handle_event does nothing
    else, so it is safe to replace it with a no-op. Events still reach the
    registered event hooks, where GuildPlayer decides what to play next.

    Everything else is inherited: position tracking, volume, pause, and the
    node failover logic in change_node that resumes the current track from
    its last known position on another node.
    """

    async def handle_event(self, event: Event) -> None:
        return None
