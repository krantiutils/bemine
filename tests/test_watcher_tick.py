from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.index.status import SessionStatus
from bemine.jobs.project_watcher import WatcherDeps, tick_watcher, Decision


def _setup(tmp_path):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    db = BemineDB(settings.db_path)
    db.upsert_project_watcher(project_id="rai-speech-toolkit", channel_id="c1",
                              brief="get training running", interval_secs=300)
    return settings, db


def _status(asks_user, cwd="/Users/cdjk/github/llm/rai-speech-toolkit"):
    return SessionStatus(
        device_id="mbp", runtime="claude", session_id="s", cwd=cwd,
        source_path="", last_interaction_at=1.0, age_seconds=10,
        last_role="assistant", asks_user=asks_user, ask_summary="proceed?",
        latest_summary="",
    )


class _Tmux:
    def __init__(self):
        self.sent_text = []
        self.sent_keys = []
    def list_panes(self, device):
        return [{"pane_id": "%2", "path": "/Users/cdjk/github/llm/rai-speech-toolkit",
                 "command": "claude"}]
    def capture(self, device, pane):
        return "Proceed? (y/n)"
    def send_text(self, device, pane, text):
        self.sent_text.append((device, pane, text))
    def send_keys(self, device, pane, keys):
        self.sent_keys.append((device, pane, keys))


def _deps(statuses, decision, tmux, echoes):
    return WatcherDeps(
        get_statuses=lambda settings: statuses,
        tmux=tmux,
        decide=lambda **kw: decision,
        echo=lambda channel_id, text: echoes.append((channel_id, text)),
        transcript_tail=lambda status: "",
    )


def test_tick_sends_text_when_session_waiting(tmp_path):
    settings, db = _setup(tmp_path)
    tmux, echoes = _Tmux(), []
    deps = _deps([_status(asks_user=True)], Decision(kind="send_text", text="y"), tmux, echoes)
    tick_watcher(settings, db, db.get_project_watcher("rai-speech-toolkit"), deps, now=100.0)
    assert tmux.sent_text == [("mbp", "%2", "y")]
    assert db.get_project_watcher("rai-speech-toolkit")["last_tick_at"] == 100.0
    assert any("sent" in t.lower() for _, t in echoes)


def test_tick_does_nothing_when_session_working(tmp_path):
    settings, db = _setup(tmp_path)
    tmux, echoes = _Tmux(), []
    deps = _deps([_status(asks_user=False)], Decision(kind="send_text", text="y"), tmux, echoes)
    tick_watcher(settings, db, db.get_project_watcher("rai-speech-toolkit"), deps, now=100.0)
    assert tmux.sent_text == []


def test_tick_escalates_and_pauses(tmp_path):
    settings, db = _setup(tmp_path)
    tmux, echoes = _Tmux(), []
    deps = _deps([_status(asks_user=True)],
                 Decision(kind="escalate", question="Which account?"), tmux, echoes)
    tick_watcher(settings, db, db.get_project_watcher("rai-speech-toolkit"), deps, now=100.0)
    row = db.get_project_watcher("rai-speech-toolkit")
    assert row["awaiting_reply"] == 1
    assert any("Which account?" in t for _, t in echoes)


def test_tick_done_stops_watcher(tmp_path):
    settings, db = _setup(tmp_path)
    tmux, echoes = _Tmux(), []
    deps = _deps([_status(asks_user=True)],
                 Decision(kind="done", rationale="training is live"), tmux, echoes)
    tick_watcher(settings, db, db.get_project_watcher("rai-speech-toolkit"), deps, now=100.0)
    assert db.get_project_watcher("rai-speech-toolkit")["status"] == "done"


def test_tick_pauses_after_missing_session(tmp_path):
    settings, db = _setup(tmp_path)
    tmux, echoes = _Tmux(), []
    deps = _deps([], Decision(kind="wait"), tmux, echoes)  # no live session
    for _ in range(3):
        w = db.get_project_watcher("rai-speech-toolkit")
        tick_watcher(settings, db, w, deps, now=100.0)
    assert db.get_project_watcher("rai-speech-toolkit")["status"] == "paused"


def test_tick_send_keys_for_menu(tmp_path):
    settings, db = _setup(tmp_path)
    tmux, echoes = _Tmux(), []
    deps = _deps([_status(asks_user=True)],
                 Decision(kind="send_keys", keys=["Down", "Enter"]), tmux, echoes)
    tick_watcher(settings, db, db.get_project_watcher("rai-speech-toolkit"), deps, now=100.0)
    assert tmux.sent_keys == [("mbp", "%2", ["Down", "Enter"])]


def test_tick_pauses_with_stale_watcher_dict(tmp_path):
    # Even if the caller passes the SAME (stale) watcher dict each time, empty_ticks
    # must accumulate via the DB and reach the pause threshold.
    settings, db = _setup(tmp_path)
    tmux, echoes = _Tmux(), []
    deps = _deps([], Decision(kind="wait"), tmux, echoes)  # no live session
    stale = db.get_project_watcher("rai-speech-toolkit")
    for _ in range(3):
        tick_watcher(settings, db, stale, deps, now=100.0)
    assert db.get_project_watcher("rai-speech-toolkit")["status"] == "paused"
