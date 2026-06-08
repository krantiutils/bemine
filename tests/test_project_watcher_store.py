from bemine.config import BemineSettings
from bemine.db import BemineDB


def _db(tmp_path):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    return BemineDB(settings.db_path)


def test_upsert_and_get_project_watcher(tmp_path):
    db = _db(tmp_path)
    db.upsert_project_watcher(
        project_id="p1", channel_id="c1", brief="long brief", interval_secs=600
    )
    row = db.get_project_watcher("p1")
    assert row["channel_id"] == "c1"
    assert row["brief"] == "long brief"
    assert row["interval_secs"] == 600
    assert row["status"] == "active"
    assert row["awaiting_reply"] == 0
    assert row["last_tick_at"] == 0


def test_upsert_is_idempotent_and_updates_brief(tmp_path):
    db = _db(tmp_path)
    db.upsert_project_watcher(project_id="p1", channel_id="c1", brief="a", interval_secs=300)
    db.upsert_project_watcher(project_id="p1", channel_id="c1", brief="b", interval_secs=120)
    row = db.get_project_watcher("p1")
    assert row["brief"] == "b"
    assert row["interval_secs"] == 120
    assert db.get_project_watcher("p1")["status"] == "active"


def test_get_by_channel_and_list_active(tmp_path):
    db = _db(tmp_path)
    db.upsert_project_watcher(project_id="p1", channel_id="c1", brief="a", interval_secs=300)
    db.upsert_project_watcher(project_id="p2", channel_id="c2", brief="b", interval_secs=300)
    db.update_project_watcher("p2", status="stopped")
    assert db.get_project_watcher_by_channel("c1")["project_id"] == "p1"
    active = db.list_active_project_watchers()
    assert [r["project_id"] for r in active] == ["p1"]


def test_update_fields(tmp_path):
    db = _db(tmp_path)
    db.upsert_project_watcher(project_id="p1", channel_id="c1", brief="a", interval_secs=300)
    db.update_project_watcher("p1", awaiting_reply=1, last_tick_at=12.5, empty_ticks=2)
    row = db.get_project_watcher("p1")
    assert row["awaiting_reply"] == 1
    assert row["last_tick_at"] == 12.5
    assert row["empty_ticks"] == 2


def test_channel_id_for_project(tmp_path):
    db = _db(tmp_path)
    db.upsert_bemine_session(
        session_id="s1", channel_id="c9", project_id="p1", runtime="kimi",
        model="kimi-k2.6", device_id="cdjk", cwd="/tmp", status="active", summary="",
    )
    assert db.get_channel_id_for_project("p1") == "c9"
    assert db.get_channel_id_for_project("nope") is None
