from __future__ import annotations

import asyncio
from pathlib import Path

from mayor_discord.dispatch import DispatchResult, ProjectChannel, ProjectRoute
from mayor_discord.gateway import (
    ACK_REACTION,
    _acknowledge_message,
    _best_effort_typing,
    _dispatch_response_text,
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
    result = DispatchResult(ok=True, project=project, output="done", elapsed_seconds=1.0)

    assert _dispatch_response_text(result, route) == "done"


def test_dispatch_response_keeps_failure_context() -> None:
    project = ProjectChannel(channel_name="bemine", cwd=Path("/tmp"))
    route = ProjectRoute(project=project, reason="default")
    result = DispatchResult(ok=False, project=project, output="failed", elapsed_seconds=1.0)

    assert _dispatch_response_text(result, route) == "Codex dispatch issue for `#bemine`:\nfailed"
