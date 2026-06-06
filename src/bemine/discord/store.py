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
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO inbound_messages (
                    message_id, channel_id, guild_id, author_id, author_name,
                    content, is_dm, created_at, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    channel_id,
                    guild_id,
                    author_id,
                    author_name,
                    content,
                    1 if is_dm else 0,
                    created_at,
                    time.time(),
                ),
            )

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
