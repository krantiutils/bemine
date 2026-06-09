from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.index.status import SessionStatus
from bemine.jobs.project_watcher import WatcherDeps, tick_watcher, Decision


def _setup(tmp_path):
    s = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    db = BemineDB(s.db_path)
    db.upsert_project_watcher(project_id="rai-speech-toolkit", channel_id="c1",
                              brief="b", interval_secs=300)
    return s, db


def _status():
    return SessionStatus(
        device_id="mbp", runtime="claude", session_id="s", cwd="/x/rai-speech-toolkit",
        source_path="", last_interaction_at=1.0, age_seconds=10, last_role="assistant",
        asks_user=True, ask_summary="?", latest_summary="",
    )


class _StuckTmux:
    def __init__(self): self.sent = 0
    def list_panes(self, d): return [{"pane_id": "%2", "path": "/x/rai-speech-toolkit", "command": "claude"}]
    def capture(self, d, p): return "SAME PANE (unchanged)"
    def send_text(self, d, p, t): self.sent += 1
    def send_keys(self, d, p, k): self.sent += 1


def _deps(tmux, echoes):
    return WatcherDeps(get_statuses=lambda s: [_status()], tmux=tmux,
                       decide=lambda **kw: Decision(kind="send_text", text="go"),
                       echo=lambda c, t: echoes.append(t), transcript_tail=lambda s: "")


def test_watcher_escalates_when_pane_never_advances(tmp_path):
    s, db = _setup(tmp_path)
    tmux, echoes = _StuckTmux(), []
    deps = _deps(tmux, echoes)
    for i in range(3):
        tick_watcher(s, db, db.get_project_watcher("rai-speech-toolkit"), deps, now=100.0 + i)
    assert any("not responding" in t.lower() or "stuck" in t.lower() for t in echoes)
