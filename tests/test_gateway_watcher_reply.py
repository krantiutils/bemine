from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.discord.gateway import maybe_consume_watcher_reply


def test_consume_when_awaiting(tmp_path, monkeypatch):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    db = BemineDB(settings.db_path)
    db.upsert_project_watcher(project_id="channel:p", channel_id="c1", brief="b",
                              interval_secs=300)
    db.update_project_watcher("channel:p", awaiting_reply=1)

    import bemine.jobs.project_watcher as pw
    monkeypatch.setattr(pw, "relay_user_reply", lambda s, d, w, text, deps: True)

    handled = maybe_consume_watcher_reply(settings, db, channel_id="c1", text="use staging")
    assert handled is True


def test_not_consumed_when_not_awaiting(tmp_path):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    db = BemineDB(settings.db_path)
    db.upsert_project_watcher(project_id="channel:p", channel_id="c1", brief="b",
                              interval_secs=300)  # awaiting_reply defaults to 0
    handled = maybe_consume_watcher_reply(settings, db, channel_id="c1", text="hi")
    assert handled is False


def test_not_consumed_when_no_watcher(tmp_path):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    db = BemineDB(settings.db_path)
    handled = maybe_consume_watcher_reply(settings, db, channel_id="cX", text="hi")
    assert handled is False
