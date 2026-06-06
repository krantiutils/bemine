from __future__ import annotations

import asyncio
from pathlib import Path

from mayor_discord.dispatch import (
    DispatchProgress,
    DispatchResult,
    ProjectChannel,
    ProjectRoute,
)
import mayor_discord.gateway as gw
from mayor_discord.gateway import (
    ACK_REACTION,
    SUCCESS_REACTION,
    WORKING_REACTION,
    _acknowledge_message,
    _best_effort_typing,
    _discord_chunk,
    _dispatch_progress_text,
    _dispatch_response_text,
    _format_conversation_context,
)
from mayor_discord.store import Store


class FakeMessage:
    def __init__(self) -> None:
        self.reactions: list[str] = []

    async def add_reaction(self, emoji: str) -> None:
        self.reactions.append(emoji)


class FakeTyping:
    def __init__(self) -> None:
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> None:
        self.entered = True

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.exited = True


class FakeChannel:
    def __init__(self) -> None:
        self.context = FakeTyping()

    def typing(self) -> FakeTyping:
        return self.context


def test_acknowledge_message_adds_receipt_reaction() -> None:
    message = FakeMessage()

    acknowledged = asyncio.run(_acknowledge_message(message))

    assert acknowledged is True
    assert message.reactions == [ACK_REACTION]


def test_acknowledge_message_is_best_effort() -> None:
    acknowledged = asyncio.run(_acknowledge_message(object()))  # type: ignore[arg-type]

    assert acknowledged is False


def test_best_effort_typing_enters_and_exits_context() -> None:
    channel = FakeChannel()

    async def run() -> None:
        async with _best_effort_typing(channel):  # type: ignore[arg-type]
            assert channel.context.entered is True
            assert channel.context.exited is False

    asyncio.run(run())

    assert channel.context.exited is True


def test_best_effort_typing_ignores_missing_typing_support() -> None:
    async def run() -> str:
        async with _best_effort_typing(object()):  # type: ignore[arg-type]
            return "handled"

    assert asyncio.run(run()) == "handled"


def test_dispatch_response_omits_success_header() -> None:
    project = ProjectChannel(channel_name="bemine", cwd=Path("/tmp"))
    route = ProjectRoute(project=project, reason="default")
    result = DispatchResult(
        ok=True, project=project, output="done", elapsed_seconds=1.0
    )

    assert _dispatch_response_text(result, route) == "done"


def test_dispatch_response_keeps_failure_context() -> None:
    project = ProjectChannel(channel_name="bemine", cwd=Path("/tmp"), runtime="kimi")
    route = ProjectRoute(project=project, reason="default")
    result = DispatchResult(
        ok=False, project=project, output="failed", elapsed_seconds=1.0
    )

    assert (
        _dispatch_response_text(result, route)
        == "Kimi dispatch issue for `#bemine`:\nfailed"
    )


def test_dispatch_progress_text_uses_runtime_summary() -> None:
    progress = DispatchProgress(
        runtime="codex",
        event_type="item.started",
        summary="💻 shell: `pwd`",
        raw_event={},
    )

    assert _dispatch_progress_text(progress) == "💻 shell: `pwd`"


def test_discord_chunk_closes_and_reopens_markdown_fences() -> None:
    text = "before\n```python\n" + "\n".join(f"print({index})" for index in range(20)) + "\n```\nafter"

    chunks = _discord_chunk(text, limit=90)

    assert len(chunks) > 1
    assert all(chunk.count("```") % 2 == 0 for chunk in chunks)
    assert chunks[0].endswith("```")
    assert chunks[1].startswith("```")


def test_store_recent_inbound_messages_are_channel_scoped_and_ordered(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "bemine.db")
    for index, (message_id, channel_id, content) in enumerate(
        [
            ("m1", "chan-a", "first"),
            ("m2", "chan-b", "other channel"),
            ("m3", "chan-a", "second"),
            ("m4", "chan-a", "current"),
        ]
    ):
        store.record_inbound_message(
            message_id=message_id,
            channel_id=channel_id,
            guild_id=None,
            author_id="user",
            author_name="voidash",
            content=content,
            is_dm=True,
            created_at=f"2026-06-05T09:0{index}:00+05:45",
        )

    rows = store.recent_inbound_messages(
        channel_id="chan-a",
        before_message_id="m4",
        limit=2,
    )

    assert [row["message_id"] for row in rows] == ["m1", "m3"]
    assert "other channel" not in _format_conversation_context(rows)


def test_store_recent_channel_messages_include_bot_replies_in_order(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "bemine.db")
    store.record_inbound_message(
        message_id="m1",
        channel_id="chan-a",
        guild_id=None,
        author_id="user",
        author_name="voidash",
        content="show tts status",
        is_dm=True,
        created_at="2026-06-05T09:00:00+05:45",
    )
    store.record_outbound(
        discord_message_id="b1",
        channel_id="chan-a",
        content="TTS status reply",
        files=[],
    )
    store.record_inbound_message(
        message_id="m2",
        channel_id="chan-a",
        guild_id=None,
        author_id="user",
        author_name="voidash",
        content="what is this?",
        is_dm=True,
        created_at="2026-06-05T09:01:00+05:45",
    )

    rows = store.recent_channel_messages(
        channel_id="chan-a",
        before_message_id="m2",
        limit=4,
    )
    context = _format_conversation_context(rows)

    assert [row["message_id"] for row in rows] == ["m1", "b1"]
    assert rows[-1]["role"] == "assistant"
    assert "balen (bot)" in context
    assert "TTS status reply" in context


def test_conversation_context_redacts_tokens_and_keeps_recent_tail() -> None:
    rows = [
        {
            "author_name": "voidash",
            "created_at": "2026-06-05T09:00:00+05:45",
            "content": "old message with ghp_abcdefghijklmnopqrstuvwxyz",
        },
        {
            "author_name": "voidash",
            "created_at": "2026-06-05T09:01:00+05:45",
            "content": "new message to keep",
        },
    ]

    context = _format_conversation_context(rows, limit=80)

    assert "ghp_abcdefghijklmnopqrstuvwxyz" not in context
    assert "new message to keep" in context


class _FakeAuthor:
    def __init__(self) -> None:
        self.id = 4242
        self.global_name = "voidash"

    def __str__(self) -> str:
        return "voidash#0001"


class _PassthroughChannel:
    def __init__(self) -> None:
        self.id = 99001
        self.name = "general"
        self.category = None
        self.type = "text"
        self.sent: list[str] = []

    async def send(self, content: str):
        self.sent.append(content)
        return _SentMessage()


class _SentMessage:
    id = 77777


class _PassthroughMessage:
    def __init__(self, content: str) -> None:
        self.id = 555
        self.content = content
        self.channel = _PassthroughChannel()
        self.guild = None
        self.author = _FakeAuthor()
        self.attachments: list[object] = []
        self.created_at = _FakeCreatedAt()
        self.reactions: list[str] = []
        self.replies: list[str] = []

    async def add_reaction(self, emoji: str) -> None:
        self.reactions.append(emoji)

    async def reply(self, content: str, mention_author: bool = False):
        self.replies.append(content)
        return _SentMessage()


class _FakeCreatedAt:
    def isoformat(self) -> str:
        return "2026-06-05T09:00:00+05:45"


def _make_gateway(tmp_path: Path):
    from mayor_discord.config import Settings
    from mayor_discord.gateway import MayorDiscordGateway

    settings = Settings(
        token="dummy-token",
        home_channel_id="",
        allowed_users={"4242"},
        allow_all_users=True,
        mayor_home=tmp_path,
        db_path=tmp_path / "bemine.db",
        inbox_dir=tmp_path / "inbox",
    )
    return MayorDiscordGateway(settings)


def test_novel_plain_message_routes_to_agent(tmp_path: Path, monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(settings, message, normalized, saved_attachments=None,
                 progress_callback=None):
        seen["normalized"] = normalized
        seen["saved_attachments"] = saved_attachments
        return "AGENT REPLY"

    monkeypatch.setattr(gw, "run_bound_agent", fake_run)

    gateway = _make_gateway(tmp_path)
    message = _PassthroughMessage("tell me a joke about kubernetes")

    asyncio.run(gateway._handle_allowed_message(message))

    # A novel plain message reached the deterministic agent, not a classifier.
    assert seen["normalized"] == "tell me a joke about kubernetes"
    # The agent's reply was streamed back to the channel.
    assert message.replies == ["AGENT REPLY"]
    assert WORKING_REACTION in message.reactions
    assert SUCCESS_REACTION in message.reactions


def test_empty_message_does_not_route_to_agent(tmp_path: Path, monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(settings, message, normalized, saved_attachments=None,
                 progress_callback=None):
        seen["normalized"] = normalized
        return "SHOULD NOT HAPPEN"

    monkeypatch.setattr(gw, "run_bound_agent", fake_run)

    gateway = _make_gateway(tmp_path)

    async def _noop_process(_message):
        return None

    monkeypatch.setattr(gateway.bot, "process_commands", _noop_process)
    message = _PassthroughMessage("")  # empty content, no attachments

    asyncio.run(gateway._handle_allowed_message(message))

    # Empty message with no attachments must NOT reach the agent.
    assert "normalized" not in seen
    assert message.replies == []
