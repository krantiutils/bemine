from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from bemine.discord.dispatch import (
    DispatchProgress,
    DispatchResult,
    ProjectChannel,
    ProjectRoute,
)
import bemine.discord.gateway as gw
from bemine.discord.gateway import (
    ACK_REACTION,
    SUCCESS_REACTION,
    WARNING_REACTION,
    WORKING_REACTION,
    _acknowledge_message,
    _best_effort_typing,
    _discord_chunk,
    _dispatch_progress_text,
    _dispatch_response_text,
    _format_conversation_context,
)
from bemine.discord.store import Store


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


def test_direct_observability_command_aliases() -> None:
    assert gw._direct_observability_command("health?") == "health"
    assert gw._direct_observability_command("what's running?") == "health"
    assert gw._direct_observability_command("what daemons are running?") == "health"
    assert gw._direct_observability_command("what can you do?") == "help"
    assert gw._direct_observability_command("discord routes") == "discord.routes"
    assert gw._direct_observability_command("line cuts") == "continuity"
    assert gw._direct_observability_command("mayor dispatch status") == "dispatch.status"
    assert gw._direct_observability_command("what model are we using?") == "dispatch.status"
    assert gw._direct_observability_command("what should I focus on today?".lower()) == "pa.plan"
    assert gw._direct_observability_command("what should I focus on this week?") == "pa.plan"
    assert gw._direct_observability_command("what should I do next?") == "pa.plan"
    assert gw._direct_observability_command("what should I focus on tomorrow?") == "pa.plan"
    assert gw._direct_observability_command("goals") == "goals.list"
    assert gw._direct_observability_command("what goal sessions are active?") == "goals.list"
    assert gw._direct_observability_command("what config is set?") == "config.show"
    assert gw._direct_observability_command("what's my config?") == "config.show"
    assert gw._direct_observability_command("what backlog owners are configured?") == "config.show"
    assert gw._direct_observability_command("what is the default backlog owner?") == "config.show"
    assert gw._direct_subagents_command("subagents") == {"command": "subagents.list"}
    assert gw._direct_subagents_command("show subagents") == {"command": "subagents.list"}
    assert gw._direct_subagents_command("what subagents are configured?") == {"command": "subagents.list"}
    assert gw._direct_subagents_command("show PA subagent") == {"command": "subagents.show", "name": "PA"}
    assert gw._direct_subagents_command("what is PA?") == {"command": "subagents.show", "name": "PA"}
    assert gw._direct_subagents_command("what does PA do?") == {"command": "subagents.show", "name": "PA"}
    assert gw._direct_subagents_command("show SMM") == {"command": "subagents.show", "name": "SMM"}
    assert gw._direct_subagents_command("what does SMM do?") == {"command": "subagents.show", "name": "SMM"}
    assert gw._direct_subagents_command("what is the style convention?") == {"command": "subagents.show", "name": "style"}
    assert gw._direct_smm_command("what should I post?") == {"command": "smm.draft"}
    assert gw._direct_smm_command("what should I post today?") == {"command": "smm.draft"}
    assert gw._direct_smm_command("what should I post this week?") == {"command": "smm.draft"}
    assert gw._direct_smm_command("show me post ideas") == {"command": "smm.draft"}
    assert gw._direct_smm_command("draft a post") == {"command": "smm.draft"}
    assert gw._direct_phantom_command("phantom") == {"command": "phantom.status"}
    assert gw._direct_phantom_command("show phantom") == {"command": "phantom.status"}
    assert gw._direct_phantom_command("what is Phantom?") == {"command": "phantom.status"}
    assert gw._direct_phantom_command("what does Phantom do?") == {"command": "phantom.status"}
    assert gw._direct_phantom_command("phantom status") == {"command": "phantom.status"}
    assert gw._direct_phantom_command("phantom install") == {"command": "phantom.install"}
    assert gw._direct_observability_command("what jobs are scheduled?") == "jobs.list"
    assert gw._direct_observability_command("what cron jobs are set up?") == "jobs.list"
    assert gw._direct_backlog_command("show backlog") == {"owner": "SMM"}
    assert gw._direct_backlog_command("what's in my backlog?") == {"owner": "SMM"}
    assert gw._direct_backlog_command("what's in PA backlog?") == {"owner": "PA"}
    assert gw._direct_observability_command('goal rai: "train"') == ""


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
    from bemine.discord.config import Settings
    from bemine.discord.gateway import MayorDiscordGateway

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
                 progress_callback=None, cancel_event=None):
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


def test_dispatch_status_plain_text_uses_local_command(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    def fake_run_command(name, settings, args):
        calls.append(name)
        return SimpleNamespace(text="Dispatch runtime:\n- runtime: kimi", data={})

    def fail_run_agent(*args, **kwargs):
        raise AssertionError("plain dispatch status should not start an agent")

    monkeypatch.setattr(gw, "run_command", fake_run_command)
    monkeypatch.setattr(gw, "run_bound_agent", fail_run_agent)

    gateway = _make_gateway(tmp_path)
    message = _PassthroughMessage("what model are we using?")

    asyncio.run(gateway._handle_allowed_message(message))

    assert calls == ["dispatch.status"]
    assert message.replies == ["Dispatch runtime:\n- runtime: kimi"]


def test_line_cut_plain_text_uses_local_command(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    def fake_run_command(name, settings, args):
        calls.append(name)
        return SimpleNamespace(text="Bemine continuity:\n- device=cdjk", data={})

    def fail_run_agent(*args, **kwargs):
        raise AssertionError("continuity status should not start an agent")

    monkeypatch.setattr(gw, "run_command", fake_run_command)
    monkeypatch.setattr(gw, "run_bound_agent", fail_run_agent)

    gateway = _make_gateway(tmp_path)
    message = _PassthroughMessage("line cuts")

    asyncio.run(gateway._handle_allowed_message(message))

    assert calls == ["continuity"]
    assert message.replies == ["Bemine continuity:\n- device=cdjk"]


def test_help_plain_text_uses_local_command(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    def fake_run_command(name, settings, args):
        calls.append(name)
        return SimpleNamespace(text="Bemine help:\n- device: `cdjk`", data={})

    def fail_run_agent(*args, **kwargs):
        raise AssertionError("help should not start an agent")

    monkeypatch.setattr(gw, "run_command", fake_run_command)
    monkeypatch.setattr(gw, "run_bound_agent", fail_run_agent)

    gateway = _make_gateway(tmp_path)
    message = _PassthroughMessage("what can you do?")

    asyncio.run(gateway._handle_allowed_message(message))

    assert calls == ["help"]
    assert message.replies == ["Bemine help:\n- device: `cdjk`"]


def test_routes_plain_text_uses_local_command(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    def fake_run_command(name, settings, args):
        calls.append(name)
        return SimpleNamespace(text="Discord route management:\n- config_exists: True", data={})

    def fail_run_agent(*args, **kwargs):
        raise AssertionError("routes should not start an agent")

    monkeypatch.setattr(gw, "run_command", fake_run_command)
    monkeypatch.setattr(gw, "run_bound_agent", fail_run_agent)

    gateway = _make_gateway(tmp_path)
    message = _PassthroughMessage("discord routes")

    asyncio.run(gateway._handle_allowed_message(message))

    assert calls == ["discord.routes"]
    assert message.replies == ["Discord route management:\n- config_exists: True"]


def test_dispatch_failure_finishes_progress_as_failed(tmp_path: Path, monkeypatch) -> None:
    notes: list[str | None] = []

    class FakeReporter:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __call__(self, _text: str) -> None:
            pass

        async def finish(self, *, note: str | None = None) -> None:
            notes.append(note)

    def fake_run(settings, message, normalized, saved_attachments=None,
                 progress_callback=None, cancel_event=None):
        raise RuntimeError("boom sk-kimi-abcdefghijklmnopqrstuvwxyz")

    monkeypatch.setattr(gw, "_ProgressReporter", FakeReporter)
    monkeypatch.setattr(gw, "run_bound_agent", fake_run)

    gateway = _make_gateway(tmp_path)
    message = _PassthroughMessage("hello")

    asyncio.run(gateway._handle_allowed_message(message))

    assert notes == ["Dispatch failed: boom <token>"]
    assert message.replies == ["Dispatch failed: boom <token>"]
    assert WARNING_REACTION in message.reactions


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
