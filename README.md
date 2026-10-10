# Discord Music Bot (discord.py + lavalink.py)

[![CI](https://github.com/Sharon-ctl/Melora.py/actions/workflows/ci.yml/badge.svg)](https://github.com/Sharon-ctl/Melora.py/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/downloads/release/python-3119/)
[![Tests](https://img.shields.io/badge/tests-238%20passed-brightgreen.svg)](https://github.com/Sharon-ctl/Melora.py)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)

A slash-command-only music bot built for unattended operation. It connects to an existing Lavalink 4.x server as a client. It does not run or configure Lavalink.

- discord.py 2.7.1, lavalink.py 5.11.0, Python 3.11.9
- Intents: guilds, voice_states, guild_messages. No members or presence intents. Message content intent is disabled and message cache is turned off.
- Per-guild players, a bot-owned queue, supervised background tasks, watchdog, soak test, and Components V2 interactive cards.

## Prerequisites

- **Python:** Python 3.11+ (tested on Python 3.11.9)
- **Java:** Java 17 or higher (required to run Lavalink 4.x)
- **Discord Bot:** An application created in the Discord Developer Portal

## Bot Invite & Required Permissions

Invite the bot to your Discord server using the permission integer calculated dynamically from `discord.Permissions(view_channel=True, send_messages=True, connect=True, speak=True, set_voice_channel_status=True)`:

- **Permission Integer:** `281474979859456`
- **OAuth2 Scopes:** `bot applications.commands`
- **Required Permissions:**
  - View Channels (`view_channel`)
  - Send Messages (`send_messages`)
  - Connect (`connect`)
  - Speak (`speak`)
  - Set Voice Channel Status (`set_voice_channel_status`)

Invite URL format:
```
https://discord.com/api/oauth2/authorize?client_id=<YOUR_CLIENT_ID>&permissions=281474979859456&scope=bot%20applications.commands
```

> **Important Note for Existing Servers:** Servers that already invited the bot must grant the **"Set Voice Channel Status"** permission to the bot's role or voice channel overrides for dynamic voice channel status updates to function. If the permission is missing, the bot skips updates quietly without disrupting playback.

---

## 1. Lavalink Server Setup

The bot connects to a Lavalink 4.x server as an audio client. Follow these steps to set up Lavalink:

1. Download the latest Lavalink 4.x release (`Lavalink.jar`) from the official Lavalink repository.
2. In the same directory as `Lavalink.jar`, create an `application.yml` file (you can copy `application.yml.example` provided in this repository).
3. Use the following recommended `application.yml` configuration:

```yaml
server:
  port: 2333
  # 127.0.0.1 = only this PC. For a node on another machine use 0.0.0.0
  # (or the node's IP), a STRONG password, and a firewall rule that only
  # allows your bot's IP.
  address: 127.0.0.1
  http2:
    enabled: false

lavalink:
  plugins:
    - dependency: "dev.lavalink.youtube:youtube-plugin:f45bbb7aebfcbc1c553769e04af6cd43afa8b7c3"
      snapshot: true
  server:
    password: "change-me"
    sources:
      youtube: false
      soundcloud: true
      bandcamp: true
      http: false
      local: false
      twitch: false
      vimeo: false
      nico: false
    filters:
      volume: true
      equalizer: true
      karaoke: true
      timescale: true
      tremolo: true
      vibrato: true
      rotation: true
      distortion: true
      channelMix: true
      lowPass: true

    # Audio quality. This is the main CPU cost per player. If a node's CPU
    # ever saturates, drop resamplingQuality to MEDIUM first.
    opusEncodingQuality: 10
    resamplingQuality: HIGH

    # Smoothness and memory per player
    bufferDurationMs: 600
    frameBufferDurationMs: 5000
    trackStuckThresholdMs: 10000
    useSeekGhosting: true
    playerUpdateInterval: 10
    # Optional, for nodes with many players and GC warnings in the log.
    # Fewer allocations per player; the cost is non-instant volume changes.
    # nonAllocatingFrameBuffer: true

    youtubeSearchEnabled: true
    soundcloudSearchEnabled: true
    gc-warnings: true

plugins:
  youtube:
    enabled: true
    allowSearch: true
    allowDirectVideoIds: true
    allowDirectPlaylistIds: true
    # MUSIC = ytmsearch, WEB = metadata only, TV = the only client that plays audio.
    # If TV ever breaks, re-add ANDROID_VR and WEBEMBEDDED here as fallbacks.
    clients:
      - MUSIC
      - WEB
      - TV
    clientOptions:
      WEB:
        playback: false
    oauth:
      enabled: true
      #refreshToken: "refresh-token-here"
    remoteCipher:
      url: "https://cipher.kikkia.dev/"
      userAgent: "my-music-bot"

logging:
  file:
    path: ./logs/
  level:
    root: INFO
    lavalink: INFO
    dev.lavalink.youtube.http.YoutubeOauth2Handler: INFO
    dev.lavalink.youtube.clients.skeleton.StreamingNonMusicClient: ERROR
  request:
    # One log line per API request is heavy I/O with many players.
    # Turn it back on temporarily when debugging.
    enabled: false
    includeClientInfo: true
    includeHeaders: false
    includeQueryString: true
    includePayload: false
    maxPayloadLength: 1000
  logback:
    rollingpolicy:
      max-file-size: 10MB
      max-history: 7
```

4. Start Lavalink:
   ```bash
   java -jar Lavalink.jar
   ```

> Note: The voice channel bitrate in Discord is the real ceiling for audio quality. To ensure the cleanest sound, server administrators should set the Discord voice channel bitrate slider to the server's maximum available bitrate.

---

## 2. Discord Developer Portal Setup

1. Open https://discord.com/developers/applications and create or select your application.
2. **Bot tab:**
   - Copy the bot token into `.env` as `DISCORD_TOKEN`.
   - **Privileged Gateway Intents:** Confirm all three stay **OFF**:
     - Presence Intent: OFF
     - Server Members Intent: OFF
     - Message Content Intent: OFF
3. **Owner ID:** Turn on Developer Mode in Discord, right-click your account name, click **Copy User ID**, and paste it into `.env` as `OWNER_ID`.

### Bot Invite URL

Scopes: `bot` and `applications.commands`. Replace `YOUR_CLIENT_ID` with the application ID:

```
https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&scope=bot%20applications.commands&permissions=3148800
```

Minimum permissions (integer `3148800`):

| Permission | Why |
| --- | --- |
| View Channel | See voice and text channels |
| Connect | Join voice channels |
| Speak | Play audio |
| Send Messages | Notices outside commands (track failures, connection lost, mentions). |

---

## 3. Bot Installation Guide

### Windows Setup

1. Open PowerShell in this folder.
2. Create and activate the virtual environment:

   ```powershell
   py -3.11 -m venv .venv
   .venv\Scripts\Activate.ps1
   ```

3. Install dependencies:

   ```powershell
   pip install -r requirements-dev.txt
   ```

4. Copy the example config and edit it:

   ```powershell
   copy .env.example .env
   notepad .env
   ```

   Required variables: `DISCORD_TOKEN`, `OWNER_ID`, `LAVALINK_HOST`, `LAVALINK_PORT`, `LAVALINK_PASSWORD`. Startup fails with a clear list of every problem if anything is missing or invalid. Tokens and secrets are never logged.

5. First-time command sync:
   Set `SYNC_ON_START=true` in `.env` for the first run so slash commands are registered with Discord. After that, set it back to `false`. Alternatively, use the `/sync` slash command as the bot owner.

6. Start the bot:

   ```powershell
   run.bat
   # Or run directly:
   python main.py
   ```

### Linux / macOS Setup

1. Open a terminal in this folder.
2. Create and activate the virtual environment:

   ```bash
   python3.11 -m venv .venv
   source .venv/bin/activate
   ```

3. Install dependencies:

   ```bash
   pip install -r requirements-dev.txt
   ```

4. Copy the example config and edit it:

   ```bash
   cp .env.example .env
   nano .env
   ```

5. Start the bot:

   ```bash
   python main.py
   ```

## Spotify Integration Disclosure

Spotify integration operates via web scraping (using `spotifyscraper` with an aiohttp embed fallback) without requiring Spotify Web API developer credentials.
- Notice: Scraping may break if Spotify modifies its public page structures, and automated scraping may conflict with Spotify's Terms of Service.
- Spotify tracks and playlists are resolved to YouTube Music or YouTube audio lazily at playtime using an intelligent candidate scoring engine.

## Configuration Reference

| Variable | Default | Description |
| --- | --- | --- |
| `DISCORD_TOKEN` | Required | Discord bot token |
| `OWNER_ID` | Required | Discord user ID of the bot owner |
| `LAVALINK_HOST` | 127.0.0.1 | Lavalink server host |
| `LAVALINK_PORT` | 2333 | Lavalink server port |
| `LAVALINK_PASSWORD` | Required | Lavalink server password |
| `DEFAULT_VOLUME` | 100 | Default playback volume (clamped 0-100) |
| `SPOTIFY_ENABLED` | true | Enable Spotify link parsing |
| `VOTE_SKIP_ENABLED` | true | Enable vote skip when multiple listeners present |
| `VOTE_SKIP_MIN_LISTENERS`| 3 | Minimum human listeners required before vote skip activates |
| `HISTORY_SIZE` | 50 | Maximum number of recently played tracks in history |
| `AUTOPLAY_BATCH_SIZE` | 5 | Tracks fetched per autoplay batch |
| `MAX_FAVORITES_PER_USER`| 50 | Maximum saved favorites per user |
| `MAX_PLAYLISTS_PER_USER`| 20 | Maximum custom playlists per user |
| `MAX_TRACKS_PER_PLAYLIST`| 100 | Maximum tracks per custom playlist |
| `DB_PATH` | data/bot.db | SQLite database file location |
| `BACKUP_COUNT` | 7 | Number of daily database backups to retain |
| `MENTION_REPLY_COOLDOWN`| 10.0 | Per-user cooldown for bot mention replies |
| `AUTOCOMPLETE_SEARCH_ENABLED` | true | Enable live search autocomplete suggestions |
| `VOICE_STATUS_ENABLED` | true | Enable dynamic voice channel status updates globally |
| `VOICE_STATUS_USE_EMOJI` | true | Use custom emojis in voice status (falls back to plain text if false or on Discord 400) |
| `USER_HISTORY_ENABLED` | true | Enable personal play history suggestions in autocomplete |
| `USER_HISTORY_MAX` | 50 | Maximum play history tracks stored per user |

## Commands

All commands are slash commands only, except mentioning the bot in chat.

### Playback Commands
- `/play query`: Play a track, search, or Spotify link.
- `/nowplaying`: Move the live Components V2 Now Playing card to current channel.
- `/insert query [position]`: Insert track at position (default 1).
- `/playnext query`: Put track next in queue.
- `/playinstant query`: Play immediately, replacing current track without altering queue.
- `/pause`: Pause playback.
- `/resume`: Resume playback.
- `/skip`: Skip track. Immediate for DJ, requester, or admin; majority vote when 3+ listeners.
- `/previous`: Replay previous track from history. Current track returns to queue front.
- `/replay`: Replay current track from the beginning.
- `/seek position`: Seek to position (e.g. 1:30 or 90).
- `/forward seconds`: Jump forward by N seconds.
- `/rewind seconds`: Jump backward by N seconds.
- `/skipto position`: Skip directly to queue position.
- `/sleep minutes`: Stop playback and leave after N minutes (0 cancels).
- `/search query`: Interactive Components V2 card with top 5 results.
- `/autoplay [enabled]`: Automatically queue related tracks when queue ends.
- `/similar`: Queue up to 5 tracks similar to the current song.
- `/volume [level]`: Set volume (0-100), bounded by server volume limit.
- `/stop`: Stop playback, clear queue, stay connected.
- `/leave`: Disconnect and destroy player.

### Queue Commands
- `/queue [page]`: Components V2 card with pagination buttons (< and >).
- `/remove position [count]`: Remove track(s) at position.
- `/clear`: Clear upcoming tracks.
- `/shuffle`: Shuffle upcoming tracks.
- `/loop mode`: Loop off, track, or queue.
- `/move from to`: Move track position.
- `/swap pos1 pos2`: Swap two track positions.
- `/dedupe`: Remove duplicate tracks in queue.
- `/history`: View last 20 played tracks.
- `/savequeue name`: Save current queue as a custom playlist.

### Library Commands
- `/favorites list`: View your saved favorite tracks.
- `/favorites add`: Add currently playing track to favorites.
- `/favorites play [shuffle]`: Play your favorites list.
- `/favorites remove position`: Remove a track from favorites.
- `/favorites clear`: Clear all favorites.
- `/playlist create name`: Create a custom playlist.
- `/playlist delete name`: Delete a custom playlist.
- `/playlist rename old new`: Rename a custom playlist.
- `/playlist list`: List your custom playlists.
- `/playlist view name`: View tracks in a custom playlist.
- `/playlist play name [shuffle]`: Enqueue a custom playlist.
- `/playlist add name [query]`: Add query or current track to playlist.
- `/playlist remove name position`: Remove track from playlist.

### Settings Commands (Manage Server)
- `/settings view`: View server music settings.
- `/settings djrole [role]`: Set or clear DJ role.
- `/settings dj-only enabled`: Enforce DJ role for playback controls.
- `/settings volume-limit limit`: Maximum allowed server volume (1-100).
- `/settings max-duration minutes`: Maximum track length in minutes.
- `/settings max-queue count`: Maximum queue capacity.
- `/settings restrict [channel]`: Restrict music commands to a channel.
- `/settings restore-queue enabled`: Opt-in queue snapshot restoration on restart.
- `/settings voice-status enabled`: Toggle dynamic voice channel status updates for this server (default true).
- `/defaultvolume volume`: Set server starting volume.
- `/247`: Toggle 24/7 mode to prevent idle and alone disconnects.

### Filters & Equalizer Commands
- `/eq preset name`: Apply an equalizer preset (flat, bassboost, rock, pop, electronic, classical).
- `/eq reset`: Reset equalizer to flat.
- `/filter add name`: Apply an audio filter effect (bassboost, nightcore, vaporwave, rotation, karaoke, tremolo, vibrato).
- `/filter remove name`: Remove an active filter.
- `/filter reset`: Reset all filters to default.
- `/filter list`: List available presets and active filters.

### Owner & Privacy Commands
- `/sync`: Owner only: sync slash commands with Discord.
- `/status`: Components V2 card with latency, uptime, Lavalink node stats, voice bitrate, and owner diagnostics.
- `/help`: Dynamically generated command list from tree.
- `/errors [count]`: Owner only: bounded in-memory ring buffer of recent unhandled error logs.
- `/privacy`: Lists what data is stored (favorites, playlists, and titles and links of tracks requested, kept per user across servers, at most 50) and how to delete it.
- `/reset`: Delete all your stored data (favorites, playlists, and play history) with secondary button confirmation.

### Bot Mention Reply
- When a user mentions only the bot in a text channel, the bot replies with a compact Components V2 card pointing to `/help`.

## UI Design & Now Playing Card

The bot features a streamlined visual presentation across all interactions:

### Message Format
- **Pattern A (Actions & Info):** `{Action} **{Subject}** • `{Detail}`` (single line, detail optional)
  - Example: `Added **Song Title** • `3:42``
  - Example: `Loop set to **Track**`
- **Pattern B (Errors & Denials):** `**{Problem}** • `{What to do}`` (single line)
  - Example: `**Not in a voice channel** • `Join one and try again``
- **Pattern C (Card Rows):** `**Label:** `value``
  - Example: `**Duration:** `3:42``
  - Example: `1. **Song Title** • `3:42``

### Now Playing Card Architecture
- Built with Discord Components V2 using a single Container with neutral gray accent colour.
- **Section Component:**
  - Accessory: static 128px PNG `Thumbnail` of the song artwork (falls back to bot avatar).
  - Text Display 1: Playback state header text:
    - Playing: `<:music:1558305498531631155> **Playing**` (plain fallback `**Playing**`).
    - Paused: `<:music:1558305498531631155> **Paused**` (plain fallback `**Paused**`).
    - Edited immediately on pause and resume so header matches real state.
  - Text Display 2:
    - Clickable markdown track link: `### [Song Title](track uri)` (title markdown escaped, prefixed with `###`, and truncated to 80 characters).
    - Silent requester mention: `**Requested by:** <@requester_id>` (sent and edited with `allowed_mentions=AllowedMentions.none()` so it never pings or highlights).
    - Duration line: `**Duration:** `3:42`` (or ``LIVE`` for streams).
- **ActionRow Component (5 buttons in exact order):**
  1. `Loop` (`ButtonStyle.secondary`, custom emoji `loop`, cycles off -> track -> queue -> off).
  2. `Previous` (`ButtonStyle.secondary`, custom emoji `previous`, disabled when queue history is empty).
  3. `Pause/Resume` (`ButtonStyle.secondary`, toggles playback and dynamically swaps between `pause` and `resume` emoji; updates card in place).
  4. `Skip` (`ButtonStyle.secondary`, custom emoji `skip`, immediate skip or vote-skip).
  5. `Stop` (`ButtonStyle.danger`, custom emoji `stop`, stops playback and deletes card through player lifecycle).

## Emoji Configuration (data/emojis.json)

Custom emojis are loaded once at startup from `data/emojis.json`:

```json
{
  "pause": 1526024157526229082,
  "resume": 1526024227881353296,
  "previous": 1525998374921175090,
  "skip": 1525998356529156137,
  "loop": 1526006850531754004,
  "stop": 1526006888498466947
}
```

- **Permission Requirements:** The emojis must be usable by the bot. They must either be uploaded as application emojis in the Discord Developer Portal or the bot must be a member of the guild that owns them.
- **Graceful Fallback:** If `emojis.json` is missing, malformed, or has missing/invalid emoji IDs, the bot logs a warning and falls back to clean text labels for that button (`Loop`, `Previous`, `Pause`/`Resume`, `Skip`, `Stop`).

## Unlimited Defaults & Scaling Configuration

All artificial user-facing limits default to **0 (unlimited)**:
- `MAX_QUEUE_SIZE=0`: No limit on queue size. Slotted items scale to 200,000+ tracks.
- `MAX_PER_USER=0`: No cap on tracks queued per user.
- `MAX_PLAYLIST_TRACKS=0`: Full playlist/album imports with streaming 200-track chunks.
- `MAX_TRACK_SECONDS=0`: No duration cap on individual tracks.
- `PLAY_COOLDOWN=0.0`: No cooldown between play requests.
- `MAX_FAVORITES_PER_USER=0`: Unlimited personal favorites.
- `MAX_PLAYLISTS_PER_USER=0`: Unlimited personal playlists.
- `MAX_TRACKS_PER_PLAYLIST=0`: Unlimited tracks per personal playlist.
- `MAX_CONCURRENT_LOADS=64`: High-throughput concurrent Lavalink track loading.
- `PER_GUILD_CONCURRENT_LOADS=4`: Guild-level concurrent load limit.

## Sharding & Multi-Node Lavalink

- **Automatic Sharding (`AutoShardedBot`):** The bot automatically manages gateway shards according to server count. Shard reconnects and guild leave events are handled safely without dropping other shards.
- **Multi-Node Lavalink (`LAVALINK_NODES`):** Configure multiple Lavalink nodes via JSON in `.env`:
  ```bash
  LAVALINK_NODES=[{"name":"node-1","host":"10.0.0.1","port":2333,"password":"secret","region":"us","secure":false},{"name":"node-2","host":"10.0.0.2","port":2333,"password":"secret","region":"us","secure":false}]
  ```
  - **Load Balancing:** New players are assigned to the node with the lowest player count and lowest penalty score.
  - **Failover:** If a node drops, players automatically migrate to an active node and resume playback from their exact position.
  - **Fallback:** If `LAVALINK_NODES` is not set, the bot seamlessly falls back to the legacy single-node variables (`LAVALINK_HOST`, `LAVALINK_PORT`, etc.).

## Gateway Tuning & Speedups

- **`MENTION_REPLY_ENABLED`:** Defaults to `true`. When set to `false`, the `guild_messages` intent and message event handler are dropped entirely, eliminating message processing overhead for massive server counts.
- **C-Extensions & Speedups:** Windows wheels for `orjson`, `aiodns`, `Brotli`, `pycares`, and `backports.zstd` are installed and integrated for ultra-fast JSON serialization and asynchronous DNS resolution.
- **Minimal Caching:** Message cache is disabled (`max_messages=None`), and member caching only retains voice-active members (`chunk_guilds_at_startup=False`).

## Central Rate Limiter (data/rate_limits.json)

Rate limiting is **disabled by default** (`"enabled": false` in `data/rate_limits.json`), providing a zero-overhead fast path:
```json
{
  "enabled": false,
  "buckets": {
    "commands": {"rate": 5, "per": 10.0, "enabled": false},
    "play": {"rate": 4, "per": 10.0, "enabled": false},
    "queue": {"rate": 8, "per": 10.0, "enabled": false},
    "library": {"rate": 6, "per": 10.0, "enabled": false},
    "views": {"rate": 4, "per": 10.0, "enabled": false},
    "components": {"rate": 3, "per": 3.0, "enabled": false},
    "guild": {"rate": 30, "per": 10.0, "enabled": false}
  },
  "max_keys": 10000,
  "idle_ttl": 60.0
}
```
When enabled, token buckets track entities with bounded memory and LRU eviction. Exceeded limits respond ephemerally with `**Rate limited** • `Try again in Ns``.

## Status & Timing Diagnostics

The `/status` slash command provides complete observability:
- **Public fields:** Gateway latency, bot uptime, active players, Lavalink node connection and resource stats, voice channel bitrate vs server maximum, and event loop lag (p50/p95/p99).
- **Owner-only diagnostics:** Process memory (RSS), supervised task count, player registry size, cache sizes, Discord HTTP 429 rate limit counter, and per-command latency percentiles (p50 and p95 for acknowledgment latency and total execution duration).

## Message Catalog Patterns

All user-facing messages strictly follow three consistent patterns:
- **Pattern A (Actions):** `Action **Subject** • `Detail``
- **Pattern B (Errors & Denials):** `**Problem** • `What to do``
  - `**Rate limited** • `Try again in 3s``
  - `**This menu expired** • `Run the command again``
  - `**Not your menu** • `Run the command yourself``
  - `**Command outdated** • `Try again in a moment``
  - `**Server is busy** • `Try again in a few seconds``
  - `**Took too long** • `Try again``
- **Pattern C (Card Rows):** `**Label:** `value``

## Style & Rule Exceptions

- **Typography & Encoding:** The bullet character `•` (U+2022) is permitted in messages and code. The Unicode characters U+1F50E (magnifying glass) and U+1F55B (twelve o'clock) are permitted exclusively in `utils/autocomplete.py` as autocomplete choice display prefixes. All other characters must remain standard ASCII. Log handlers and file I/O enforce UTF-8.
- **Custom Emojis:** Custom emojis (the `<:name:id>` markup, loaded only from `data/emojis.json`) are allowed in exactly three places: the Now Playing card buttons, the Now Playing header, and the voice channel status. Nowhere else.
- **Button Styles:** All buttons across all cards use `ButtonStyle.secondary`, with the sole exception of the `Stop` button on the Now Playing card which uses `ButtonStyle.danger`.

## Verification & Benchmarks

Run the complete verification protocol, soak test, and scale benchmark:

```powershell
# 1. Compilation & Type Integrity
.venv\Scripts\python.exe -m compileall -q .

# 2. Complete Test Suite (238 tests)
.venv\Scripts\python.exe -m pytest

# 3. Linter & Style Consistency
.venv\Scripts\ruff.exe check .

# 4. Phase 8 Invariants & Style Audit
.venv\Scripts\python.exe scripts/verify_phase8.py

# 5. Soak Test (100,000 track unlimited queues, returns to baseline)
.venv\Scripts\python.exe -m tests.run_soak --cycles 500 --guilds 25

# 6. Scale Benchmark (2,000 simulated active guilds)
.venv\Scripts\python.exe tests/run_scale.py --guilds 2000
```

