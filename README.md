# Discord Music Bot (discord.py + lavalink.py)

[![CI](https://github.com/Sharon-ctl/Melora.py/actions/workflows/ci.yml/badge.svg)](https://github.com/Sharon-ctl/Melora.py/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/downloads/release/python-3119/)
[![Tests](https://img.shields.io/badge/tests-171%20passed-brightgreen.svg)](https://github.com/Sharon-ctl/Melora.py)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)

A slash-command-only music bot built for unattended operation. It connects to an existing Lavalink 4.x server as a client. It does not run or configure Lavalink.

- discord.py 2.7.1, lavalink.py 5.11.0, Python 3.11.9
- Intents: guilds, voice_states, guild_messages. No members or presence intents. Message content intent is disabled and message cache is turned off.
- Per-guild players, a bot-owned queue, supervised background tasks, watchdog, soak test, and Components V2 interactive cards.

## Prerequisites

- **Python:** Python 3.11+ (tested on Python 3.11.9)
- **Java:** Java 17 or higher (required to run Lavalink 4.x)
- **Discord Bot:** An application created in the Discord Developer Portal

---

## 1. Lavalink Server Setup

The bot connects to a Lavalink 4.x server as an audio client. Follow these steps to set up Lavalink:

1. Download the latest Lavalink 4.x release (`Lavalink.jar`) from the official Lavalink repository.
2. In the same directory as `Lavalink.jar`, create an `application.yml` file (you can copy `application.yml.example` provided in this repository).
3. Use the following recommended `application.yml` configuration:

```yaml
server:
  port: 2333
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

    # Audio quality
    opusEncodingQuality: 10
    resamplingQuality: HIGH

    # Smoothness
    bufferDurationMs: 600
    frameBufferDurationMs: 5000
    trackStuckThresholdMs: 10000
    useSeekGhosting: true
    playerUpdateInterval: 5

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
      refreshToken: "refresh-token-here"
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
    enabled: true
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
- `/privacy`: Explanation of what data is stored and why.
- `/reset`: Delete all your stored data (favorites and playlists) with secondary button confirmation.

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
  - Text Display 1: Bold live bot display name (`guild.me.display_name`, falling back to `client.user.name`).
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

## Central Rate Limiter (data/rate_limits.json)

The bot features a sliding-window rate limiter with bounded memory and LRU eviction, configured via `data/rate_limits.json`:

```json
{
  "buckets": {
    "commands": {"rate": 5, "per": 10.0},
    "play": {"rate": 4, "per": 10.0},
    "queue": {"rate": 8, "per": 10.0},
    "library": {"rate": 6, "per": 10.0},
    "views": {"rate": 4, "per": 10.0},
    "components": {"rate": 3, "per": 3.0},
    "guild": {"rate": 30, "per": 10.0}
  },
  "max_keys": 10000,
  "idle_ttl": 60.0
}
```

- **Choke points:** Checked atomically in `tree.interaction_check` (slash commands) and `BaseCardView.interaction_check` (buttons and selects).
- **Owner bypass:** The configured `OWNER_ID` bypasses all rate limits.
- **Single message:** Rate limits respond once, ephemerally, with `**Rate limited** • `Try again in Ns``.
- **Autocomplete:** Exceeded autocomplete limits drop silently with an empty list.

## Status & Timing Diagnostics

The `/status` slash command provides complete observability:
- **Public fields:** Gateway latency, bot uptime, active players, Lavalink node connection and resource stats, voice channel bitrate vs server maximum.
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

- **Typography & Encoding:** The bullet character `•` (U+2022) is permitted in messages and code. All other characters must remain standard ASCII. Log handlers and file I/O enforce UTF-8.
- **Custom Emojis:** Custom emojis are strictly isolated to the 5 Now Playing card buttons. No other emojis are allowed anywhere else in the bot.
- **Button Styles:** All buttons across all cards use `ButtonStyle.secondary`, with the sole exception of the `Stop` button on the Now Playing card which uses `ButtonStyle.danger`.

## Verification & Auditing

Run the test suite and audit script:

```powershell
.venv\Scripts\python.exe -m compileall -q .
.venv\Scripts\python.exe -m pytest
.venv\Scripts\ruff.exe check .
.venv\Scripts\python.exe scripts/verify_phase8.py
.venv\Scripts\python.exe -m tests.run_soak --cycles 500 --guilds 25
```

