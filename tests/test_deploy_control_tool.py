from pathlib import Path
from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.context.tool_registry import BemineToolRegistry, ToolContext
from bemine.deploy.state import DeployState, state_path


def _ctx(tmp_path):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    db = BemineDB(settings.db_path)
    db.upsert_bemine_session(
        session_id="s1", channel_id="c1", project_id="bemine", runtime="kimi",
        model="kimi-k2.6", device_id="cdjk", cwd="/tmp", status="active", summary="",
    )
    return settings, db, ToolContext(settings=settings, db=db, session_id="s1",
                                     project_id="bemine", cwd=Path("/tmp"))


def test_deploy_control_request(tmp_path):
    settings, db, ctx = _ctx(tmp_path)
    reg = BemineToolRegistry()
    out = reg.execute("deploy_control", {"action": "request"}, ctx)
    assert out["ok"] is True
    assert DeployState.load(state_path(settings)).phase == "requested"
    assert DeployState.load(state_path(settings)).channel_id == "c1"


def test_deploy_control_status(tmp_path):
    settings, db, ctx = _ctx(tmp_path)
    reg = BemineToolRegistry()
    out = reg.execute("deploy_control", {"action": "status"}, ctx)
    assert out["phase"] == "idle"
