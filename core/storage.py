"""Thread-safe SQLite storage using a single dedicated worker thread.

All queries are parameterized and executed sequentially on one worker thread.
The event loop never blocks on database I/O.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from utils.autocomplete import UserHistoryEntry
from utils.cache import TTLCache

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "bot.db"
STORAGE_QUEUE_LIMIT = 10_000


@dataclass(slots=True)
class _StorageOp:
    func: Callable[[sqlite3.Connection], Any]
    future: asyncio.Future[Any]
    loop: asyncio.AbstractEventLoop
    is_write: bool = False
    coalesce_key: tuple[str, Any] | None = None


class StorageError(Exception):
    """Raised when a storage operation fails."""


class StorageUnavailable(StorageError):
    """Raised when database access is offline or failed."""


@dataclass(frozen=True, slots=True)
class StoredTrack:
    uri: str
    title: str
    artist: str
    duration_ms: int
    requester_id: int = 0
    position_ms: int = 0


@dataclass(frozen=True)
class GuildSettings:
    guild_id: int
    voice_247_channel_id: int = 0
    dj_role_id: int = 0
    dj_only: bool = False
    volume_limit: int = 100
    max_duration: int = 0
    max_queue: int = 0
    restrict_channel_id: int = 0
    restore_queue: bool = False
    autoplay: bool = False
    default_volume: int = 100
    voice_status_enabled: bool = True


SCHEMA_MIGRATIONS = [
    # Migration 1: Initial schema
    """
    CREATE TABLE IF NOT EXISTS guild_settings (
        guild_id INTEGER PRIMARY KEY,
        voice_247_channel_id INTEGER NOT NULL DEFAULT 0,
        dj_role_id INTEGER NOT NULL DEFAULT 0,
        dj_only INTEGER NOT NULL DEFAULT 0,
        volume_limit INTEGER NOT NULL DEFAULT 100,
        max_duration INTEGER NOT NULL DEFAULT 0,
        max_queue INTEGER NOT NULL DEFAULT 0,
        restrict_channel_id INTEGER NOT NULL DEFAULT 0,
        restore_queue INTEGER NOT NULL DEFAULT 0,
        autoplay INTEGER NOT NULL DEFAULT 0,
        default_volume INTEGER NOT NULL DEFAULT 100
    );

    CREATE TABLE IF NOT EXISTS favorites (
        user_id INTEGER NOT NULL,
        position INTEGER NOT NULL,
        uri TEXT NOT NULL,
        title TEXT NOT NULL,
        artist TEXT NOT NULL,
        duration_ms INTEGER NOT NULL,
        added_at REAL NOT NULL,
        PRIMARY KEY (user_id, position)
    );

    CREATE TABLE IF NOT EXISTS playlists (
        playlist_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        created_at REAL NOT NULL,
        UNIQUE (user_id, name)
    );

    CREATE TABLE IF NOT EXISTS playlist_tracks (
        playlist_id INTEGER NOT NULL,
        position INTEGER NOT NULL,
        uri TEXT NOT NULL,
        title TEXT NOT NULL,
        artist TEXT NOT NULL,
        duration_ms INTEGER NOT NULL,
        added_at REAL NOT NULL,
        PRIMARY KEY (playlist_id, position),
        FOREIGN KEY (playlist_id) REFERENCES playlists (playlist_id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS queue_snapshots (
        guild_id INTEGER PRIMARY KEY,
        tracks_json TEXT NOT NULL,
        updated_at REAL NOT NULL
    );
    """,
    # Migration 2: Add voice_status_enabled to guild_settings
    """
    ALTER TABLE guild_settings ADD COLUMN voice_status_enabled INTEGER NOT NULL DEFAULT 1;
    """,
    # Migration 3: Add user_play_history table and index
    """
    CREATE TABLE IF NOT EXISTS user_play_history (
        user_id INTEGER NOT NULL,
        key TEXT NOT NULL,
        title TEXT NOT NULL,
        artist TEXT NOT NULL DEFAULT '',
        uri TEXT NOT NULL DEFAULT '',
        played_at REAL NOT NULL,
        PRIMARY KEY (user_id, key)
    );

    CREATE INDEX IF NOT EXISTS idx_user_play_history_played_at
    ON user_play_history (user_id, played_at DESC);
    """,
    # Migration 4: Persist the exact voice channel for safe session recovery.
    """
    ALTER TABLE queue_snapshots ADD COLUMN voice_channel_id INTEGER NOT NULL DEFAULT 0;
    """,
    # Migration 5: Restore the now-playing card to its previous text channel.
    """
    ALTER TABLE queue_snapshots ADD COLUMN card_channel_id INTEGER NOT NULL DEFAULT 0;
    """,
    # Migration 6: Allow an owner to grant and revoke read/play access to a playlist.
    """
    CREATE TABLE IF NOT EXISTS playlist_shares (
        playlist_id INTEGER NOT NULL,
        recipient_id INTEGER NOT NULL,
        created_at REAL NOT NULL,
        PRIMARY KEY (playlist_id, recipient_id),
        FOREIGN KEY (playlist_id) REFERENCES playlists (playlist_id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_playlist_shares_recipient ON playlist_shares (recipient_id);
    """,
    # Migration 7: Persist qualified listening totals and ranked tracks for /stats.
    """
    CREATE TABLE IF NOT EXISTS user_listening_stats (
        user_id INTEGER PRIMARY KEY,
        total_listening_ms INTEGER NOT NULL DEFAULT 0,
        total_tracks_played INTEGER NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS user_listening_tracks (
        user_id INTEGER NOT NULL,
        track_key TEXT NOT NULL,
        title TEXT NOT NULL,
        artist TEXT NOT NULL DEFAULT '',
        play_count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, track_key)
    );
    CREATE INDEX IF NOT EXISTS idx_user_listening_tracks_count
    ON user_listening_tracks (user_id, play_count DESC);
    """,
    # Migration 8: Retain the most recently played track when listening counts tie.
    """
    ALTER TABLE user_listening_tracks ADD COLUMN last_played_at REAL NOT NULL DEFAULT 0;
    """,
    # Migration 9: Persist moderated music requests across restarts.
    """
    CREATE TABLE IF NOT EXISTS track_requests (
        request_id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        requester_id INTEGER NOT NULL,
        uri TEXT NOT NULL,
        title TEXT NOT NULL,
        artist TEXT NOT NULL DEFAULT '',
        duration_ms INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_track_requests_guild ON track_requests (guild_id, request_id);
    """,
    # Migration 10: Private per-user track notes.
    """
    CREATE TABLE IF NOT EXISTS user_track_notes (
        user_id INTEGER NOT NULL,
        track_key TEXT NOT NULL,
        title TEXT NOT NULL,
        note TEXT NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (user_id, track_key)
    );
    CREATE INDEX IF NOT EXISTS idx_user_track_notes_updated ON user_track_notes (user_id, updated_at DESC);
    """,
]


class Storage:
    """Async wrapper around SQLite running in a single worker thread."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path or DEFAULT_DB_PATH)
        self._queue: queue.Queue[_StorageOp | tuple[Any, ...] | None] = queue.Queue(maxsize=STORAGE_QUEUE_LIMIT)
        self._thread: threading.Thread | None = None
        self._closed = False
        self._init_event = threading.Event()
        self._init_error: Exception | None = None
        self._settings_cache: TTLCache[int, GuildSettings] = TTLCache(max_size=2000, ttl=300.0)
        self._history_cache: TTLCache[int, list[UserHistoryEntry]] = TTLCache(max_size=2000, ttl=600.0)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_event.clear()
        self._init_error = None
        self._thread = threading.Thread(target=self._worker, name="StorageWorker", daemon=True)
        self._thread.start()
        self._init_event.wait(timeout=10.0)
        if self._init_error is not None:
            raise StorageUnavailable(f"Storage worker initialization failed: {self._init_error}")

    def _worker(self) -> None:
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(str(self.db_path), check_same_thread=True)
            conn.execute("PRAGMA journal_mode=WAL;")
            # Queue snapshots are the recovery record for an unclean process exit.
            # FULL makes a successful commit acknowledgment survive a power loss.
            conn.execute("PRAGMA synchronous=FULL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.execute("PRAGMA busy_timeout=5000;")
            self._migrate(conn)
        except Exception as exc:
            self._init_error = exc
            self._init_event.set()
            if conn:
                try:
                    conn.close()
                except Exception as close_exc:
                    log.debug("Database close error: %s", close_exc)
            return

        self._init_event.set()

        while True:
            first_raw = self._queue.get()
            if first_raw is None:
                self._queue.task_done()
                break

            batch_raw: list[Any] = [first_raw]
            while True:
                try:
                    item_raw = self._queue.get_nowait()
                    if item_raw is None:
                        # Re-enqueue shutdown sentinel so loop exits cleanly after this batch
                        self._queue.put(None)
                        break
                    batch_raw.append(item_raw)
                except queue.Empty:
                    break

            batch: list[_StorageOp] = []
            for r in batch_raw:
                if isinstance(r, _StorageOp):
                    batch.append(r)
                elif isinstance(r, tuple):
                    batch.append(_StorageOp(r[0], r[1], r[2]))
                else:
                    batch.append(r)

            # Coalesce writes (latest-wins per coalesce_key, e.g. ("queue_snapshot", guild_id))
            seen_keys: set[tuple[str, Any]] = set()
            ops_to_execute: list[_StorageOp] = []
            coalesced_ops: list[_StorageOp] = []

            for op in reversed(batch):
                if op.coalesce_key is not None:
                    if op.coalesce_key in seen_keys:
                        coalesced_ops.append(op)
                        continue
                    seen_keys.add(op.coalesce_key)
                ops_to_execute.append(op)

            ops_to_execute.reverse()

            # Fast-complete coalesced superseded ops
            for cop in coalesced_ops:
                self._queue.task_done()
                self._complete_future(cop, result=None)

            # Execute operations grouped into a single transaction. Do not resolve
            # write futures until commit succeeds: callers use this as a durability
            # boundary during graceful shutdown and queue restoration.
            has_writes = any(op.is_write for op in ops_to_execute)
            completed: list[tuple[_StorageOp, Any, Exception | None]] = []
            for op in ops_to_execute:
                try:
                    if op.is_write:
                        conn.execute("SAVEPOINT op_sp;")
                    result = op.func(conn)
                    if op.is_write:
                        conn.execute("RELEASE SAVEPOINT op_sp;")
                    completed.append((op, result, None))
                except Exception as exc:
                    if op.is_write:
                        try:
                            conn.execute("ROLLBACK TO SAVEPOINT op_sp;")
                            conn.execute("RELEASE SAVEPOINT op_sp;")
                        except Exception as rb_exc:
                            log.debug("Savepoint rollback error: %s", rb_exc)
                    completed.append((op, None, exc))
                finally:
                    self._queue.task_done()

            if has_writes or conn.in_transaction:
                try:
                    conn.commit()
                except Exception as c_exc:
                    log.debug("Batch commit error: %s", c_exc)
                    try:
                        conn.rollback()
                    except Exception:
                        log.debug("Batch rollback after commit failure failed", exc_info=True)
                    completed = [(op, None, c_exc if op.is_write and error is None else error) for op, result, error in completed]

            for op, result, error in completed:
                self._complete_future(op, result=result, error=error)

        try:
            if conn.in_transaction:
                conn.commit()
            conn.close()
        except Exception as exc:
            log.debug("Database final close error: %s", exc)

    @staticmethod
    def _complete_future(op: _StorageOp, *, result: Any = None, error: Exception | None = None) -> None:
        """Complete a caller future on its owning loop, tolerating cancellation/close races."""
        if op.loop.is_closed():
            return

        def complete() -> None:
            if op.future.done():
                return
            if error is None:
                op.future.set_result(result)
            else:
                op.future.set_exception(error)

        try:
            op.loop.call_soon_threadsafe(complete)
        except RuntimeError as exc:
            # The loop may close between is_closed() and thread-safe scheduling.
            log.debug("Could not deliver completed storage result because its event loop closed: %s", exc)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        cursor = conn.cursor()
        cursor.execute("PRAGMA user_version;")
        row = cursor.fetchone()
        current_version = int(row[0]) if row else 0

        for idx, script in enumerate(SCHEMA_MIGRATIONS, start=1):
            if idx > current_version:
                cursor.executescript(script)
                cursor.execute(f"PRAGMA user_version = {idx};")
                conn.commit()
                log.info("Applied database migration %d", idx)

    async def _run(
        self,
        func: Callable[[sqlite3.Connection], Any],
        *,
        is_write: bool = False,
        coalesce_key: tuple[str, Any] | None = None,
    ) -> Any:
        if self._closed or self._thread is None or not self._thread.is_alive():
            raise StorageUnavailable("Storage is closed or unavailable.")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()

        def _consume_storage_future(f: asyncio.Future[Any]) -> None:
            if not f.cancelled():
                f.exception()

        future.add_done_callback(_consume_storage_future)
        try:
            self._queue.put_nowait(_StorageOp(func, future, loop, is_write=is_write, coalesce_key=coalesce_key))
        except queue.Full:
            raise StorageUnavailable("Storage is busy; retry shortly.") from None
        try:
            return await future
        except (asyncio.CancelledError, GeneratorExit):
            raise
        except StorageError:
            raise
        except Exception as exc:
            log.warning("Storage query failed: %s", exc)
            raise StorageUnavailable("Storage operation failed.") from exc

    async def close(self) -> None:
        """Cleanly close the database connection and wait for the worker thread to exit."""
        if self._closed:
            return
        self._closed = True
        if self._thread is not None and self._thread.is_alive():
            await asyncio.to_thread(self._queue.put, None)
            await asyncio.to_thread(self._thread.join, 20.0)
            if self._thread.is_alive():
                raise StorageUnavailable("Storage worker did not stop before shutdown timeout.")

    # ------------------------------------------------------------- backup
    async def backup(self, dest_path: Path | str) -> None:
        """Create a backup of the database using sqlite3's online backup API."""
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)

        def _do_backup(conn: sqlite3.Connection) -> None:
            dest_conn = sqlite3.connect(str(dest))
            try:
                conn.backup(dest_conn)
            finally:
                dest_conn.close()

        await self._run(_do_backup)

    async def perform_daily_backup(self, backup_dir: Path | str, keep: int = 7) -> Path:
        """Create a dated backup using sqlite3's backup API and keep the latest N copies."""
        b_dir = Path(backup_dir)
        b_dir.mkdir(parents=True, exist_ok=True)
        dest = b_dir / f"bot_{time.strftime('%Y%m%d_%H%M%S')}.db"
        await self.backup(dest)

        try:
            existing = sorted(b_dir.glob("bot_*.db"), key=lambda p: p.stat().st_mtime)
            while len(existing) > keep:
                oldest = existing.pop(0)
                oldest.unlink(missing_ok=True)
                log.info("Removed old database backup: %s", oldest.name)
        except Exception as exc:
            log.debug("Failed rotating database backups: %s", exc)

        return dest

    # ----------------------------------------------------- guild settings
    async def get_guild_settings(self, guild_id: int) -> GuildSettings:
        cached = self._settings_cache.get(guild_id)
        if cached is not None:
            return cached

        def _get(conn: sqlite3.Connection) -> GuildSettings:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT guild_id, voice_247_channel_id, dj_role_id, dj_only, volume_limit,
                       max_duration, max_queue, restrict_channel_id, restore_queue, autoplay, default_volume,
                       voice_status_enabled
                FROM guild_settings WHERE guild_id = ?
                """,
                (guild_id,),
            )
            row = cur.fetchone()
            if not row:
                return GuildSettings(guild_id=guild_id)
            return GuildSettings(
                guild_id=row[0],
                voice_247_channel_id=row[1],
                dj_role_id=row[2],
                dj_only=bool(row[3]),
                volume_limit=row[4],
                max_duration=row[5],
                max_queue=row[6],
                restrict_channel_id=row[7],
                restore_queue=bool(row[8]),
                autoplay=bool(row[9]),
                default_volume=row[10],
                voice_status_enabled=bool(row[11]) if len(row) > 11 else True,
            )

        settings = await self._run(_get)
        self._settings_cache.set(guild_id, settings)
        return settings

    async def update_guild_settings(self, guild_id: int, **fields: Any) -> GuildSettings:
        allowed = {
            "voice_247_channel_id",
            "dj_role_id",
            "dj_only",
            "volume_limit",
            "max_duration",
            "max_queue",
            "restrict_channel_id",
            "restore_queue",
            "autoplay",
            "default_volume",
            "voice_status_enabled",
        }
        for k in fields:
            if k not in allowed:
                raise ValueError(f"Unknown guild setting: {k}")

        self._settings_cache.invalidate(guild_id)

        def _update(conn: sqlite3.Connection) -> GuildSettings:
            cur = conn.cursor()
            cur.execute("SELECT guild_id FROM guild_settings WHERE guild_id = ?", (guild_id,))
            exists = cur.fetchone() is not None
            if not exists:
                cur.execute(
                    "INSERT INTO guild_settings (guild_id) VALUES (?)",
                    (guild_id,),
                )
            if fields:
                set_clauses = [f"{col} = ?" for col in fields]
                values = [int(v) if isinstance(v, bool) else v for v in fields.values()]
                values.append(guild_id)
                cur.execute(
                    f"UPDATE guild_settings SET {', '.join(set_clauses)} WHERE guild_id = ?",
                    values,
                )
            return self._get_settings_direct(conn, guild_id)

        res = await self._run(_update, is_write=True)
        self._settings_cache.set(guild_id, res)
        return res

    @staticmethod
    def _get_settings_direct(conn: sqlite3.Connection, guild_id: int) -> GuildSettings:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT guild_id, voice_247_channel_id, dj_role_id, dj_only, volume_limit,
                   max_duration, max_queue, restrict_channel_id, restore_queue, autoplay, default_volume,
                   voice_status_enabled
            FROM guild_settings WHERE guild_id = ?
            """,
            (guild_id,),
        )
        row = cur.fetchone()
        if not row:
            return GuildSettings(guild_id=guild_id)
        return GuildSettings(
            guild_id=row[0],
            voice_247_channel_id=row[1],
            dj_role_id=row[2],
            dj_only=bool(row[3]),
            volume_limit=row[4],
            max_duration=row[5],
            max_queue=row[6],
            restrict_channel_id=row[7],
            restore_queue=bool(row[8]),
            autoplay=bool(row[9]),
            default_volume=row[10],
            voice_status_enabled=bool(row[11]) if len(row) > 11 else True,
        )

    async def delete_guild(self, guild_id: int) -> None:
        self._settings_cache.invalidate(guild_id)

        def _del(conn: sqlite3.Connection) -> None:
            cur = conn.cursor()
            cur.execute("DELETE FROM guild_settings WHERE guild_id = ?", (guild_id,))
            cur.execute("DELETE FROM queue_snapshots WHERE guild_id = ?", (guild_id,))

        await self._run(_del, is_write=True)

    # ---------------------------------------------------------- favorites
    async def add_track_request(self, guild_id: int, requester_id: int, track: StoredTrack, limit: int = 50) -> int:
        def _add(conn: sqlite3.Connection) -> int:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM track_requests WHERE guild_id = ?", (guild_id,))
            if int(cur.fetchone()[0]) >= limit:
                raise StorageError("The server request list is full. Please try again later.")
            cur.execute(
                "INSERT INTO track_requests "
                "(guild_id, requester_id, uri, title, artist, duration_ms, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (guild_id, requester_id, track.uri, track.title, track.artist, track.duration_ms, time.time()),
            )
            return int(cur.lastrowid)

        return await self._run(_add, is_write=True)

    async def list_track_requests(self, guild_id: int, limit: int = 50) -> list[tuple[int, int, StoredTrack]]:
        def _list(conn: sqlite3.Connection) -> list[tuple[int, int, StoredTrack]]:
            rows = conn.execute(
                "SELECT request_id, requester_id, uri, title, artist, duration_ms "
                "FROM track_requests WHERE guild_id = ? ORDER BY request_id LIMIT ?",
                (guild_id, limit),
            ).fetchall()
            return [(r[0], r[1], StoredTrack(r[2], r[3], r[4], r[5], r[1])) for r in rows]

        return await self._run(_list)

    async def get_track_request(self, guild_id: int, request_id: int) -> tuple[int, StoredTrack] | None:
        def _get(conn: sqlite3.Connection) -> tuple[int, StoredTrack] | None:
            row = conn.execute(
                "SELECT requester_id, uri, title, artist, duration_ms FROM track_requests WHERE guild_id = ? AND request_id = ?",
                (guild_id, request_id),
            ).fetchone()
            return (row[0], StoredTrack(row[1], row[2], row[3], row[4], row[0])) if row else None

        return await self._run(_get)

    async def delete_track_request(self, guild_id: int, request_id: int) -> bool:
        def _delete(conn: sqlite3.Connection) -> bool:
            cur = conn.execute("DELETE FROM track_requests WHERE guild_id = ? AND request_id = ?", (guild_id, request_id))
            return cur.rowcount > 0

        return await self._run(_delete, is_write=True)

    async def set_track_note(self, user_id: int, track_key: str, title: str, note: str) -> None:
        def _set(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO user_track_notes (user_id, track_key, title, note, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, track_key) DO UPDATE SET "
                "title=excluded.title, note=excluded.note, updated_at=excluded.updated_at",
                (user_id, track_key, title, note, time.time()),
            )

        await self._run(_set, is_write=True)

    async def list_track_notes(self, user_id: int, limit: int = 25) -> list[tuple[str, str, str]]:
        def _list(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
            return conn.execute(
                "SELECT track_key, title, note FROM user_track_notes WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()

        return await self._run(_list)

    async def get_track_note(self, user_id: int, track_key: str) -> str | None:
        def _get(conn: sqlite3.Connection) -> str | None:
            row = conn.execute(
                "SELECT note FROM user_track_notes WHERE user_id = ? AND track_key = ?",
                (user_id, track_key),
            ).fetchone()
            return row[0] if row else None

        return await self._run(_get)

    async def pop_track_request(self, guild_id: int, request_id: int) -> tuple[int, StoredTrack] | None:
        def _pop(conn: sqlite3.Connection) -> tuple[int, StoredTrack] | None:
            row = conn.execute(
                "SELECT requester_id, uri, title, artist, duration_ms FROM track_requests WHERE guild_id = ? AND request_id = ?",
                (guild_id, request_id),
            ).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM track_requests WHERE guild_id = ? AND request_id = ?", (guild_id, request_id))
            return row[0], StoredTrack(row[1], row[2], row[3], row[4], row[0])

        return await self._run(_pop, is_write=True)

    async def get_favorites(self, user_id: int) -> list[StoredTrack]:
        def _get(conn: sqlite3.Connection) -> list[StoredTrack]:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT uri, title, artist, duration_ms
                FROM favorites WHERE user_id = ?
                ORDER BY position ASC
                """,
                (user_id,),
            )
            return [StoredTrack(uri=r[0], title=r[1], artist=r[2], duration_ms=r[3]) for r in cur.fetchall()]

        return await self._run(_get)

    async def add_favorite(self, user_id: int, track: StoredTrack, limit: int) -> int:
        def _add(conn: sqlite3.Connection) -> int:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM favorites WHERE user_id = ?", (user_id,))
            count = cur.fetchone()[0]
            if limit > 0 and count >= limit:
                raise StorageError(f"Favorite limit of {limit} reached.")
            next_pos = count + 1
            cur.execute(
                """
                INSERT INTO favorites (user_id, position, uri, title, artist, duration_ms, added_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, next_pos, track.uri, track.title, track.artist, track.duration_ms, time.time()),
            )
            return next_pos

        return await self._run(_add, is_write=True)

    async def remove_favorite(self, user_id: int, position: int) -> StoredTrack:
        def _remove(conn: sqlite3.Connection) -> StoredTrack:
            cur = conn.cursor()
            cur.execute(
                "SELECT uri, title, artist, duration_ms FROM favorites WHERE user_id = ? AND position = ?",
                (user_id, position),
            )
            row = cur.fetchone()
            if not row:
                raise StorageError("No favorite found at that position.")
            removed = StoredTrack(uri=row[0], title=row[1], artist=row[2], duration_ms=row[3])
            cur.execute("DELETE FROM favorites WHERE user_id = ? AND position = ?", (user_id, position))
            cur.execute(
                "UPDATE favorites SET position = position - 1 WHERE user_id = ? AND position > ?",
                (user_id, position),
            )
            return removed

        return await self._run(_remove, is_write=True)

    async def clear_favorites(self, user_id: int) -> int:
        def _clear(conn: sqlite3.Connection) -> int:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM favorites WHERE user_id = ?", (user_id,))
            count = cur.fetchone()[0]
            cur.execute("DELETE FROM favorites WHERE user_id = ?", (user_id,))
            return count

        return await self._run(_clear, is_write=True)

    # ---------------------------------------------------------- playlists
    async def list_playlists(self, user_id: int) -> list[str]:
        def _list(conn: sqlite3.Connection) -> list[str]:
            cur = conn.cursor()
            cur.execute(
                "SELECT name FROM playlists WHERE user_id = ? "
                "UNION SELECT p.name FROM playlists p JOIN playlist_shares s USING (playlist_id) "
                "WHERE s.recipient_id = ? ORDER BY name COLLATE NOCASE",
                (user_id, user_id),
            )
            return [r[0] for r in cur.fetchall()]

        return await self._run(_list)

    async def create_playlist(self, user_id: int, name: str, limit: int) -> int:
        clean_name = name.strip()
        if not clean_name:
            raise StorageError("Playlist name cannot be empty.")

        def _create(conn: sqlite3.Connection) -> int:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM playlists WHERE user_id = ?", (user_id,))
            count = cur.fetchone()[0]
            if limit > 0 and count >= limit:
                raise StorageError(f"Playlist limit of {limit} reached.")
            try:
                cur.execute(
                    "INSERT INTO playlists (user_id, name, created_at) VALUES (?, ?, ?)",
                    (user_id, clean_name, time.time()),
                )
                return cur.lastrowid or 0
            except sqlite3.IntegrityError:
                raise StorageError("A playlist with that name already exists.") from None

        return await self._run(_create, is_write=True)

    async def delete_playlist(self, user_id: int, name: str) -> bool:
        clean_name = name.strip()

        def _del(conn: sqlite3.Connection) -> bool:
            cur = conn.cursor()
            cur.execute("SELECT playlist_id FROM playlists WHERE user_id = ? AND name = ?", (user_id, clean_name))
            row = cur.fetchone()
            if not row:
                return False
            playlist_id = row[0]
            cur.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))
            cur.execute("DELETE FROM playlists WHERE playlist_id = ?", (playlist_id,))
            return True

        return await self._run(_del, is_write=True)

    async def rename_playlist(self, user_id: int, old_name: str, new_name: str) -> bool:
        clean_old = old_name.strip()
        clean_new = new_name.strip()
        if not clean_new:
            raise StorageError("New playlist name cannot be empty.")

        def _rename(conn: sqlite3.Connection) -> bool:
            cur = conn.cursor()
            try:
                cur.execute(
                    "UPDATE playlists SET name = ? WHERE user_id = ? AND name = ?",
                    (clean_new, user_id, clean_old),
                )
                return cur.rowcount > 0
            except sqlite3.IntegrityError:
                raise StorageError("A playlist with the new name already exists.") from None

        return await self._run(_rename, is_write=True)

    async def get_playlist_tracks(self, user_id: int, name: str) -> list[StoredTrack]:
        clean_name = name.strip()

        def _get(conn: sqlite3.Connection) -> list[StoredTrack]:
            cur = conn.cursor()
            cur.execute(
                "SELECT playlist_id FROM playlists WHERE user_id = ? AND name = ?",
                (user_id, clean_name),
            )
            row = cur.fetchone()
            if not row:
                raise StorageError("Playlist not found.")
            playlist_id = row[0]
            cur.execute(
                """
                SELECT uri, title, artist, duration_ms
                FROM playlist_tracks WHERE playlist_id = ?
                ORDER BY position ASC
                """,
                (playlist_id,),
            )
            return [StoredTrack(uri=r[0], title=r[1], artist=r[2], duration_ms=r[3]) for r in cur.fetchall()]

        return await self._run(_get)

    async def share_playlist(self, owner_id: int, name: str, recipient_id: int) -> None:
        clean_name = name.strip()

        def _share(conn: sqlite3.Connection) -> None:
            cur = conn.cursor()
            cur.execute("SELECT playlist_id FROM playlists WHERE user_id = ? AND name = ?", (owner_id, clean_name))
            row = cur.fetchone()
            if not row:
                raise StorageError("Playlist not found.")
            try:
                cur.execute(
                    "INSERT INTO playlist_shares (playlist_id, recipient_id, created_at) VALUES (?, ?, ?)",
                    (row[0], recipient_id, time.time()),
                )
            except sqlite3.IntegrityError:
                raise StorageError("That playlist is already shared with this user.") from None

        await self._run(_share, is_write=True)

    async def unshare_playlist(self, owner_id: int, name: str, recipient_id: int) -> bool:
        def _unshare(conn: sqlite3.Connection) -> bool:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM playlist_shares WHERE recipient_id = ? AND playlist_id IN "
                "(SELECT playlist_id FROM playlists WHERE user_id = ? AND name = ?)",
                (recipient_id, owner_id, name.strip()),
            )
            return cur.rowcount > 0

        return await self._run(_unshare, is_write=True)

    async def list_shared_playlists(self, recipient_id: int) -> list[tuple[int, str]]:
        def _list(conn: sqlite3.Connection) -> list[tuple[int, str]]:
            cur = conn.cursor()
            cur.execute(
                "SELECT p.user_id, p.name FROM playlist_shares s JOIN playlists p USING (playlist_id) "
                "WHERE s.recipient_id = ? ORDER BY p.name",
                (recipient_id,),
            )
            return [(int(row[0]), str(row[1])) for row in cur.fetchall()]

        return await self._run(_list)

    async def get_shared_playlist_tracks(self, recipient_id: int, owner_id: int, name: str) -> list[StoredTrack]:
        def _get(conn: sqlite3.Connection) -> list[StoredTrack]:
            cur = conn.cursor()
            cur.execute(
                "SELECT p.playlist_id FROM playlists p JOIN playlist_shares s USING (playlist_id) "
                "WHERE s.recipient_id = ? AND p.user_id = ? AND p.name = ?",
                (recipient_id, owner_id, name.strip()),
            )
            row = cur.fetchone()
            if not row:
                raise StorageError("No shared playlist with that owner and name was found.")
            cur.execute(
                "SELECT uri, title, artist, duration_ms FROM playlist_tracks WHERE playlist_id = ? ORDER BY position", (row[0],)
            )
            return [StoredTrack(uri=r[0], title=r[1], artist=r[2], duration_ms=r[3]) for r in cur.fetchall()]

        return await self._run(_get)

    async def add_playlist_track(self, user_id: int, name: str, track: StoredTrack, limit: int) -> int:
        clean_name = name.strip()

        def _add(conn: sqlite3.Connection) -> int:
            cur = conn.cursor()
            cur.execute(
                "SELECT playlist_id FROM playlists WHERE user_id = ? AND name = ?",
                (user_id, clean_name),
            )
            row = cur.fetchone()
            if not row:
                raise StorageError("Playlist not found.")
            playlist_id = row[0]
            cur.execute("SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))
            count = cur.fetchone()[0]
            if limit > 0 and count >= limit:
                raise StorageError(f"Playlist track limit of {limit} reached.")
            next_pos = count + 1
            cur.execute(
                """
                INSERT INTO playlist_tracks (playlist_id, position, uri, title, artist, duration_ms, added_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (playlist_id, next_pos, track.uri, track.title, track.artist, track.duration_ms, time.time()),
            )
            return next_pos

        return await self._run(_add, is_write=True)

    async def remove_playlist_track(self, user_id: int, name: str, position: int) -> StoredTrack:
        clean_name = name.strip()

        def _remove(conn: sqlite3.Connection) -> StoredTrack:
            cur = conn.cursor()
            cur.execute(
                "SELECT playlist_id FROM playlists WHERE user_id = ? AND name = ?",
                (user_id, clean_name),
            )
            row = cur.fetchone()
            if not row:
                raise StorageError("Playlist not found.")
            playlist_id = row[0]
            cur.execute(
                "SELECT uri, title, artist, duration_ms FROM playlist_tracks WHERE playlist_id = ? AND position = ?",
                (playlist_id, position),
            )
            row_track = cur.fetchone()
            if not row_track:
                raise StorageError("No track found at that position in playlist.")
            removed = StoredTrack(uri=row_track[0], title=row_track[1], artist=row_track[2], duration_ms=row_track[3])
            cur.execute("DELETE FROM playlist_tracks WHERE playlist_id = ? AND position = ?", (playlist_id, position))
            cur.execute(
                "UPDATE playlist_tracks SET position = position - 1 WHERE playlist_id = ? AND position > ?",
                (playlist_id, position),
            )
            return removed

        return await self._run(_remove, is_write=True)

    # ----------------------------------------------------- queue snapshot
    async def save_queue_snapshot(
        self,
        guild_id: int,
        tracks: list[StoredTrack],
        *,
        voice_channel_id: int = 0,
        card_channel_id: int = 0,
    ) -> None:
        raw_list = [
            {
                "uri": t.uri,
                "title": t.title,
                "artist": t.artist,
                "duration_ms": t.duration_ms,
                "requester_id": t.requester_id,
                "position_ms": max(0, int(t.position_ms)),
            }
            for t in tracks
        ]
        try:
            import orjson

            data_json = orjson.dumps(raw_list).decode("utf-8")
        except ImportError:
            data_json = json.dumps(raw_list, separators=(",", ":"))

        def _save(conn: sqlite3.Connection) -> None:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO queue_snapshots (guild_id, tracks_json, updated_at, voice_channel_id, card_channel_id)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    tracks_json = excluded.tracks_json,
                    updated_at = excluded.updated_at,
                    voice_channel_id = excluded.voice_channel_id,
                    card_channel_id = excluded.card_channel_id
                """,
                (guild_id, data_json, time.time(), max(0, int(voice_channel_id)), max(0, int(card_channel_id))),
            )

        await self._run(_save, is_write=True, coalesce_key=("queue_snapshot", guild_id))

    async def get_queue_snapshot_voice_channel_id(self, guild_id: int) -> int:
        """Return the saved channel only when a recoverable queue snapshot exists."""

        def _get(conn: sqlite3.Connection) -> int:
            cur = conn.cursor()
            cur.execute(
                "SELECT voice_channel_id FROM queue_snapshots WHERE guild_id = ? AND tracks_json != ''",
                (guild_id,),
            )
            row = cur.fetchone()
            return max(0, int(row[0])) if row and row[0] is not None else 0

        return await self._run(_get)

    async def get_queue_snapshot_card_channel_id(self, guild_id: int) -> int:
        """Return the text channel used by the recoverable now-playing card."""

        def _get(conn: sqlite3.Connection) -> int:
            cur = conn.cursor()
            cur.execute(
                "SELECT card_channel_id FROM queue_snapshots WHERE guild_id = ? AND tracks_json != ''",
                (guild_id,),
            )
            row = cur.fetchone()
            return max(0, int(row[0])) if row and row[0] is not None else 0

        return await self._run(_get)

    async def get_queue_snapshot(self, guild_id: int) -> list[StoredTrack]:
        def _get(conn: sqlite3.Connection) -> list[StoredTrack]:
            cur = conn.cursor()
            cur.execute("SELECT tracks_json FROM queue_snapshots WHERE guild_id = ?", (guild_id,))
            row = cur.fetchone()
            if not row or not row[0]:
                return []
            try:
                try:
                    import orjson

                    items = orjson.loads(row[0])
                except ImportError:
                    items = json.loads(row[0])
                return [StoredTrack(**item) for item in items]
            except Exception as exc:
                log.warning("Corrupted queue snapshot for guild %s: %s", guild_id, exc)
                return []

        return await self._run(_get)

    async def delete_queue_snapshot(self, guild_id: int) -> None:
        def _del(conn: sqlite3.Connection) -> None:
            cur = conn.cursor()
            cur.execute("DELETE FROM queue_snapshots WHERE guild_id = ?", (guild_id,))

        await self._run(_del, is_write=True, coalesce_key=("queue_snapshot", guild_id))

    # ---------------------------------------------------- user data reset
    async def reset_user_data(self, user_id: int) -> None:
        def _reset(conn: sqlite3.Connection) -> None:
            cur = conn.cursor()
            cur.execute("DELETE FROM favorites WHERE user_id = ?", (user_id,))
            cur.execute(
                "DELETE FROM playlist_tracks WHERE playlist_id IN (SELECT playlist_id FROM playlists WHERE user_id = ?)",
                (user_id,),
            )
            cur.execute("DELETE FROM playlists WHERE user_id = ?", (user_id,))
            cur.execute("DELETE FROM user_play_history WHERE user_id = ?", (user_id,))
            cur.execute("DELETE FROM track_requests WHERE requester_id = ?", (user_id,))
            cur.execute("DELETE FROM playlist_shares WHERE recipient_id = ?", (user_id,))
            cur.execute("DELETE FROM user_listening_tracks WHERE user_id = ?", (user_id,))
            cur.execute("DELETE FROM user_listening_stats WHERE user_id = ?", (user_id,))
            cur.execute("DELETE FROM user_track_notes WHERE user_id = ?", (user_id,))

        try:
            await self._run(_reset, is_write=True)
        finally:
            self._history_cache.invalidate(user_id)

    # ---------------------------------------------------- user play history
    async def record_user_play_history(
        self,
        user_id: int,
        title: str,
        artist: str = "",
        uri: str = "",
        max_entries: int = 50,
    ) -> None:
        def _record(conn: sqlite3.Connection) -> None:
            now = time.time()
            clean_title = (title or "").strip()
            clean_artist = (artist or "").strip()
            clean_uri = (uri or "").strip()
            k = clean_uri.lower() if clean_uri else f"{clean_title} {clean_artist}".strip().lower()
            if not k:
                return
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO user_play_history (user_id, key, title, artist, uri, played_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (user_id, key) DO UPDATE SET
                    played_at = excluded.played_at,
                    title = excluded.title,
                    artist = excluded.artist,
                    uri = excluded.uri;
                """,
                (user_id, k, clean_title, clean_artist, clean_uri, now),
            )
            limit_val = max(1, max_entries)
            cur.execute(
                """
                DELETE FROM user_play_history
                WHERE user_id = ?
                  AND key NOT IN (
                      SELECT key FROM user_play_history
                      WHERE user_id = ?
                      ORDER BY played_at DESC
                      LIMIT ?
                  );
                """,
                (user_id, user_id, limit_val),
            )

        try:
            await self._run(_record, is_write=True)
            await self._refresh_user_history_cache(user_id, max_entries=max_entries)
        except Exception as exc:
            log.debug("user=%s record_user_play_history error: %s", user_id, exc)

    async def _refresh_user_history_cache(self, user_id: int, max_entries: int = 50) -> None:
        try:
            entries = await self.get_user_play_history(user_id, limit=max(max_entries, 50))
            self._history_cache.set(user_id, entries)
        except Exception as exc:
            self._history_cache.invalidate(user_id)
            log.debug("user=%s refresh history cache error: %s", user_id, exc)

    async def get_user_play_history(self, user_id: int, limit: int = 25) -> list[UserHistoryEntry]:
        def _get(conn: sqlite3.Connection) -> list[UserHistoryEntry]:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT title, artist, uri, played_at
                FROM user_play_history
                WHERE user_id = ?
                ORDER BY played_at DESC
                LIMIT ?;
                """,
                (user_id, limit),
            )
            rows = cur.fetchall()
            return [
                UserHistoryEntry(
                    title=str(r[0]),
                    artist=str(r[1] or ""),
                    uri=str(r[2] or ""),
                    played_at=float(r[3]),
                )
                for r in rows
            ]

        return await self._run(_get)

    async def get_recent_bot_history_tracks(self, user_id: int, limit: int = 200) -> list[StoredTrack]:
        def _get(conn: sqlite3.Connection) -> list[StoredTrack]:
            cur = conn.cursor()
            cur.execute(
                "SELECT h.title, h.artist, h.uri FROM user_play_history h "
                "WHERE h.user_id = ? AND h.played_at = (SELECT MAX(latest.played_at) "
                "FROM user_play_history latest WHERE latest.user_id = h.user_id AND latest.key = h.key) "
                "ORDER BY h.played_at DESC LIMIT ?",
                (user_id, max(1, min(limit, 500))),
            )
            return [StoredTrack(uri=r[2], title=r[0], artist=r[1], duration_ms=0) for r in cur.fetchall()]

        return await self._run(_get)

    async def get_user_play_history_cached(self, user_id: int, limit: int = 25) -> list[UserHistoryEntry]:
        cached = self._history_cache.get(user_id)
        if cached is not None:
            return cached[:limit]
        try:
            entries = await asyncio.wait_for(
                self.get_user_play_history(user_id, limit=max(limit, 50)),
                timeout=1.5,
            )
            self._history_cache.set(user_id, entries)
            return entries[:limit]
        except (asyncio.TimeoutError, TimeoutError):
            log.debug("user=%s history cache miss timed out (>1.5s)", user_id)
            return []
        except Exception as exc:
            log.debug("user=%s history cache miss error: %s", user_id, exc)
            return []

    async def delete_user_history(self, user_id: int) -> None:
        def _del(conn: sqlite3.Connection) -> None:
            cur = conn.cursor()
            cur.execute("DELETE FROM user_play_history WHERE user_id = ?", (user_id,))
            cur.execute("DELETE FROM user_listening_tracks WHERE user_id = ?", (user_id,))
            cur.execute("DELETE FROM user_listening_stats WHERE user_id = ?", (user_id,))

        try:
            await self._run(_del, is_write=True)
        finally:
            self._history_cache.invalidate(user_id)

    async def record_user_listening_stats(
        self, user_id: int, title: str, artist: str, listened_ms: int, *, max_tracks: int = 500
    ) -> None:
        if user_id <= 0 or listened_ms <= 0:
            return
        clean_title = (title or "").strip()[:200]
        clean_artist = (artist or "").strip()[:100]
        if not clean_title:
            return
        key = f"{clean_title} - {clean_artist}".casefold()

        def _record(conn: sqlite3.Connection) -> None:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO user_listening_stats (user_id, total_listening_ms, total_tracks_played, updated_at) "
                "VALUES (?, ?, 1, ?) ON CONFLICT(user_id) DO UPDATE SET "
                "total_listening_ms = total_listening_ms + excluded.total_listening_ms, "
                "total_tracks_played = total_tracks_played + 1, updated_at = excluded.updated_at",
                (user_id, listened_ms, time.time()),
            )
            cur.execute(
                "INSERT INTO user_listening_tracks (user_id, track_key, title, artist, play_count, last_played_at) "
                "VALUES (?, ?, ?, ?, 1, ?) ON CONFLICT(user_id, track_key) DO UPDATE SET "
                "title = excluded.title, artist = excluded.artist, play_count = play_count + 1, "
                "last_played_at = excluded.last_played_at",
                (user_id, key, clean_title, clean_artist, time.time()),
            )
            limit_value = max(1, max_tracks)
            cur.execute(
                "DELETE FROM user_listening_tracks WHERE user_id = ? AND track_key != ? AND track_key NOT IN "
                "(SELECT track_key FROM user_listening_tracks WHERE user_id = ? "
                "ORDER BY play_count DESC, last_played_at DESC LIMIT ?)",
                (user_id, key, user_id, limit_value),
            )

        await self._run(_record, is_write=True)

    async def get_user_listening_stats(self, user_id: int) -> dict[str, Any]:
        def _get(conn: sqlite3.Connection) -> dict[str, Any]:
            cur = conn.cursor()
            cur.execute(
                "SELECT total_listening_ms, total_tracks_played FROM user_listening_stats WHERE user_id = ?",
                (user_id,),
            )
            row = cur.fetchone()
            cur.execute(
                "SELECT artist, SUM(play_count) AS plays FROM user_listening_tracks "
                "WHERE user_id = ? AND artist != '' GROUP BY artist ORDER BY plays DESC, artist COLLATE NOCASE LIMIT 10",
                (user_id,),
            )
            artists = [(str(r[0]), int(r[1])) for r in cur.fetchall()]
            cur.execute(
                "SELECT title, artist, play_count FROM user_listening_tracks "
                "WHERE user_id = ? ORDER BY play_count DESC, last_played_at DESC LIMIT 10",
                (user_id,),
            )
            tracks = [(str(r[0]), str(r[1]), int(r[2])) for r in cur.fetchall()]
            return {
                "total_listening_ms": int(row[0]) if row else 0,
                "total_tracks_played": int(row[1]) if row else 0,
                "artists": artists,
                "tracks": tracks,
            }

        return await self._run(_get)

    # ---------------------------------------------------- json export
    async def export_all_json(self) -> dict[str, Any]:
        def _export(conn: sqlite3.Connection) -> dict[str, Any]:
            export: dict[str, Any] = {
                "guild_settings": [],
                "favorites": [],
                "playlists": [],
                "playlist_shares": [],
                "queue_snapshots": [],
                "track_requests": [],
                "user_track_notes": [],
                "user_play_history": [],
                "user_listening_stats": [],
                "user_listening_tracks": [],
            }

            cur = conn.cursor()
            cur.execute("SELECT * FROM guild_settings")
            cols = [d[0] for d in cur.description]
            for row in cur.fetchall():
                export["guild_settings"].append(dict(zip(cols, row)))

            cur.execute("SELECT * FROM favorites")
            cols = [d[0] for d in cur.description]
            for row in cur.fetchall():
                export["favorites"].append(dict(zip(cols, row)))

            cur.execute("SELECT * FROM playlists")
            cols = [d[0] for d in cur.description]
            playlists = [dict(zip(cols, row)) for row in cur.fetchall()]

            for pl in playlists:
                cur.execute(
                    "SELECT position, uri, title, artist, duration_ms, added_at "
                    "FROM playlist_tracks WHERE playlist_id = ? ORDER BY position ASC",
                    (pl["playlist_id"],),
                )
                t_cols = [d[0] for d in cur.description]
                pl["tracks"] = [dict(zip(t_cols, r)) for r in cur.fetchall()]
            export["playlists"] = playlists

            cur.execute("SELECT * FROM queue_snapshots")
            cols = [d[0] for d in cur.description]
            for row in cur.fetchall():
                export["queue_snapshots"].append(dict(zip(cols, row)))

            cur.execute("SELECT * FROM track_requests")
            cols = [d[0] for d in cur.description]
            for row in cur.fetchall():
                export["track_requests"].append(dict(zip(cols, row)))

            cur.execute("SELECT * FROM user_track_notes")
            cols = [d[0] for d in cur.description]
            for row in cur.fetchall():
                export["user_track_notes"].append(dict(zip(cols, row)))

            cur.execute("SELECT * FROM user_play_history")
            cols = [d[0] for d in cur.description]
            for row in cur.fetchall():
                export["user_play_history"].append(dict(zip(cols, row)))

            for table in ("playlist_shares", "user_listening_stats", "user_listening_tracks"):
                cur.execute(f"SELECT * FROM {table}")
                cols = [d[0] for d in cur.description]
                export[table] = [dict(zip(cols, row)) for row in cur.fetchall()]

            return export

        return await self._run(_export)

    async def delete_guild_data(self, guild_id: int) -> None:
        """Delete all guild settings and snapshots when the bot is removed from a guild."""
        self._settings_cache.invalidate(guild_id)

        def _del(conn: sqlite3.Connection) -> None:
            cur = conn.cursor()
            cur.execute("DELETE FROM guild_settings WHERE guild_id = ?", (guild_id,))
            cur.execute("DELETE FROM queue_snapshots WHERE guild_id = ?", (guild_id,))
            cur.execute("DELETE FROM track_requests WHERE guild_id = ?", (guild_id,))

        await self._run(_del, is_write=True)
