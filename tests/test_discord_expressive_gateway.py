from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from mayor_discord.dispatch import (
    DispatchProgress,
    DispatchResult,
    ProjectChannel,
    ProjectRoute,
)
from mayor_discord.gateway import (
    ACK_REACTION,
    _acknowledge_message,
    _best_effort_typing,
    _discord_chunk,
    _dispatch_progress_text,
    _dispatch_response_text,
    _format_conversation_context,
    _local_tool_response,
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


def test_discord_local_tool_router_updates_dispatch_config(
    tmp_path: Path, monkeypatch
) -> None:
    config = tmp_path / "bemine.toml"
    config.write_text(
        """
[dispatch]
runtime = "kimi"

[kimi]
model = "kimi-k2.6"

[codex]
model = ""
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BEMINE_CONFIG", str(config))

    result = _local_tool_response(
        "switch to gpt 5.4 mini please",
        router=lambda _content, _catalog: {
            "tool": "config.update_dispatch",
            "arguments": {"runtime": "codex", "model": "gpt 5.4 mini"},
            "confidence": 0.91,
        },
    )

    assert result is not None
    assert result.tool == "config.update_dispatch"
    assert "runtime: codex" in result.text
    assert 'runtime = "codex"' in config.read_text(encoding="utf-8")
    assert 'model = "gpt-5.4-mini"' in config.read_text(encoding="utf-8")


def test_discord_local_tool_router_falls_through_to_project_dispatch() -> None:
    result = _local_tool_response(
        "fix the tests in this project",
        router=lambda _content, _catalog: {
            "tool": "project.dispatch",
            "arguments": {},
            "confidence": 0.93,
        },
    )

    assert result is None


def test_discord_local_tool_router_ignores_low_confidence_tool_choice() -> None:
    result = _local_tool_response(
        "maybe do something with config",
        router=lambda _content, _catalog: {
            "tool": "config.show",
            "arguments": {},
            "confidence": 0.3,
        },
    )

    assert result is None


def test_discord_local_tool_router_adds_checklist_item(
    tmp_path: Path, monkeypatch
) -> None:
    config = tmp_path / "bemine.toml"
    vault = tmp_path / "vault"
    config.write_text(
        f"""
[paths]
vault_dir = "{vault}"
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BEMINE_CONFIG", str(config))

    result = _local_tool_response(
        "add coffee beans to my buy list",
        router=lambda _content, _catalog: {
            "tool": "checklist.add",
            "arguments": {"list": "buy", "item": "coffee beans"},
            "confidence": 0.95,
        },
    )

    checklist = vault / "Checklists" / "Shopping.md"
    assert result is not None
    assert result.tool == "checklist.add"
    assert "coffee beans" in result.text
    assert "- [ ] coffee beans" in checklist.read_text(encoding="utf-8")


def test_discord_checklist_tool_rejects_empty_item() -> None:
    with pytest.raises(ValueError, match="checklist item is required"):
        _local_tool_response(
            "add it to shopping",
            router=lambda _content, _catalog: {
                "tool": "checklist.add",
                "arguments": {"list": "Shopping", "item": ""},
                "confidence": 0.9,
            },
        )


def test_discord_local_tool_router_receives_channel_context() -> None:
    seen: dict[str, str] = {}

    result = _local_tool_response(
        "in codeblock please",
        conversation_context="- voidash: show me the config please",
        router=lambda content, _catalog: (
            seen.setdefault("content", content),
            {
                "tool": "config.show",
                "arguments": {"format": "toml"},
                "confidence": 0.9,
            },
        )[1],
    )

    assert result is not None
    assert "show me the config please" in seen["content"]
    assert "Current Discord message:\nin codeblock please" in seen["content"]
    assert result.text.startswith("```toml")


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
