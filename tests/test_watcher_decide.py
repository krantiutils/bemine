from bemine.jobs.project_watcher import decide, _parse_decision


class _Resp:
    def __init__(self, content):
        self.content = content


class _FakeClient:
    def __init__(self, content):
        self._content = content
        self.last_kwargs = None
    def complete(self, *, messages, tools, model, system):
        self.last_kwargs = {"messages": messages, "tools": tools,
                            "model": model, "system": system}
        return _Resp(self._content)


def test_parse_send_text_decision():
    d = _parse_decision('{"kind": "send_text", "text": "yes, proceed"}')
    assert d.kind == "send_text"
    assert d.text == "yes, proceed"


def test_parse_send_keys_decision():
    d = _parse_decision('{"kind": "send_keys", "keys": ["Down", "Enter"]}')
    assert d.kind == "send_keys"
    assert d.keys == ["Down", "Enter"]


def test_parse_escalate_decision():
    d = _parse_decision('{"kind": "escalate", "question": "Which AWS account?"}')
    assert d.kind == "escalate"
    assert d.question == "Which AWS account?"


def test_parse_handles_fenced_json_and_garbage():
    d = _parse_decision('here you go:\n```json\n{"kind": "done", "rationale": "goal met"}\n```')
    assert d.kind == "done"
    assert d.rationale == "goal met"
    # Unparseable -> safe escalate
    bad = _parse_decision("I am not json")
    assert bad.kind == "escalate"


def test_decide_passes_brief_and_pane_to_client():
    client = _FakeClient('{"kind": "send_text", "text": "go"}')
    d = decide(client, model="kimi-k2.6", brief="get training running",
               pane_capture="Proceed? (y/n)", transcript_tail="...waiting...",
               ask_summary="asks to proceed", channel_context="")
    assert d.kind == "send_text"
    sent = client.last_kwargs
    assert "get training running" in sent["system"]
    assert "Proceed? (y/n)" in sent["messages"][-1]["content"]
    assert sent["tools"] == []  # one-shot, no tools
