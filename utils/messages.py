"""Centralized catalog of all user-facing strings and message builders.

Rules enforced:
- Pattern A, actions and info, ONE line:
  {Action} **{Subject}** • `{Detail}` (the detail part is optional)
- Pattern B, errors and denials, ONE line:
  **{Problem}** • `{What to do}`
- Pattern C, label and value lines inside cards:
  **Label:** `value` (mentions are never put in backticks)
- Subjects (track titles, playlist names, channel names) are bold, escaped with
  discord.utils.escape_markdown, and truncated to 60 characters with "...".
- Durations are m:ss or h:mm:ss inside backticks. Counts, positions, and short
  hints also go in backticks.
- No exclamation marks, no filler words, no extra lines. Multi-item progress
  may use two lines at most.
- ASCII plus bullet (U+2022) only.
"""
from __future__ import annotations

import discord

from utils.text import clean

BULLET = "•"


def escape_subject(text: object, max_len: int = 60) -> str:
    cleaned = clean(text)
    if not cleaned:
        return ""
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 3].rstrip() + "..."
    return discord.utils.escape_markdown(cleaned)


# ------------------------------------------------------------------ Playback
def added_to_queue(
    title: str,
    url: str | None = None,
    artist: str | None = None,
    position: int = 0,
    duration_str: str | None = None,
) -> str:
    subj = escape_subject(title) or "Track"
    if duration_str:
        return f"Added **{subj}** • `{duration_str}`"
    if position > 0:
        return f"Added **{subj}** • `#{position}`"
    return f"Added **{subj}**"


def single_track_added(
    title: str,
    position: int | None = None,
    duration_str: str | None = None,
) -> str:
    subj = escape_subject(title) or "Track"
    if duration_str:
        return f"Added **{subj}** • `{duration_str}`"
    if position is not None and position > 0:
        return f"Added **{subj}** • `#{position}`"
    return f"Added **{subj}**"


def added_playlist(count: int, total: int | None = None) -> str:
    if total is not None and total != count:
        return f"Added **{count} of {total} tracks** • `Spotify limit`"
    return f"Added **{count} tracks**"


def collection_tracks_added(
    name: str,
    count: int,
    total: int,
    skipped: int = 0,
) -> str:
    subj = escape_subject(name, 40) or "collection"
    if total > 0 and count != total:
        line1 = f"Added **{count} of {total} tracks** • `{subj}`"
        if skipped > 0:
            return f"{line1}\nSkipped **{skipped} tracks** • `Limit reached`"
        return line1
    if skipped > 0:
        return f"Added **{count} tracks** • `{subj}`\nSkipped **{skipped} tracks** • `Limit reached`"
    return f"Added **{count} tracks** • `{subj}`"


def playing_next(
    title: str,
    url: str | None = None,
    artist: str | None = None,
    duration_str: str | None = None,
) -> str:
    subj = escape_subject(title) or "Track"
    if duration_str:
        return f"Playing next **{subj}** • `{duration_str}`"
    return f"Playing next **{subj}**"


def playing_instant(title: str, url: str | None = None, artist: str | None = None) -> str:
    subj = escape_subject(title) or "Track"
    return f"Playing now **{subj}**"


def now_playing(title: str) -> str:
    subj = escape_subject(title) or "Track"
    return f"Playing now **{subj}**"


def paused(title: str | None = None) -> str:
    if title:
        return f"Paused **{escape_subject(title)}**"
    return "Paused **Playback**"


def resumed(title: str | None = None) -> str:
    if title:
        return f"Resumed **{escape_subject(title)}**"
    return "Resumed **Playback**"


def stopped() -> str:
    return "Stopped playback • `Queue cleared`"


def left_channel(channel: str = "Voice Channel") -> str:
    subj = escape_subject(channel) or "Voice Channel"
    return f"Left **{subj}**"


def skipped(title: str | None = None) -> str:
    if title:
        return f"Skipped **{escape_subject(title)}**"
    return "Skipped **Track**"


def vote_skip_registered(current: int, needed: int) -> str:
    return f"Vote added • `{current}/{needed}`"


def vote_skip_passed() -> str:
    return "Vote passed • `Skipping track`"


def previous_track(title: str) -> str:
    subj = escape_subject(title) or "Track"
    return f"Playing previous **{subj}**"


def replayed(title: str) -> str:
    subj = escape_subject(title) or "Track"
    return f"Replaying **{subj}**"


def seeked(position_str: str, title: str = "Track") -> str:
    subj = escape_subject(title) or "Track"
    return f"Moved to **{position_str}** • `{subj}`"


def forwarded(seconds: int, new_pos_str: str) -> str:
    return f"Moved to **{new_pos_str}** • `+{seconds}s`"


def rewound(seconds: int, new_pos_str: str) -> str:
    return f"Moved to **{new_pos_str}** • `-{seconds}s`"


def skipto(position: int, title: str) -> str:
    subj = escape_subject(title) or "Track"
    return f"Skipped to **#{position}** • `{subj}`"


def skipto_result(position: int, title: str, dropped_count: int = 0) -> str:
    subj = escape_subject(title) or "Track"
    return f"Skipped to **#{position}** • `{subj}`"


def sleep_set(minutes: int) -> str:
    return f"Sleep timer set • `{minutes} min`"


def sleep_cancelled() -> str:
    return "Sleep timer cancelled • `Normal playback`"


def sleep_finished() -> str:
    return "Left **Voice Channel** • `Sleep timer ended`"


def autoplay_toggled(enabled: bool) -> str:
    state = "enabled" if enabled else "disabled"
    return f"Autoplay **{state}**"


def similar_added(count: int) -> str:
    return f"Added **{count} tracks** • `Similar music`"


def similar_not_found() -> str:
    return "**No similar tracks found** • `Try a different search`"


# --------------------------------------------------------------------- Queue
def queue_cleared() -> str:
    return "Cleared **Queue** • `All tracks removed`"


def queue_cleared_count(count: int) -> str:
    return f"Cleared **{count} tracks**"


def shuffled(count: int) -> str:
    return f"Shuffled **{count} tracks**"


def loop_mode_set(mode: str) -> str:
    formatted = escape_subject(mode.capitalize())
    return f"Loop set to **{formatted}**"


def unknown_loop_mode() -> str:
    return "**Invalid loop mode** • `Choose off, track, or queue`"


def track_moved(title: str, from_pos: int, to_pos: int) -> str:
    subj = escape_subject(title) or "Track"
    return f"Moved **{subj}** • `{from_pos} to {to_pos}`"


def track_swapped(title1: str, pos1: int, title2: str, pos2: int) -> str:
    subj1 = escape_subject(title1) or "Track A"
    subj2 = escape_subject(title2) or "Track B"
    return f"Swapped **{subj1}** and **{subj2}**"


def deduped(removed_count: int) -> str:
    if removed_count > 0:
        return f"Removed **{removed_count} duplicates**"
    return "Removed **0 duplicates** • `Queue is unique`"


def queue_saved(name: str, count: int) -> str:
    subj = escape_subject(name) or "Queue"
    return f"Created playlist **{subj}** • `{count} tracks`"


def queue_save_failed(reason: str) -> str:
    detail = escape_subject(reason, 40) or "Database error"
    return f"**Could not save queue** • `{detail}`"


def track_removed(title: str, count: int = 1) -> str:
    subj = escape_subject(title) or "Track"
    if count > 1:
        return f"Removed **{count} tracks** • `{subj}`"
    return f"Removed **{subj}**"


def track_inserted(title: str, position: int) -> str:
    subj = escape_subject(title) or "Track"
    return f"Moved **{subj}** • `Position {position}`"


def queue_empty() -> str:
    return "**Queue is empty** • `Use /play to add tracks`"


# -------------------------------------------------------------------- Errors
def not_in_guild() -> str:
    return "**Server required** • `Run this command in a server`"


def not_in_voice() -> str:
    return "**Not in a voice channel** • `Join one and try again`"


def wrong_channel(channel_id: int | None = None) -> str:
    if channel_id:
        return f"**Wrong voice channel** • `Join <#{channel_id}> and try again`"
    return "**Wrong voice channel** • `Join the bot voice channel and try again`"


def nothing_playing() -> str:
    return "**Nothing is playing** • `Use /play to start`"


def queue_full(max_size: int | None = None) -> str:
    if max_size:
        return f"**Queue is full** • `Limit is {max_size} tracks`"
    return "**Queue is full** • `Remove some tracks and try again`"


def user_limit_reached(limit: int) -> str:
    return f"**Track limit reached** • `Limit is {limit} tracks per user`"


def track_too_long(max_minutes: int | None = None) -> str:
    if max_minutes:
        return f"**Track too long** • `Maximum is {max_minutes} min`"
    return "**Track too long** • `Choose a shorter track`"


def stream_not_seekable() -> str:
    return "**Cannot seek stream** • `Live streams do not support seeking`"


def stage_unsupported() -> str:
    return "**Stage unsupported** • `Join a standard voice channel`"


def missing_voice_permissions() -> str:
    return "**Missing voice permissions** • `Connect and Speak required`"


def dj_required() -> str:
    return "**DJ role required** • `Ask an admin for the DJ role`"


def owner_only() -> str:
    return "**Owner only** • `Only the bot owner can use this`"


def cooldown_hit(seconds: int) -> str:
    sec = max(1, seconds)
    return f"**On cooldown** • `Try again in {sec}s`"


def no_matches() -> str:
    return "**No results found** • `Try a different search`"


def load_failed() -> str:
    return "**Load failed** • `Check the source link or query`"


def node_offline() -> str:
    return "**Music server offline** • `Try again soon`"


def node_disconnected() -> str:
    return "**Music server offline** • `Try again soon`"


def voice_connect_failed() -> str:
    return "**Voice connection failed** • `Check permissions and try again`"


def voice_disconnected() -> str:
    return "Left **Voice Channel** • `Disconnected`"


def storage_unavailable() -> str:
    return "**Storage unavailable** • `This feature is down for now`"


def channel_restricted(channel_id: int) -> str:
    return f"**Command restricted** • `Use commands in <#{channel_id}>`"


def circuit_breaker_tripped() -> str:
    return "Stopped playback • `Too many failures`"


def circuit_breaker_tripped_count(count: int) -> str:
    return f"Stopped playback • `{count} consecutive failures`"


def track_failed_notice(title: str, reason: str | None = None) -> str:
    subj = escape_subject(title) or "Track"
    if reason:
        detail = escape_subject(reason, 40) or "Playback error"
        return f"Skipped **{subj}** • `{detail}`"
    return f"Skipped **{subj}** • `Playback error`"


def unhandled_error(error_id: str) -> str:
    return f"**Unexpected error** • `Reference ID {error_id}`"


def invalid_time_format() -> str:
    return "**Invalid time format** • `Use mm:ss or seconds`"


def author_only_controls() -> str:
    return "**Access denied** • `Only the command author can use this`"


def requester_only_pick() -> str:
    return "**Access denied** • `Only the requester can pick a track`"


def track_already_chosen() -> str:
    return "**Selection complete** • `Track was already chosen`"


def reset_author_only() -> str:
    return "**Access denied** • `Only the command author can confirm`"


def spotify_disabled() -> str:
    return "**Spotify disabled** • `Spotify support is inactive`"


def spotify_track_not_found() -> str:
    return "**Track not found** • `Check Spotify link and try again`"


def spotify_album_not_found() -> str:
    return "**Album not found** • `Check Spotify link and try again`"


def spotify_playlist_not_found() -> str:
    return "**Playlist not found** • `Check Spotify link and try again`"


def collection_all_tracks_too_long(kind: str = "playlist") -> str:
    return f"**Tracks too long** • `All tracks in this {kind} exceed limit`"


def playback_start_failed() -> str:
    return "**Playback failed** • `Try again in a moment`"


def spotify_match_failed(title: str) -> str:
    subj = escape_subject(title) or "Track"
    return f"Skipped **{subj}** • `No match found`"


def missing_permissions() -> str:
    return "**Missing permission** • `You lack required permissions`"


def bot_forbidden() -> str:
    return "**Bot missing permission** • `Grant the bot access to this channel`"


def bot_missing_permission() -> str:
    return bot_forbidden()


def bot_missing_permissions(perms: str) -> str:
    return f"**Missing permission** • `Grant {perms}`"


def check_failure() -> str:
    return "**Permission denied** • `You cannot run this command`"


def manage_server_required() -> str:
    return "**Missing permission** • `Manage Server required`"


def invalid_input() -> str:
    return "**Invalid input** • `Check your command parameters`"


def sync_failed() -> str:
    return "**Sync failed** • `Check server logs and try again`"


def filter_not_active(name: str) -> str:
    subj = escape_subject(name) or "Filter"
    return f"**Filter not active** • `{subj} is not active`"


def speed_set(speed: float) -> str:
    return f"Playback speed set to **{speed:.2f}x** • `Smooth playback`"


def speed_reset() -> str:
    return "Playback speed reset to **1.00x** • `Normal speed`"


# ------------------------------------------------------------------- Library
def favorite_added(title: str) -> str:
    subj = escape_subject(title) or "Track"
    return f"Added **{subj}** to favorites"


def favorite_removed(title: str) -> str:
    subj = escape_subject(title) or "Track"
    return f"Removed **{subj}** from favorites"


def favorites_cleared() -> str:
    return "Cleared **Favorites** • `All favorites removed`"


def favorites_empty() -> str:
    return "**No favorites saved** • `Use /favorites add to save tracks`"


def favorites_limit(limit: int) -> str:
    return f"**Favorites limit reached** • `Limit is {limit} tracks`"


def favorites_queued(added: int, skipped: int = 0) -> str:
    if skipped > 0:
        return f"Added **{added} tracks** • `From favorites`\nSkipped **{skipped} tracks** • `Limit reached`"
    return f"Added **{added} tracks** • `From favorites`"


def favorite_position_removed(position: int) -> str:
    return f"Removed favorite • `#{position}`"


def favorite_not_found(position: int) -> str:
    return f"**Favorite not found** • `No track at #{position}`"


def playlist_created(name: str) -> str:
    subj = escape_subject(name) or "Playlist"
    return f"Created playlist **{subj}**"


def playlist_deleted(name: str) -> str:
    subj = escape_subject(name) or "Playlist"
    return f"Removed playlist **{subj}**"


def playlist_renamed(old_name: str, new_name: str) -> str:
    old_subj = escape_subject(old_name) or "Playlist"
    new_subj = escape_subject(new_name) or "Playlist"
    return f"Renamed playlist **{old_subj}** • `To {new_subj}`"


def playlist_track_added(playlist_name: str, track_title: str) -> str:
    pl = escape_subject(playlist_name) or "Playlist"
    tr = escape_subject(track_title) or "Track"
    return f"Added **{tr}** • `{pl}`"


def playlist_track_removed(playlist_name: str, track_title: str) -> str:
    pl = escape_subject(playlist_name) or "Playlist"
    tr = escape_subject(track_title) or "Track"
    return f"Removed **{tr}** • `{pl}`"


def playlist_track_removed_index(name: str, index: int) -> str:
    pl = escape_subject(name) or "Playlist"
    return f"Removed track • `#{index} from {pl}`"


def playlist_not_found(name: str) -> str:
    subj = escape_subject(name) or "Playlist"
    return f"**Playlist not found** • `No playlist named {subj}`"


def playlist_empty(name: str) -> str:
    subj = escape_subject(name) or "Playlist"
    return f"**Playlist is empty** • `{subj} has no tracks`"


def playlist_empty_or_missing(name: str) -> str:
    subj = escape_subject(name) or "Playlist"
    return f"**Playlist unavailable** • `{subj} is empty or missing`"


def playlist_limit(limit: int) -> str:
    return f"**Playlist limit reached** • `Limit is {limit} playlists`"


def playlist_tracks_limit(limit: int) -> str:
    return f"**Playlist capacity reached** • `Limit is {limit} tracks`"


def playlists_empty_list() -> str:
    return "**No playlists found** • `Use /playlist create to start one`"


def playlist_queued(name: str, added: int, skipped: int = 0) -> str:
    subj = escape_subject(name, 30) or "playlist"
    if skipped > 0:
        return f"Added **{added} tracks** • `{subj}`\nSkipped **{skipped} tracks** • `Limit reached`"
    return f"Added **{added} tracks** • `{subj}`"


def playlist_index_not_found(name: str, index: int) -> str:
    subj = escape_subject(name) or "Playlist"
    return f"**Track not found** • `No track at #{index} in {subj}`"


# ------------------------------------------------------------------ Settings
def settings_updated(setting_name: str, value: str) -> str:
    return f"Set {setting_name} to **{value}**"


def dj_role_set(role_id: int | None) -> str:
    if role_id:
        return f"Set DJ role to <@&{role_id}>"
    return "Cleared **DJ role** • `Restriction removed`"


def dj_only_toggled(enabled: bool) -> str:
    state = "enabled" if enabled else "disabled"
    return f"DJ-only mode **{state}**"


def default_volume_set(volume: int) -> str:
    return f"Set volume to **{volume}%** • `Default`"


def volume_limit_set(volume: int) -> str:
    return f"Set volume limit to **{volume}%**"


def volume_updated(level: int, limit: int | None = None) -> str:
    if limit is not None:
        return f"Set volume to **{level}%** • `Limit {limit}%`"
    return f"Set volume to **{level}%**"


def max_duration_set(minutes: int) -> str:
    return f"Set max duration to **{minutes} min**"


def max_queue_set(count: int) -> str:
    return f"Set max queue to **{count} tracks**"


def restrict_channel_set(channel_id: int | None) -> str:
    if channel_id:
        return f"Restricted commands to <#{channel_id}>"
    return "Cleared **Channel restriction** • `All channels allowed`"


def restore_queue_toggled(enabled: bool) -> str:
    state = "enabled" if enabled else "disabled"
    return f"Restore queue **{state}**"


def mode_247_toggled(enabled: bool, channel_id: int | None = None) -> str:
    if enabled and channel_id:
        return f"24/7 mode **enabled** • `<#{channel_id}>`"
    if enabled:
        return "24/7 mode **enabled**"
    return "24/7 mode **disabled**"


def server_setting_row(label: str, value: str, is_mention: bool = False) -> str:
    """Pattern C: **Label:** `value` (mentions are never put in backticks)."""
    if is_mention:
        return f"**{label}:** {value}"
    return f"**{label}:** `{value}`"


# ------------------------------------------------------------------- Filters
def filter_added(name: str) -> str:
    subj = escape_subject(name) or "Filter"
    return f"Applied filter **{subj}**"


def filter_removed(name: str) -> str:
    subj = escape_subject(name) or "Filter"
    return f"Removed filter **{subj}**"


def filters_reset() -> str:
    return "Reset **Audio filters** • `All filters cleared`"


def eq_preset_applied(name: str) -> str:
    subj = escape_subject(name) or "Preset"
    return f"Applied EQ preset **{subj}**"


def eq_reset() -> str:
    return "Reset **Equalizer** • `Bands flat`"


def unknown_preset(name: str, available: list[str]) -> str:
    opts = ", ".join(sorted(available)) if available else "none"
    return f"**Unknown preset** • `Available: {opts}`"


# ------------------------------------------------------------- Admin / Misc
def commands_synced(count: int) -> str:
    return f"Synced **{count} commands**"


def privacy_policy() -> str:
    return "Privacy policy • `Melora stores settings and playlists only`"


def reset_prompt() -> str:
    return "**Delete personal data** • `Confirm or cancel below`"


def reset_confirmed() -> str:
    return "Deleted personal data • `Favorites and playlists removed`"


def reset_cancelled() -> str:
    return "Cancelled **Data reset** • `No data was modified`"


def nowplaying_moved() -> str:
    return "Moved **Player card** • `To this channel`"


def mention_reply_card() -> str:
    return "Melora music bot • `Use /help to view commands`"


def idle_leave(channel: str = "Voice Channel") -> str:
    subj = escape_subject(channel) or "Voice Channel"
    return f"Left **{subj}** • `Idle timeout`"


def alone_leave(channel: str = "Voice Channel") -> str:
    subj = escape_subject(channel) or "Voice Channel"
    return f"Left **{subj}** • `Channel empty`"


# ------------------------------------------------------------- Rate Limits & Lifecycle
def rate_limited(seconds: int = 3) -> str:
    sec = max(1, int(seconds))
    return f"**Rate limited** • `Try again in {sec}s`"


def menu_expired() -> str:
    return "**This menu expired** • `Run the command again`"


def not_your_menu() -> str:
    return "**Not your menu** • `Run the command yourself`"


def command_outdated() -> str:
    return "**Command outdated** • `Try again in a moment`"


def server_busy() -> str:
    return "**Server is busy** • `Try again in a few seconds`"


def took_too_long() -> str:
    return "**Took too long** • `Try again`"

