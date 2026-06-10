from bemine.config import BemineSettings
from bemine.agent.clients import FallbackClient, build_client_for_runtime


class _Resp:
    def __init__(self, content):
        self.content = content
        self.tool_calls = None


class _Client:
    def __init__(self, fail=0, label="x"):
        self.fail = fail
        self.calls = 0
        self.label = label

    def complete(self, *, messages, tools, model, system):
        self.calls += 1
        if self.calls <= self.fail:
            raise RuntimeError("Error code: 500 - api_error: max turns")
        return _Resp(self.label)


def test_primary_success_never_touches_fallback():
    primary, fallback = _Client(), _Client(label="fb")
    fc = FallbackClient(primary, fallback, fallback_model="kimi-k2.6")
    resp = fc.complete(messages=[], tools=[], model="claude-opus-4-6", system="s")
    assert resp.content == "x"
    assert fallback.calls == 0


def test_falls_back_after_primary_retry_exhausted():
    primary, fallback = _Client(fail=99), _Client(label="fb")
    fc = FallbackClient(primary, fallback, fallback_model="kimi-k2.6")
    resp = fc.complete(messages=[], tools=[], model="claude-opus-4-6", system="s")
    assert resp.content == "fb"
    assert primary.calls == 2          # tried, retried once, then gave way
    assert fallback.calls == 1


def test_primary_recovers_on_retry_no_fallback():
    primary, fallback = _Client(fail=1), _Client(label="fb")
    fc = FallbackClient(primary, fallback, fallback_model="kimi-k2.6")
    resp = fc.complete(messages=[], tools=[], model="claude-opus-4-6", system="s")
    assert resp.content == "x"
    assert fallback.calls == 0


def test_both_dead_raises_primary_error():
    primary, fallback = _Client(fail=99), _Client(fail=99, label="fb")
    fc = FallbackClient(primary, fallback, fallback_model="kimi-k2.6")
    try:
        fc.complete(messages=[], tools=[], model="m", system="s")
        assert False, "expected raise"
    except RuntimeError:
        pass


def test_claude_runtime_is_wrapped_with_fallback(tmp_path):
    s = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    client = build_client_for_runtime(s, "claude")
    assert isinstance(client, FallbackClient)
