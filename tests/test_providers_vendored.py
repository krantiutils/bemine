from bemine.providers.types import NormalizedResponse, ToolCall


def test_toolcall_is_provider_neutral():
    tc = ToolCall(id="abc", name="status_show", arguments='{"limit": 5}')
    assert tc.name == "status_show"
    # Hermes backcompat shim: tc.function.name and tc.type == "function"
    assert tc.function.name == "status_show"
    assert tc.type == "function"


def test_normalized_response_shape():
    resp = NormalizedResponse(content="hi", tool_calls=None, finish_reason="stop")
    assert resp.finish_reason == "stop"
    assert resp.content == "hi"
