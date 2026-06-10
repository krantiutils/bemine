from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from bemine.cli import main
from bemine.discord.store import Store
from bemine.discord.transcript import render_channel_summaries, render_transcript
from bemine.index.status import _redact


def _record_message(
    store: Store,
    *,
    message_id: str,
    channel_id: str,
    content: str,
    author_name: str = "voidash",
) -> None:
    store.record_inbound_message(
        message_id=message_id,
        channel_id=channel_id,
        channel_name=channel_id.replace("chan-", ""),
        category_name="Projects",
        guild_id="guild-1",
        author_id="user-1",
        author_name=author_name,
        content=content,
        is_dm=False,
        created_at=f"2026-06-10T10:0{message_id[-1]}:00+05:45",
    )


def test_store_summarizes_and_searches_local_discord_threads(tmp_path: Path) -> None:
    store = Store(tmp_path / "bemine.db")
    _record_message(
        store,
        message_id="m1",
        channel_id="chan-rai",
        content="goal rai-speech-toolkit: train checkpoint",
    )
    store.record_outbound(
        discord_message_id="b1",
        channel_id="chan-rai",
        content="Started goal worker goal-rai.",
        files=[],
    )
    _record_message(
        store,
        message_id="m2",
        channel_id="chan-other",
        content="unrelated",
    )

    summaries = store.channel_summaries(query="rai", limit=10)
    assert [row["channel_id"] for row in summaries] == ["chan-rai"]
    assert summaries[0]["total_messages"] == 2
    assert summaries[0]["inbound_messages"] == 1
    assert summaries[0]["outbound_messages"] == 1

    transcript = store.transcript_messages(channel_id="chan-rai", limit=5)
    assert [row["message_id"] for row in transcript] == ["m1", "b1"]
    assert transcript[-1]["role"] == "assistant"

    rendered = render_channel_summaries(summaries) + render_transcript(transcript)
    assert "chan-rai" in rendered
    assert "#Projects/rai" in rendered
    assert "Started goal worker" in rendered
    assert "chan-other" not in rendered


def test_transcript_rendering_redacts_assignment_style_secrets() -> None:
    rows = [
        {
            "channel_id": "chan-rai",
            "created_at": "2026-06-10T10:00:00+05:45",
            "author_name": "voidash",
            "role": "user",
            "content": "retry with SSHPASS='codingjokers' and TOKEN=abc123",
        }
    ]

    rendered = render_transcript(rows)

    assert "codingjokers" not in rendered
    assert "abc123" not in rendered
    assert "SSHPASS='<secret>'" in rendered
    assert "TOKEN=<secret>" in rendered
    assert _redact("password: letmein") == "password: <secret>"


def test_cli_discord_threads_and_transcript_read_local_store(
    tmp_path: Path,
    capsys,
) -> None:
    state_dir = tmp_path / "state"
    vault_dir = tmp_path / "vault"
    store = Store(state_dir / "bemine.db")
    _record_message(
        store,
        message_id="m1",
        channel_id="chan-voidash",
        content="SMM idea: show Rai checkpoint progress with SSHPASS='letmein'",
    )
    store.record_outbound(
        discord_message_id="b1",
        channel_id="chan-voidash",
        content="Added SMM backlog item.",
        files=[],
    )

    main(
        [
            "--state-dir",
            str(state_dir),
            "--vault",
            str(vault_dir),
            "discord",
            "threads",
            "--query",
            "rai",
            "--json",
        ]
    )
    threads = json.loads(capsys.readouterr().out)
    assert threads[0]["channel_id"] == "chan-voidash"
    assert threads[0]["channel_name"] == "voidash"
    assert "letmein" not in json.dumps(threads)
    assert "SSHPASS='<secret>'" in threads[0]["latest_content"]

    main(
        [
            "--state-dir",
            str(state_dir),
            "--vault",
            str(vault_dir),
            "discord",
            "transcript",
            "--channel-id",
            "chan-voidash",
            "--limit",
            "5",
        ]
    )
    text = capsys.readouterr().out
    assert "Local Discord transcript:" in text
    assert "#Projects/voidash" in text
    assert "SMM idea" in text
    assert "Added SMM backlog item." in text
    assert "letmein" not in text
    assert "SSHPASS='<secret>'" in text


def test_store_migrates_existing_discord_messages_without_channel_names(tmp_path: Path) -> None:
    db_path = tmp_path / "bemine.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE inbound_messages (
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
            CREATE TABLE outbound_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_message_id TEXT,
                channel_id TEXT NOT NULL,
                content TEXT NOT NULL,
                files_json TEXT NOT NULL,
                sent_at REAL NOT NULL
            );
            CREATE TABLE inbound_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT NOT NULL,
                attachment_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                content_type TEXT,
                size INTEGER,
                url TEXT NOT NULL,
                saved_path TEXT NOT NULL,
                is_image INTEGER NOT NULL,
                is_voice INTEGER NOT NULL
            );
            INSERT INTO inbound_messages VALUES (
                'm1', 'chan-old', 'guild-1', 'user-1', 'voidash',
                'old message', 0, '2026-06-10T10:00:00+05:45', 1.0
            );
            """
        )

    store = Store(db_path)
    cols = {
        row[1]
        for row in sqlite3.connect(db_path).execute("PRAGMA table_info(inbound_messages)")
    }
    assert {"channel_name", "category_name"} <= cols
    assert store.channel_summaries(limit=1)[0]["channel_id"] == "chan-old"


def test_channel_metadata_labels_outbound_only_threads(tmp_path: Path) -> None:
    store = Store(tmp_path / "bemine.db")
    store.record_outbound(
        discord_message_id="b1",
        channel_id="chan-digest",
        content="daily digest",
        files=[],
    )
    store.record_channel_metadata(
        channel_id="chan-digest",
        guild_id="guild-1",
        channel_name="daily-digest",
        category_name="Ops",
        channel_type=0,
    )

    rendered = render_channel_summaries(store.channel_summaries(limit=5))

    assert "#Ops/daily-digest" in rendered
    assert "chan-digest" in rendered
    assert store.local_channel_ids() == ["chan-digest"]


def test_fetch_guild_channel_metadata_maps_parent_category(monkeypatch) -> None:
    from bemine.discord import channel_index

    monkeypatch.setattr(
        channel_index,
        "_discord_json",
        lambda token, url: [
            {"id": "cat-1", "name": "Projects", "type": 4},
            {"id": "chan-1", "name": "voidash", "type": 0, "parent_id": "cat-1"},
            {"id": "chan-2", "name": "dm-ish", "type": 0, "parent_id": None},
        ],
    )

    rows = channel_index.fetch_guild_channel_metadata("token", "guild-1")

    assert rows[0].channel_name == "Projects"
    assert rows[1].channel_name == "voidash"
    assert rows[1].category_name == "Projects"


def test_default_guild_id_prefers_voidash(monkeypatch) -> None:
    from bemine.discord import channel_index

    monkeypatch.setattr(
        channel_index,
        "_discord_json",
        lambda token, url: [
            {"id": "guild-other", "name": "Other"},
            {"id": "guild-voidash", "name": "voidash"},
        ],
    )

    assert channel_index.default_guild_id("token") == "guild-voidash"


def test_refresh_channel_metadata_fetches_unknown_local_thread(monkeypatch, tmp_path: Path) -> None:
    from bemine.discord import channel_index

    responses = {
        "guilds/guild-1/channels": [{"id": "cat-1", "name": "Projects", "type": 4}],
        "channels/thread-1": {
            "id": "thread-1",
            "name": "launch-plan",
            "type": 11,
            "guild_id": "guild-1",
            "parent_id": "parent-1",
        },
        "channels/parent-1": {"id": "parent-1", "name": "voidash", "type": 0},
    }

    def fake_json(token: str, url: str):
        for key, value in responses.items():
            if url.endswith(key):
                return value
        return []

    monkeypatch.setattr(channel_index, "_discord_json", fake_json)
    store = Store(tmp_path / "bemine.db")

    rows = channel_index.refresh_channel_metadata(
        store,
        token="token",
        guild_id="guild-1",
        channel_ids=["thread-1"],
    )

    assert any(row["channel_id"] == "thread-1" for row in rows)
    rendered = render_channel_summaries([
        {
            "channel_id": "thread-1",
            "channel_name": "launch-plan",
            "category_name": "voidash",
            "total_messages": 1,
            "inbound_messages": 1,
            "outbound_messages": 0,
        }
    ])
    assert "#voidash/launch-plan" in rendered
