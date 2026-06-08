from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.index.status import SessionStatus
from bemine.jobs.project_watcher import WatcherDeps, relay_user_reply, tick_all_watchers


def _settings_db(tmp_path):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    return settings, BemineDB(settings.db_path)


def _status():
    return SessionStatus(
        device_id="mbp", runtime="claude", session_id="s",
        cwd="/Users/cdjk/github/llm/rai-speech-toolkit", source_path="",
        last_interaction_at=1.0, age_seconds=10, last_role="assistant",
        asks_user=True, ask_summary="", latest_summary="",
    )


class _Tmux:
    def __init__(self):
        self.sent_text = []
    def list_panes(self, device):
        return [{"pane_id": "%2", "path": "/Users/cdjk/github/llm/rai-speech-toolkit",
                 "command": "claude"}]
    def capture(self, device, pane):
        return ""
    def send_text(self, device, pane, text):
        self.sent_text.append(text)
    def send_keys(self, device, pane, keys):
        pass


def _deps(tmux):
    return WatcherDeps(get_statuses=lambda s: [_status()], tmux=tmux,
                       decide=lambda **kw: None, echo=lambda c, t: None,
                       transcript_tail=lambda s: "")


def test_relay_user_reply_sends_text_and_resumes(tmp_path):
    settings, db = _settings_db(tmp_path)
    db.upsert_project_watcher(project_id="rai-speech-toolkit", channel_id="c1",
                              brief="b", interval_secs=300)
    db.update_project_watcher("rai-speech-toolkit", awaiting_reply=1)
    tmux = _Tmux()
    ok = relay_user_reply(settings, db, db.get_project_watcher_by_channel("c1"),
                          "use the staging account", _deps(tmux))
    assert ok is True
    assert tmux.sent_text == ["use the staging account"]
    row = db.get_project_watcher("rai-speech-toolkit")
    assert row["awaiting_reply"] == 0
    assert row["last_tick_at"] == 0  # so it re-ticks promptly


def test_tick_all_fires_only_due_active_watchers(tmp_path):
    settings, db = _settings_db(tmp_path)
    db.upsert_project_watcher(project_id="due", channel_id="c1", brief="b", interval_secs=300)
    db.upsert_project_watcher(project_id="fresh", channel_id="c2", brief="b", interval_secs=300)
    db.update_project_watcher("fresh", last_tick_at=1000.0)
    db.update_project_watcher("due", last_tick_at=0.0)
    db.update_project_watcher("due", awaiting_reply=0)
    fired = []
    deps = _deps(_Tmux())
    tick_all_watchers(settings, db, deps=deps, now=1100.0,
                      tick=lambda s, d, w, dp, now: fired.append(w["project_id"]))
    assert fired == ["due"]  # 'fresh' not due (1000 + 300 > 1100)


def test_tick_all_skips_awaiting_reply(tmp_path):
    settings, db = _settings_db(tmp_path)
    db.upsert_project_watcher(project_id="p", channel_id="c", brief="b", interval_secs=300)
    db.update_project_watcher("p", last_tick_at=0.0, awaiting_reply=1)
    fired = []
    tick_all_watchers(settings, db, deps=_deps(_Tmux()), now=1100.0,
                      tick=lambda s, d, w, dp, now: fired.append(w["project_id"]))
    assert fired == []
