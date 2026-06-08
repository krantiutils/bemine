from pathlib import Path
from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.context.tool_registry import BemineToolRegistry, ToolContext


def _ctx(tmp_path, project_id="channel:rai-speech-toolkit"):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    db = BemineDB(settings.db_path)
    db.upsert_bemine_session(
        session_id="sess1", channel_id="c1", project_id=project_id, runtime="kimi",
        model="kimi-k2.6", device_id="cdjk", cwd="/tmp", status="active", summary="",
    )
    return settings, db, ToolContext(settings=settings, db=db, session_id="sess1",
                                     project_id=project_id, cwd=Path("/tmp"))


def test_watch_control_start_creates_watcher(tmp_path):
    settings, db, ctx = _ctx(tmp_path)
    reg = BemineToolRegistry()
    out = reg.execute("watch_control",
                      {"action": "start", "brief": "get training running",
                       "interval_secs": 600}, ctx)
    assert out["status"] == "active"
    row = db.get_project_watcher("channel:rai-speech-toolkit")
    assert row["brief"] == "get training running"
    assert row["interval_secs"] == 600
    assert row["channel_id"] == "c1"


def test_watch_control_stop_and_status(tmp_path):
    settings, db, ctx = _ctx(tmp_path)
    reg = BemineToolRegistry()
    reg.execute("watch_control", {"action": "start", "brief": "b"}, ctx)
    status = reg.execute("watch_control", {"action": "status"}, ctx)
    assert status["status"] == "active"
    stopped = reg.execute("watch_control", {"action": "stop"}, ctx)
    assert stopped["status"] == "stopped"
    assert db.get_project_watcher("channel:rai-speech-toolkit")["status"] == "stopped"
