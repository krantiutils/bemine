from __future__ import annotations

import json
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
    assert "SMM idea" in text
    assert "Added SMM backlog item." in text
    assert "letmein" not in text
    assert "SSHPASS='<secret>'" in text
