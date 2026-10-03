"""Export the entire SQLite database to a JSON file on demand."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import time

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "bot.db"


def export_database(db_path: Path | str, output_path: Path | str | None = None) -> Path:
    db = Path(db_path)
    if not db.exists():
        raise FileNotFoundError(f"Database file not found at {db}")

    if output_path is None:
        timestamp = int(time.time())
        dest = db.parent / f"export_{timestamp}.json"
    else:
        dest = Path(output_path)

    dest.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db))
    try:
        cur = conn.cursor()
        export_data: dict[str, list[dict[str, object]]] = {
            "guild_settings": [],
            "favorites": [],
            "playlists": [],
            "queue_snapshots": [],
        }

        # guild_settings
        cur.execute("SELECT * FROM guild_settings")
        cols = [d[0] for d in cur.description]
        for row in cur.fetchall():
            export_data["guild_settings"].append(dict(zip(cols, row)))

        # favorites
        cur.execute("SELECT * FROM favorites")
        cols = [d[0] for d in cur.description]
        for row in cur.fetchall():
            export_data["favorites"].append(dict(zip(cols, row)))

        # playlists and tracks
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
        export_data["playlists"] = playlists

        # queue_snapshots
        cur.execute("SELECT * FROM queue_snapshots")
        cols = [d[0] for d in cur.description]
        for row in cur.fetchall():
            export_data["queue_snapshots"].append(dict(zip(cols, row)))

        with open(dest, "w", encoding="utf-8") as f:
            json.dump(export_data, f, indent=2)

        return dest
    finally:
        conn.close()


def main() -> None:
    target_out = sys.argv[1] if len(sys.argv) > 1 else None
    out = export_database(DEFAULT_DB_PATH, target_out)
    print(f"Exported database to {out}")


if __name__ == "__main__":
    main()
