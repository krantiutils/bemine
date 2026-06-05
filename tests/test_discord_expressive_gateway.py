from __future__ import annotations

import asyncio
from pathlib import Path

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
    _dispatch_progress_text,
    _dispatch_response_text,
    _local_tool_response,
)


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
    project = ProjectChannel(channel_name="bemine", cwd=Path("/tmp"))
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
