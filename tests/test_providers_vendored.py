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


from bemine.providers._anthropic_adapter import convert_tools_to_anthropic

SAMPLE_TOOL = {
    "type": "function",
    "function": {
        "name": "status_show",
        "description": "Show fleet status",
        "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}},
    },
}


def test_anthropic_tool_translation_renames_parameters_to_input_schema():
    out = convert_tools_to_anthropic([SAMPLE_TOOL])
    assert len(out) == 1
    tool = out[0]
    assert tool["name"] == "status_show"
    assert "input_schema" in tool
    assert "parameters" not in tool
    assert tool["input_schema"]["type"] == "object"


from bemine.providers._codex_responses_adapter import _responses_tools


def test_codex_tool_translation_keeps_function_type():
    out = _responses_tools([SAMPLE_TOOL])
    assert out and out[0]["type"] == "function"
    assert out[0]["name"] == "status_show" or out[0]["function"]["name"] == "status_show"
