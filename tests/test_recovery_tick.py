from bemine.config import BemineSettings
from bemine.jobs.auto_recovery import (
    tick_recovery, RecoveryDeps, load_recovery_state, recovery_state_path,
)


def _settings(tmp_path):
    return BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")


def _pane(*, activity=0, command="claude", pane_id="%2",
          path="/Users/cdjk/github/llm/rai-speech-toolkit", device="mbp"):
    # activity=0 -> staleness check skipped (fail-open), good for deterministic tests
    return {"device": device, "pane_id": pane_id, "path": path,
            "command": command, "activity": activity}


class _Tmux:
    """Returns a scripted sequence of captures; records sends."""
    def __init__(self, captures):
        self._caps = list(captures)
        self.i = 0
        self.sent_keys = []
        self.sent_text = []
    def capture(self, device, pane, lines=200):
        v = self._caps[min(self.i, len(self._caps) - 1)]
        self.i += 1
        return v
    def send_keys(self, device, pane, keys):
        self.sent_keys.append(keys)
    def send_text(self, device, pane, text):
        self.sent_text.append(text)


class _Decision:
    def __init__(self, kind, text="", keys=None, question=""):
        self.kind = kind
        self.text = text
        self.keys = keys or []
        self.question = question


def _deps(panes, tmux, *, clock, escalations, audits, decide=None):
    return RecoveryDeps(
        get_panes=lambda s: panes,
        tmux=tmux,
        decide=decide,
        escalate=lambda e: escalations.append(e),
        now=clock["now"],
        sleep=lambda s: None,
        audit=lambda et, kind, pane: audits.append((et, kind)),
    )


def _clock(t=2000.0):
    box = {"t": t}
    return {"now": lambda: box["t"], "box": box}


def test_rate_limit_resubmits_with_up_enter(tmp_path):
    s = _settings(tmp_path)
    tmux = _Tmux(["API Error: Rate limited"])
    clock, esc, aud = _clock(), [], []
    deps = _deps([_pane()], tmux, clock=clock, escalations=esc, audits=aud)
    tick_recovery(s, deps, now=clock["box"]["t"])
    assert tmux.sent_keys == [["Up", "Enter"]]
    assert ("resubmit", "rate_limit") in aud


def test_rate_limit_recovered_on_later_clean_tick(tmp_path):
    s = _settings(tmp_path)
    # tick1: rate-limited -> resubmit (attempts=1). tick2 (past backoff): clean -> recovered.
    tmux = _Tmux(["Rate limited", "all good now"])
    clock, esc, aud = _clock(), [], []
    deps = _deps([_pane()], tmux, clock=clock, escalations=esc, audits=aud)
    tick_recovery(s, deps, now=2000.0)
    tick_recovery(s, deps, now=2000.0 + 100000)  # past backoff
    assert ("recovered", "rate_limit") in aud
    assert load_recovery_state(recovery_state_path(s)) == {}


def test_rate_limit_escalates_once_when_persistent(tmp_path):
    s = _settings(tmp_path)
    tmux = _Tmux(["Rate limited"])  # always rate-limited
    clock, esc, aud = _clock(), [], []
    deps = _deps([_pane()], tmux, clock=clock, escalations=esc, audits=aud)
    for i in range(8):
        tick_recovery(s, deps, now=2000.0 + i * 100000)
    assert len(esc) == 1  # escalate-once even though it stays rate-limited


def test_trivial_confirm_sends_yes(tmp_path):
    s = _settings(tmp_path)
    tmux = _Tmux(["Do you want to proceed? (y/n)"])
    clock, esc, aud = _clock(), [], []
    deps = _deps([_pane()], tmux, clock=clock, escalations=esc, audits=aud)
    tick_recovery(s, deps, now=2000.0)
    assert tmux.sent_text == ["yes"]


def test_llm_answers_stuck_question_when_idle(tmp_path):
    s = _settings(tmp_path)
    # Same screen both ticks (idle). Not rate-limit, not trivial -> LLM tier on the 2nd tick.
    q = "I found two configs. Which environment should I target?"
    tmux = _Tmux([q, q, q])
    clock, esc, aud = _clock(), [], []
    calls = {"n": 0}
    def decide(pane, before):
        calls["n"] += 1
        return _Decision("send_text", text="use staging")
    deps = _deps([_pane()], tmux, clock=clock, escalations=esc, audits=aud, decide=decide)
    tick_recovery(s, deps, now=2000.0)   # 1st sighting: record hash, no LLM yet
    assert tmux.sent_text == []
    tick_recovery(s, deps, now=2001.0)   # same screen -> idle -> LLM answers
    assert tmux.sent_text == ["use staging"]
    assert calls["n"] == 1
    tick_recovery(s, deps, now=2002.0)   # same screen, already judged -> no repeat LLM
    assert calls["n"] == 1


def test_llm_not_called_when_screen_changing(tmp_path):
    s = _settings(tmp_path)
    tmux = _Tmux(["working line 1", "working line 2", "working line 3"])
    clock, esc, aud = _clock(), [], []
    calls = {"n": 0}
    def decide(pane, before):
        calls["n"] += 1
        return _Decision("wait")
    deps = _deps([_pane()], tmux, clock=clock, escalations=esc, audits=aud, decide=decide)
    for i in range(3):
        tick_recovery(s, deps, now=2000.0 + i)
    assert calls["n"] == 0  # screen keeps changing -> treated as working, never LLM'd
    assert tmux.sent_text == []


def test_skips_stale_pane(tmp_path):
    s = _settings(tmp_path)
    tmux = _Tmux(["Rate limited"])
    clock, esc, aud = _clock(), [], []
    # activity far in past; now huge -> exceeds 4h (14400) window from repo bemine.toml
    deps = _deps([_pane(activity=1)], tmux, clock=clock, escalations=esc, audits=aud)
    tick_recovery(s, deps, now=10_000_000.0)
    assert tmux.sent_keys == [] and tmux.sent_text == []


def test_tick_isolates_failing_pane(tmp_path):
    s = _settings(tmp_path)
    class _Boom:
        def capture(self, d, p, lines=200): raise RuntimeError("ssh fail")
        def send_keys(self, d, p, k): pass
        def send_text(self, d, p, t): pass
    clock, esc, aud = _clock(), [], []
    deps = _deps([_pane()], _Boom(), clock=clock, escalations=esc, audits=aud)
    tick_recovery(s, deps, now=2000.0)  # must not raise
    assert load_recovery_state(recovery_state_path(s)) == {}
