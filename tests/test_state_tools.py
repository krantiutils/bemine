from pathlib import Path

from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.context.tool_registry import BemineToolRegistry, ToolContext


def _ctx(tmp_path):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    db = BemineDB(settings.db_path)
    db.upsert_bemine_session(
        session_id="s1", channel_id="c1", project_id="p1", runtime="kimi",
        model="kimi-k2.6", device_id="local", cwd=str(tmp_path), status="active", summary="")
    return settings, db, ToolContext(settings=settings, db=db, session_id="s1",
                                     project_id="p1", cwd=tmp_path)


def test_bemine_config_tool_reports_active_runtime(tmp_path):
    settings, db, ctx = _ctx(tmp_path)
    reg = BemineToolRegistry()
    out = reg.execute("bemine_config", {}, ctx)
    assert out["active_runtime"] == "kimi"
    assert out["active_model"] == "kimi-k2.6"
    assert "kimi" in out["available_runtimes"]


def test_usage_report_tool(tmp_path):
    settings, db, ctx = _ctx(tmp_path)
    db.record_usage_sample(provider="kimi", raw_json="{}", input_tokens=5, output_tokens=2, total_tokens=7)
    out = BemineToolRegistry().execute("usage_report", {}, ctx)
    assert any(p["provider"] == "kimi" for p in out["providers"])


def test_jobs_list_tool_runs(tmp_path):
    settings, db, ctx = _ctx(tmp_path)
    out = BemineToolRegistry().execute("jobs_list", {}, ctx)
    assert "jobs" in out and isinstance(out["jobs"], list)


def test_backlog_tools_write_and_list_vault_items(tmp_path):
    settings, db, ctx = _ctx(tmp_path)
    reg = BemineToolRegistry()

    added = reg.execute(
        "backlog_add",
        {"owner": "SMM", "text": "post the Rai checkpoint chart"},
        ctx,
    )
    assert added["ok"] is True
    assert Path(added["entry"]["path"]).exists()

    listed = reg.execute("backlog_list", {"owner": "SMM"}, ctx)
    assert listed["owner"] == "SMM"
    assert "post the Rai checkpoint chart" in listed["items"][0]


def test_new_tools_convert_with_real_names(tmp_path):
    from bemine.providers._anthropic_adapter import convert_tools_to_anthropic
    schemas = BemineToolRegistry().openai_schemas()
    names = {t["name"] for t in convert_tools_to_anthropic(schemas)}
    assert {"bemine_config", "jobs_list", "usage_report", "backlog_add"} <= names
    assert all(t["name"] for t in convert_tools_to_anthropic(schemas))
