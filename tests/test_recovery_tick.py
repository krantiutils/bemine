from bemine.config import BemineSettings
from bemine.index.status import SessionStatus
from bemine.jobs.auto_recovery import (
    tick_recovery, RecoveryDeps, load_recovery_state, recovery_state_path,
)


def _settings(tmp_path):
    return BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")


def _status(*, pane_age=10, asks_user=False, cwd="/Users/cdjk/github/llm/rai-speech-toolkit"):
    return SessionStatus(
        device_id="mbp", runtime="claude", session_id="s1", cwd=cwd, source_path="",
        last_interaction_at=1.0, age_seconds=pane_age, last_role="assistant",
        asks_user=asks_user, ask_summary="?", latest_summary="",
    )


class _Tmux:
    def __init__(self, before, after):
        self._seq = [before, after] * 50
        self.i = 0
        self.sent_keys = []
        self.sent_text = []
    def list_panes(self, device):
        return [{"pane_id": "%2", "path": "/Users/cdjk/github/llm/rai-speech-toolkit",
                 "command": "claude"}]
    def capture(self, device, pane):
        v = self._seq[self.i]
        self.i += 1
        return v
    def send_keys(self, device, pane, keys):
        self.sent_keys.append(keys)
    def send_text(self, device, pane, text):
        self.sent_text.append(text)


def _deps(statuses, tmux, *, clock, escalations, audits, decide=None):
    return RecoveryDeps(
        get_statuses=lambda s: statuses,
        tmux=tmux,
        decide=decide or (lambda status, pane_text: None),
        escalate=lambda event: escalations.append(event),
        now=clock["now"],
        sleep=lambda s: None,
        audit=lambda et, detail, status: audits.append((et, detail)),
    )


def _clock(t=1000.0):
    box = {"t": t}
    return {"now": lambda: box["t"], "box": box}


def test_rate_limit_resubmits_and_recovers(tmp_path):
    s = _settings(tmp_path)
    tmux = _Tmux("API Error: Rate limited", "working again...")
    clock, esc, aud = _clock(), [], []
    deps = _deps([_status()], tmux, clock=clock, escalations=esc, audits=aud)
    tick_recovery(s, deps, now=1000.0)
    assert tmux.sent_keys == [["Enter"]]
    assert esc == []
    assert load_recovery_state(recovery_state_path(s)) == {}


def test_rate_limit_unrecovered_backs_off_then_escalates(tmp_path):
    s = _settings(tmp_path)
    tmux = _Tmux("Rate limited", "Rate limited")
    clock, esc, aud = _clock(), [], []
    deps = _deps([_status()], tmux, clock=clock, escalations=esc, audits=aud)
    for i in range(5):
        clock["box"]["t"] = 1000.0 + i * 100000
        tick_recovery(s, deps, now=clock["box"]["t"])
    assert len(esc) == 1
    assert esc[0]["type"] == "needs_input"


def test_trivial_confirm_sends_yes(tmp_path):
    s = _settings(tmp_path)
    tmux = _Tmux("Do you want to proceed? (y/n)", "done")
    clock, esc, aud = _clock(), [], []
    deps = _deps([_status(asks_user=True)], tmux, clock=clock, escalations=esc, audits=aud)
    tick_recovery(s, deps, now=1000.0)
    assert tmux.sent_text == ["yes"]


def test_skips_stale_session(tmp_path):
    s = _settings(tmp_path)
    tmux = _Tmux("Rate limited", "Rate limited")
    clock, esc, aud = _clock(), [], []
    deps = _deps([_status(pane_age=99999)], tmux, clock=clock, escalations=esc, audits=aud)
    tick_recovery(s, deps, now=1000.0)
    assert tmux.sent_keys == [] and tmux.sent_text == []


def test_healthy_session_no_action(tmp_path):
    s = _settings(tmp_path)
    tmux = _Tmux("writing code", "writing code")
    clock, esc, aud = _clock(), [], []
    deps = _deps([_status()], tmux, clock=clock, escalations=esc, audits=aud)
    tick_recovery(s, deps, now=1000.0)
    assert tmux.sent_keys == [] and tmux.sent_text == []
