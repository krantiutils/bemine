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
