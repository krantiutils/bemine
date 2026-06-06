import json
from pathlib import Path

from bemine.providers.types import NormalizedResponse, ToolCall


class FakeClient:
    """Scripted LLMClient: yields each queued NormalizedResponse per call."""

    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls = []

    def complete(self, *, messages, tools, model, system):
        self.calls.append({"messages": list(messages), "tools": list(tools)})
        return self._scripted.pop(0)


def test_fake_client_scripts_responses():
    client = FakeClient([NormalizedResponse(content="done", tool_calls=None, finish_reason="stop")])
    out = client.complete(messages=[], tools=[], model="m", system="s")
    assert out.content == "done"


from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.tool_registry import BemineToolRegistry
from bemine.agent.loop import AgentLoop


def _settings(tmp_path: Path) -> BemineSettings:
    return BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "vault")


def test_loop_executes_tool_then_returns_final_text(tmp_path):
    settings = _settings(tmp_path)
    db = BemineDB(settings.db_path)
    db.upsert_bemine_session(
        session_id="s1", channel_id="chan1", project_id="p1",
        runtime="kimi", model="kimi-k2.6", device_id="local",
        cwd=str(tmp_path), status="active", summary="",
    )
    client = FakeClient([
        NormalizedResponse(
            content=None, finish_reason="tool_calls",
            tool_calls=[ToolCall(id="t1", name="status_show", arguments=json.dumps({"limit": 1}))],
        ),
        NormalizedResponse(content="Here is your status.", finish_reason="stop", tool_calls=None),
    ])
    loop = AgentLoop(settings=settings, db=db, client=client,
                     tools=BemineToolRegistry(), max_iterations=5)

    result = loop.run(session_id="s1", project_id="p1", cwd=tmp_path,
                      user_message="status please", system="You are Bemine.")

    assert result.final_text == "Here is your status."
    assert result.tool_calls == 1
    assert len(client.calls) == 2
    roles = [m["role"] for m in client.calls[1]["messages"]]
    assert "tool" in roles
    stored = db.list_bemine_session_messages(session_id="s1")
    assert any(m["role"] == "user" for m in stored)
    assert any(m["role"] == "assistant" for m in stored)


def test_loop_stops_at_max_iterations(tmp_path):
    settings = _settings(tmp_path)
    db = BemineDB(settings.db_path)
    db.upsert_bemine_session(
        session_id="s2", channel_id="chan2", project_id="p1", runtime="kimi",
        model="m", device_id="local", cwd=str(tmp_path), status="active", summary="",
    )
    looping = [
        NormalizedResponse(content=None, finish_reason="tool_calls",
                           tool_calls=[ToolCall(id=f"t{i}", name="status_show", arguments="{}")])
        for i in range(10)
    ]
    client = FakeClient(looping)
    loop = AgentLoop(settings=settings, db=db, client=client,
                     tools=BemineToolRegistry(), max_iterations=3)
    result = loop.run(session_id="s2", project_id="p1", cwd=tmp_path,
                      user_message="go", system="s")
    assert result.stopped_reason == "max_iterations"
    assert len(client.calls) == 3


from bemine.agent.clients import AnthropicClient


class _FakeAnthropicSDK:
    """Stands in for anthropic.Anthropic(): records kwargs, returns a canned object."""
    def __init__(self):
        self.messages = self
        self.seen = {}

    def create(self, **kwargs):
        self.seen = kwargs
        class _Block:
            type = "tool_use"; id = "t1"; name = "status_show"; input = {"limit": 1}
        class _Resp:
            content = [_Block()]; stop_reason = "tool_use"
            usage = type("U", (), {"input_tokens": 10, "output_tokens": 3})()
        return _Resp()


def test_anthropic_client_normalizes_tool_use():
    sdk = _FakeAnthropicSDK()
    client = AnthropicClient(sdk=sdk, base_url="https://api.kimi.com/coding")
    out = client.complete(
        messages=[{"role": "user", "content": "status"}],
        tools=[{"type": "function", "function": {"name": "status_show",
                "description": "", "parameters": {"type": "object", "properties": {}}}}],
        model="kimi-k2.6", system="You are Bemine.",
    )
    assert out.tool_calls and out.tool_calls[0].name == "status_show"
    assert "tools" in sdk.seen
