from bemine.config import BemineSettings
from bemine.jobs.auto_recovery import (
    tick_recovery, RecoveryDeps, load_recovery_state, recovery_state_path,
)


def _settings(tmp_path):
    return BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")


def _pane(*, activity=1000, command="claude", pane_id="%2",
          path="/Users/cdjk/github/llm/rai-speech-toolkit", device="mbp"):
    return {"device": device, "pane_id": pane_id, "path": path,
            "command": command, "activity": activity}


class _Tmux:
    def __init__(self, before, after):
        self._seq = [before, after] * 50
        self.i = 0
        self.sent_keys = []
        self.sent_text = []
    def capture(self, device, pane, lines=200):
        v = self._seq[self.i]
        self.i += 1
        return v
    def send_keys(self, device, pane, keys):
        self.sent_keys.append(keys)
    def send_text(self, device, pane, text):
        self.sent_text.append(text)


def _deps(panes, tmux, *, clock, escalations, audits):
    return RecoveryDeps(
        get_panes=lambda s: panes,
        tmux=tmux,
        escalate=lambda event: escalations.append(event),
        now=clock["now"],
        sleep=lambda s: None,
        audit=lambda et, kind, pane: audits.append((et, kind)),
    )


def _clock(t=2000.0):
    box = {"t": t}
    return {"now": lambda: box["t"], "box": box}


def test_rate_limit_resubmits_and_recovers(tmp_path):
    s = _settings(tmp_path)
    tmux = _Tmux("API Error: Rate limited", "working again...")
    clock, esc, aud = _clock(), [], []
    deps = _deps([_pane()], tmux, clock=clock, escalations=esc, audits=aud)
    tick_recovery(s, deps, now=clock["box"]["t"])
    assert tmux.sent_keys == [["Up", "Enter"]]
    assert esc == []
    assert load_recovery_state(recovery_state_path(s)) == {}


def test_rate_limit_unrecovered_backs_off_then_escalates(tmp_path):
    s = _settings(tmp_path)
    tmux = _Tmux("Rate limited", "Rate limited")
    clock, esc, aud = _clock(), [], []
    # activity=0 so pane is never considered stale regardless of how far now advances
    deps = _deps([_pane(activity=0)], tmux, clock=clock, escalations=esc, audits=aud)
    for i in range(5):
        clock["box"]["t"] = 2000.0 + i * 100000
        tick_recovery(s, deps, now=clock["box"]["t"])
    assert len(esc) == 1
    assert esc[0]["type"] == "needs_input"
    assert esc[0]["device_id"] == "mbp"


def test_escalates_only_once_even_after_hold(tmp_path):
    s = _settings(tmp_path)
    tmux = _Tmux("Rate limited", "Rate limited")
    clock, esc, aud = _clock(), [], []
    # activity=0 so pane is never considered stale regardless of how far now advances
    deps = _deps([_pane(activity=0)], tmux, clock=clock, escalations=esc, audits=aud)
    for i in range(8):
        clock["box"]["t"] = 2000.0 + i * 200000.0
        tick_recovery(s, deps, now=clock["box"]["t"])
    assert len(esc) == 1


def test_trivial_confirm_sends_yes(tmp_path):
    s = _settings(tmp_path)
    tmux = _Tmux("Do you want to proceed? (y/n)", "done")
    clock, esc, aud = _clock(), [], []
    deps = _deps([_pane()], tmux, clock=clock, escalations=esc, audits=aud)
    tick_recovery(s, deps, now=clock["box"]["t"])
    assert tmux.sent_text == ["yes"]


def test_skips_stale_pane(tmp_path):
    s = _settings(tmp_path)
    tmux = _Tmux("Rate limited", "Rate limited")
    # Use a large now so that activity=1 is older than max_session_age regardless of config value
    clock, esc, aud = _clock(t=20000.0), [], []
    deps = _deps([_pane(activity=1)], tmux, clock=clock, escalations=esc, audits=aud)
    tick_recovery(s, deps, now=clock["box"]["t"])
    assert tmux.sent_keys == [] and tmux.sent_text == []


def test_healthy_pane_no_action(tmp_path):
    s = _settings(tmp_path)
    tmux = _Tmux("writing code", "writing code")
    clock, esc, aud = _clock(), [], []
    deps = _deps([_pane()], tmux, clock=clock, escalations=esc, audits=aud)
    tick_recovery(s, deps, now=clock["box"]["t"])
    assert tmux.sent_keys == [] and tmux.sent_text == []


class _ExplodingTmux:
    def capture(self, device, pane, lines=200):
        raise RuntimeError("ssh: command not found: tmux")
    def send_keys(self, device, pane, keys):
        pass
    def send_text(self, device, pane, text):
        pass


def test_tick_isolates_failing_pane(tmp_path):
    s = _settings(tmp_path)
    clock, esc, aud = _clock(), [], []
    deps = _deps([_pane()], _ExplodingTmux(), clock=clock, escalations=esc, audits=aud)
    tick_recovery(s, deps, now=clock["box"]["t"])  # must not raise
    assert load_recovery_state(recovery_state_path(s)) == {}
