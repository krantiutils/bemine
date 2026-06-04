from __future__ import annotations

import asyncio
from pathlib import Path

from mayor_discord import send
from mayor_discord.config import Settings


def test_send_message_splits_long_text_and_replies_only_first_chunk(monkeypatch, tmp_path: Path) -> None:
    sent_payloads: list[dict] = []

    class FakeSession:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    async def fake_post_json(session, url, headers, payload):
        sent_payloads.append(payload)
        return {"id": f"msg-{len(sent_payloads)}"}

    monkeypatch.setattr(send.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(send, "_post_json", fake_post_json)
    settings = Settings(
        token="discord-token",
        home_channel_id="home",
        allowed_users=set(),
        allow_all_users=True,
        mayor_home=tmp_path,
        db_path=tmp_path / "bemine.db",
        inbox_dir=tmp_path / "inbox",
    )

    result = asyncio.run(
        send.send_message(
            settings=settings,
            channel_id="channel-1",
            text=("a" * send.MAX_DISCORD_CONTENT) + "\n" + ("b" * 10),
            files=[],
            reply_to="reply-1",
        )
    )

    assert result == {"id": "msg-2"}
    assert len(sent_payloads) == 2
    assert all(len(payload["content"]) <= send.MAX_DISCORD_CONTENT for payload in sent_payloads)
    assert sent_payloads[0]["message_reference"]["message_id"] == "reply-1"
    assert "message_reference" not in sent_payloads[1]
