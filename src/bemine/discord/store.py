from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


SCHEMA = """
CREATE TABLE IF NOT EXISTS inbound_messages (
    message_id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    channel_name TEXT,
    category_name TEXT,
    guild_id TEXT,
    author_id TEXT NOT NULL,
    author_name TEXT NOT NULL,
    content TEXT NOT NULL,
    is_dm INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    received_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS inbound_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL,
    attachment_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    content_type TEXT,
    size INTEGER,
    url TEXT NOT NULL,
    saved_path TEXT NOT NULL,
    is_image INTEGER NOT NULL,
    is_voice INTEGER NOT NULL,
    FOREIGN KEY(message_id) REFERENCES inbound_messages(message_id)
);

CREATE TABLE IF NOT EXISTS outbound_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_message_id TEXT,
    channel_id TEXT NOT NULL,
    content TEXT NOT NULL,
    files_json TEXT NOT NULL,
    sent_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS discord_channels (
    channel_id TEXT PRIMARY KEY,
    guild_id TEXT,
    channel_name TEXT NOT NULL,
    category_name TEXT NOT NULL,
    channel_type INTEGER,
    updated_at REAL NOT NULL
);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            _ensure_column(conn, "inbound_messages", "channel_name", "TEXT")
            _ensure_column(conn, "inbound_messages", "category_name", "TEXT")

    def record_inbound_message(
        self,
        *,
        message_id: str,
        channel_id: str,
        guild_id: str | None,
        author_id: str,
        author_name: str,
        content: str,
        is_dm: bool,
        created_at: str,
        channel_name: str = "",
        category_name: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO inbound_messages (
                    message_id, channel_id, channel_name, category_name, guild_id,
                    author_id, author_name, content, is_dm, created_at, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    channel_id,
                    channel_name,
                    category_name,
                    guild_id,
                    author_id,
                    author_name,
                    content,
                    1 if is_dm else 0,
                    created_at,
                    time.time(),
                ),
            )
            if channel_name or category_name:
                _upsert_channel_metadata(
                    conn,
                    channel_id=channel_id,
                    guild_id=guild_id or "",
                    channel_name=channel_name,
                    category_name=category_name,
                    channel_type=None,
                    updated_at=time.time(),
                )

    def record_channel_metadata(
        self,
        *,
        channel_id: str,
        guild_id: str = "",
        channel_name: str,
        category_name: str = "",
        channel_type: int | None = None,
    ) -> None:
        with self._connect() as conn:
            _upsert_channel_metadata(
                conn,
                channel_id=channel_id,
                guild_id=guild_id,
                channel_name=channel_name,
                category_name=category_name,
                channel_type=channel_type,
                updated_at=time.time(),
            )

    def local_channel_ids(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT channel_id FROM inbound_messages
                UNION
                SELECT channel_id FROM outbound_messages
                ORDER BY channel_id
                """
            ).fetchall()
        return [str(row["channel_id"]) for row in rows]

    def channel_metadata(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT channel_id, guild_id, channel_name, category_name,
                       channel_type, updated_at
                FROM discord_channels
                ORDER BY category_name, channel_name, channel_id
                """
            ).fetchall()
        return [_row_dict(row) for row in rows]

    def record_attachment(
        self,
        *,
        message_id: str,
        attachment_id: str,
        filename: str,
        content_type: str | None,
        size: int | None,
        url: str,
        saved_path: Path,
        is_image: bool,
        is_voice: bool,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO inbound_attachments (
                    message_id, attachment_id, filename, content_type, size,
                    url, saved_path, is_image, is_voice
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    attachment_id,
                    filename,
                    content_type,
                    size,
                    url,
                    str(saved_path),
                    1 if is_image else 0,
                    1 if is_voice else 0,
                ),
            )

    def record_outbound(
        self,
        *,
        discord_message_id: str | None,
        channel_id: str,
        content: str,
        files: list[Path],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO outbound_messages (
                    discord_message_id, channel_id, content, files_json, sent_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    discord_message_id,
                    channel_id,
                    content,
                    json.dumps([str(path) for path in files]),
                    time.time(),
                ),
            )

    def recent_inbound_messages(
        self,
        *,
        channel_id: str,
        before_message_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        with self._connect() as conn:
            current = conn.execute(
                "SELECT received_at FROM inbound_messages WHERE message_id = ?",
                (before_message_id,),
            ).fetchone()
            before_received_at = (
                float(current["received_at"]) if current is not None else time.time()
            )
            rows = conn.execute(
                """
                SELECT message_id, author_name, content, created_at, received_at
                FROM inbound_messages
                WHERE channel_id = ?
                  AND message_id != ?
                  AND received_at <= ?
                ORDER BY received_at DESC
                LIMIT ?
                """,
                (channel_id, before_message_id, before_received_at, limit),
            ).fetchall()
        return [_row_dict(row) for row in reversed(rows)]

    def recent_channel_messages(
        self,
        *,
        channel_id: str,
        before_message_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        with self._connect() as conn:
            current = conn.execute(
                "SELECT received_at FROM inbound_messages WHERE message_id = ?",
                (before_message_id,),
            ).fetchone()
            before_at = float(current["received_at"]) if current else time.time()
            rows = conn.execute(
                """
                SELECT *
                FROM (
                    SELECT
                        message_id AS message_id,
                        channel_id AS channel_id,
                        COALESCE(channel_name, '') AS channel_name,
                        COALESCE(category_name, '') AS category_name,
                        author_name AS author_name,
                        content AS content,
                        created_at AS created_at,
                        received_at AS sort_at,
                        'user' AS role
                    FROM inbound_messages
                    WHERE channel_id = ?
                      AND message_id != ?
                      AND received_at <= ?
                    UNION ALL
                    SELECT
                        COALESCE(discord_message_id, 'outbound:' || id) AS message_id,
                        channel_id AS channel_id,
                        NULL AS channel_name,
                        NULL AS category_name,
                        'balen' AS author_name,
                        content AS content,
                        datetime(sent_at, 'unixepoch', 'localtime') AS created_at,
                        sent_at AS sort_at,
                        'assistant' AS role
                    FROM outbound_messages
                    WHERE channel_id = ?
                      AND sent_at <= ?
                )
                ORDER BY sort_at DESC
                LIMIT ?
                """,
                (
                    channel_id,
                    before_message_id,
                    before_at,
                    channel_id,
                    before_at,
                    limit,
                ),
            ).fetchall()
        return [_row_dict(row) for row in reversed(rows)]

    def channel_summaries(
        self,
        *,
        limit: int = 25,
        query: str = "",
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        query = query.strip()
        pattern = _like_pattern(query)
        with self._connect() as conn:
            rows = conn.execute(
                """
                WITH events AS (
                    SELECT
                        message_id AS message_id,
                        inbound_messages.channel_id AS channel_id,
                        COALESCE(NULLIF(inbound_messages.channel_name, ''), dc.channel_name, '') AS channel_name,
                        COALESCE(NULLIF(inbound_messages.category_name, ''), dc.category_name, '') AS category_name,
                        inbound_messages.guild_id AS guild_id,
                        author_name AS author_name,
                        content AS content,
                        created_at AS created_at,
                        received_at AS sort_at,
                        'user' AS role
                    FROM inbound_messages
                    LEFT JOIN discord_channels dc ON dc.channel_id = inbound_messages.channel_id
                    UNION ALL
                    SELECT
                        COALESCE(discord_message_id, 'outbound:' || id) AS message_id,
                        outbound_messages.channel_id AS channel_id,
                        COALESCE(dc.channel_name, '') AS channel_name,
                        COALESCE(dc.category_name, '') AS category_name,
                        dc.guild_id AS guild_id,
                        'balen' AS author_name,
                        content AS content,
                        datetime(sent_at, 'unixepoch', 'localtime') AS created_at,
                        sent_at AS sort_at,
                        'assistant' AS role
                    FROM outbound_messages
                    LEFT JOIN discord_channels dc ON dc.channel_id = outbound_messages.channel_id
                ),
                filtered AS (
                    SELECT *
                    FROM events
                    WHERE ? = ''
                       OR channel_id LIKE ?
                       OR channel_name LIKE ?
                       OR category_name LIKE ?
                       OR author_name LIKE ?
                       OR content LIKE ?
                ),
                ranked AS (
                    SELECT
                        *,
                        ROW_NUMBER() OVER (
                            PARTITION BY channel_id ORDER BY sort_at DESC
                        ) AS rn,
                        COUNT(*) OVER (PARTITION BY channel_id) AS total_messages,
                        SUM(CASE WHEN role = 'user' THEN 1 ELSE 0 END)
                            OVER (PARTITION BY channel_id) AS inbound_messages,
                        SUM(CASE WHEN role = 'assistant' THEN 1 ELSE 0 END)
                            OVER (PARTITION BY channel_id) AS outbound_messages,
                        MAX(guild_id) OVER (PARTITION BY channel_id) AS latest_guild_id,
                        MAX(channel_name) OVER (PARTITION BY channel_id) AS latest_channel_name,
                        MAX(category_name) OVER (PARTITION BY channel_id) AS latest_category_name
                    FROM filtered
                )
                SELECT
                    ranked.channel_id,
                    COALESCE(NULLIF(latest_channel_name, ''), discord_channels.channel_name, '') AS channel_name,
                    COALESCE(NULLIF(latest_category_name, ''), discord_channels.category_name, '') AS category_name,
                    latest_guild_id AS guild_id,
                    total_messages,
                    inbound_messages,
                    outbound_messages,
                    message_id AS latest_message_id,
                    author_name AS latest_author_name,
                    content AS latest_content,
                    created_at AS latest_created_at,
                    sort_at AS latest_sort_at,
                    role AS latest_role
                FROM ranked
                LEFT JOIN discord_channels ON discord_channels.channel_id = ranked.channel_id
                WHERE rn = 1
                ORDER BY latest_sort_at DESC
                LIMIT ?
                """,
                (query, pattern, pattern, pattern, pattern, pattern, limit),
            ).fetchall()
        return [_row_dict(row) for row in rows]

    def transcript_messages(
        self,
        *,
        channel_id: str = "",
        query: str = "",
        limit: int = 40,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        channel_id = channel_id.strip()
        query = query.strip()
        pattern = _like_pattern(query)
        with self._connect() as conn:
            rows = conn.execute(
                """
                WITH channel_meta AS (
                    SELECT
                        channel_id,
                        MAX(channel_name) AS channel_name,
                        MAX(category_name) AS category_name
                    FROM (
                        SELECT channel_id, channel_name, category_name FROM discord_channels
                        UNION ALL
                        SELECT channel_id, channel_name, category_name FROM inbound_messages
                    )
                    GROUP BY channel_id
                )
                SELECT *
                FROM (
                    SELECT
                        message_id AS message_id,
                        channel_id AS channel_id,
                        channel_name AS channel_name,
                        category_name AS category_name,
                        guild_id AS guild_id,
                        author_name AS author_name,
                        content AS content,
                        created_at AS created_at,
                        received_at AS sort_at,
                        'user' AS role
                    FROM inbound_messages
                    WHERE (? = '' OR channel_id = ?)
                      AND (
                          ? = ''
                          OR channel_id LIKE ?
                          OR channel_name LIKE ?
                          OR category_name LIKE ?
                          OR author_name LIKE ?
                          OR content LIKE ?
                      )
                    UNION ALL
                    SELECT
                        COALESCE(discord_message_id, 'outbound:' || id) AS message_id,
                        outbound_messages.channel_id AS channel_id,
                        channel_meta.channel_name AS channel_name,
                        channel_meta.category_name AS category_name,
                        NULL AS guild_id,
                        'balen' AS author_name,
                        content AS content,
                        datetime(sent_at, 'unixepoch', 'localtime') AS created_at,
                        sent_at AS sort_at,
                        'assistant' AS role
                    FROM outbound_messages
                    LEFT JOIN channel_meta ON channel_meta.channel_id = outbound_messages.channel_id
                    WHERE (? = '' OR outbound_messages.channel_id = ?)
                      AND (
                          ? = ''
                          OR outbound_messages.channel_id LIKE ?
                          OR channel_meta.channel_name LIKE ?
                          OR channel_meta.category_name LIKE ?
                          OR 'balen' LIKE ?
                          OR content LIKE ?
                      )
                )
                ORDER BY sort_at DESC
                LIMIT ?
                """,
                (
                    channel_id,
                    channel_id,
                    query,
                    pattern,
                    pattern,
                    pattern,
                    pattern,
                    pattern,
                    channel_id,
                    channel_id,
                    query,
                    pattern,
                    pattern,
                    pattern,
                    pattern,
                    pattern,
                    limit,
                ),
            ).fetchall()
        return [_row_dict(row) for row in reversed(rows)]

    def stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            inbound = conn.execute("SELECT COUNT(*) FROM inbound_messages").fetchone()[0]
            attachments = conn.execute("SELECT COUNT(*) FROM inbound_attachments").fetchone()[0]
            outbound = conn.execute("SELECT COUNT(*) FROM outbound_messages").fetchone()[0]
        return {
            "inbound_messages": inbound,
            "inbound_attachments": attachments,
            "outbound_messages": outbound,
        }


def _like_pattern(query: str) -> str:
    cleaned = query.strip()
    return f"%{cleaned}%" if cleaned else "%"


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _upsert_channel_metadata(
    conn: sqlite3.Connection,
    *,
    channel_id: str,
    guild_id: str,
    channel_name: str,
    category_name: str,
    channel_type: int | None,
    updated_at: float,
) -> None:
    if not channel_id or not channel_name:
        return
    conn.execute(
        """
        INSERT INTO discord_channels (
            channel_id, guild_id, channel_name, category_name, channel_type, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(channel_id) DO UPDATE SET
            guild_id = excluded.guild_id,
            channel_name = excluded.channel_name,
            category_name = excluded.category_name,
            channel_type = excluded.channel_type,
            updated_at = excluded.updated_at
        """,
        (channel_id, guild_id, channel_name, category_name, channel_type, updated_at),
    )
