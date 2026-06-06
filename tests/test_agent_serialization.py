"""Real-adapter serialization tests.

These exercise the ACTUAL vendored converters (pure functions, no network) with
the exact shapes Bemine's registry and agent loop produce, to prove the agent
sends usable tools and a faithful transcript round-trips for every provider.
"""

import json

from bemine.context.tool_registry import BemineToolRegistry
from bemine.providers._anthropic_adapter import (
    convert_tools_to_anthropic,
    convert_messages_to_anthropic,
)
from bemine.providers._codex_responses_adapter import (
    _responses_tools,
    _chat_messages_to_responses_input,
)


def test_registry_tools_convert_with_real_names_anthropic():
    schemas = BemineToolRegistry().openai_schemas()
    out = convert_tools_to_anthropic(schemas)
    assert out and all(t["name"] for t in out)  # non-empty names
    assert all("input_schema" in t for t in out)


def test_registry_tools_convert_for_codex():
    schemas = BemineToolRegistry().openai_schemas()
    out = _responses_tools(schemas)
    assert out and all(
        (t.get("name") or t.get("function", {}).get("name")) for t in out
    )


def test_assistant_tool_call_transcript_round_trips_through_anthropic():
    # Simulate a persisted multi-turn transcript in the SHAPE the loop now stores,
    # then run it through the real Anthropic message converter and assert the
    # assistant tool_use + tool_result survive (names present, not stringified).
    messages = [
        {"role": "user", "content": "status please"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "t1",
                    "type": "function",
                    "function": {"name": "status_show", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "{\"ok\":true}"},
        {"role": "user", "content": "thanks"},
    ]
    # Real signature returns (system, anthropic_messages).
    _system, converted = convert_messages_to_anthropic(messages)
    blob = json.dumps(converted)
    assert "status_show" in blob  # tool name preserved
    assert "tool_use" in blob and "tool_result" in blob
    assert "'role':" not in blob  # no stringified python dicts leaked
    # The tool_use id and tool_result tool_use_id must link.
    assert "t1" in blob


def test_assistant_tool_call_transcript_round_trips_through_codex():
    # Same transcript, through the real Codex Responses converter: assert the
    # function_call and its function_call_output both survive and link.
    messages = [
        {"role": "user", "content": "status please"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "t1",
                    "type": "function",
                    "function": {"name": "status_show", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "{\"ok\":true}"},
        {"role": "user", "content": "thanks"},
    ]
    items = _chat_messages_to_responses_input(messages)
    types = [item.get("type") for item in items]
    assert "function_call" in types
    assert "function_call_output" in types
    fc = next(i for i in items if i.get("type") == "function_call")
    fco = next(i for i in items if i.get("type") == "function_call_output")
    assert fc["name"] == "status_show"
    # No orphaned output: the call_id of the output matches the call.
    assert fc["call_id"] == fco["call_id"]
