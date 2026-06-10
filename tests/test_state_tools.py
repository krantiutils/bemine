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


def test_health_status_tool_uses_shared_health_packet(tmp_path, monkeypatch):
    settings, db, ctx = _ctx(tmp_path)
    calls = {}

    def fake_collect_health(settings_arg, **kwargs):
        calls["settings"] = settings_arg
        calls.update(kwargs)
        return {
            "ok": True,
            "status": "ok",
            "deploy": {"phase": "confirmed", "target_sha": "sha1"},
            "services": [],
            "jobs": {"total": 0, "enabled": 0, "failed": 0, "failed_jobs": []},
            "watchers": {"active": 0, "items": []},
            "issues": [],
        }

    monkeypatch.setattr("bemine.context.tool_registry.collect_health", fake_collect_health)

    out = BemineToolRegistry().execute("health_status", {}, ctx)

    assert out["ok"] is True
    assert out["status"] == "ok"
    assert out["deploy"]["phase"] == "confirmed"
    assert calls["settings"] is settings
    assert calls["db"] is db


def test_capabilities_status_tool_scans_and_returns_manifest(tmp_path, monkeypatch):
    settings, db, ctx = _ctx(tmp_path)
    calls = {}

    class FakeInventory:
        def __init__(self, db_arg, *, device_id=""):
            calls["db"] = db_arg
            calls["device_id"] = device_id

        def scan(self):
            calls["scanned"] = True

    def fake_build_capability_manifest(rows, *, device_id):
        calls["rows"] = rows
        return {
            "schema": "bemine.capabilities.v1",
            "device_id": device_id,
            "credentials": [],
            "tools": {
                "adb": {
                    "available": True,
                    "status": "attention_required",
                    "devices": [{"serial": "phone", "state": "unauthorized"}],
                }
            },
            "worker_contract": [],
        }

    monkeypatch.setattr("bemine.context.tool_registry.CredentialInventory", FakeInventory)
    monkeypatch.setattr(
        "bemine.context.tool_registry.build_capability_manifest",
        fake_build_capability_manifest,
    )

    out = BemineToolRegistry().execute("capabilities_status", {}, ctx)

    assert out["schema"] == "bemine.capabilities.v1"
    assert out["device_id"] == settings.device_id
    assert out["tools"]["adb"]["devices"][0]["state"] == "unauthorized"
    assert calls["db"] is db
    assert calls["device_id"] == settings.device_id
    assert calls["scanned"] is True
    assert calls["rows"] == []


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


def test_brief_run_tool_generates_pa_smm_brief(tmp_path, monkeypatch):
    settings, db, ctx = _ctx(tmp_path)

    class Result:
        def to_dict(self):
            return {
                "path": str(settings.vault_dir / "Briefs" / "brief.md"),
                "items": [],
                "backlog_entries": [],
                "evidence": {},
            }

    calls = {}

    def fake_run_brief(settings_arg, **kwargs):
        calls.update(kwargs)
        return Result()

    monkeypatch.setattr("bemine.context.tool_registry.run_brief", fake_run_brief)

    out = BemineToolRegistry().execute(
        "brief_run",
        {"limit": 25, "record_backlog": False},
        ctx,
    )

    assert out["ok"] is True
    assert out["result"]["path"].endswith("brief.md")
    assert calls["limit"] == 25
    assert calls["record_backlog"] is False
    assert calls["db"] is db


def test_dream_run_tool_records_dream(tmp_path, monkeypatch):
    settings, db, ctx = _ctx(tmp_path)

    class Result:
        def to_dict(self):
            return {
                "path": str(settings.vault_dir / "Dreams" / "dreams.jsonl"),
                "data": {"schema": "bemine.dream.v1", "signals": []},
            }

    calls = {}

    def fake_run_dream(settings_arg, **kwargs):
        calls["settings"] = settings_arg
        calls.update(kwargs)
        return Result()

    monkeypatch.setattr("bemine.context.tool_registry.run_dream", fake_run_dream)

    out = BemineToolRegistry().execute("dream_run", {"limit": 25}, ctx)

    assert out["ok"] is True
    assert out["result"]["data"]["schema"] == "bemine.dream.v1"
    assert out["result"]["path"].endswith("dreams.jsonl")
    assert calls["settings"] is settings
    assert calls["limit"] == 25
    assert calls["db"] is db


def test_goal_start_tool_uses_current_context_defaults(tmp_path, monkeypatch):
    settings, db, ctx = _ctx(tmp_path)
    calls = []

    class Worker:
        name = "goal-p1"

        def to_dict(self):
            return {
                "name": self.name,
                "runtime": "shell",
                "cwd": str(tmp_path),
                "project_id": "p1",
            }

    class Result:
        worker = Worker()
        watcher = {"project_id": "p1", "channel_id": "c1", "interval_secs": 600}
        prompt = "# Bemine Goal Session\ntrain"

    def fake_start_goal(settings_arg, **kwargs):
        calls.append({"settings": settings_arg, **kwargs})
        return Result()

    monkeypatch.setattr("bemine.context.tool_registry.start_goal", fake_start_goal)

    out = BemineToolRegistry().execute("goal_start", {"brief": "train"}, ctx)

    assert out["ok"] is True
    assert out["worker"]["name"] == "goal-p1"
    assert out["watcher"]["channel_id"] == "c1"
    assert out["prompt_len"] == len(Result.prompt)
    assert calls[0]["settings"] is settings
    assert calls[0]["project"] == "p1"
    assert calls[0]["brief"] == "train"
    assert calls[0]["cwd"] == tmp_path
    assert calls[0]["channel_id"] == "c1"
    assert calls[0]["interval_secs"] == 0
    assert calls[0]["db"] is db


def test_goal_start_tool_named_project_does_not_reuse_current_cwd(tmp_path, monkeypatch):
    settings, db, ctx = _ctx(tmp_path)
    calls = {}

    class Worker:
        name = "goal-rai"

        def to_dict(self):
            return {"name": self.name, "runtime": "shell", "cwd": "/repo/rai"}

    class Result:
        worker = Worker()
        watcher = {"project_id": "rai", "channel_id": "c1", "interval_secs": 45}
        prompt = "prompt"

    def fake_start_goal(settings_arg, **kwargs):
        calls.update({"settings": settings_arg, **kwargs})
        return Result()

    monkeypatch.setattr("bemine.context.tool_registry.start_goal", fake_start_goal)

    out = BemineToolRegistry().execute(
        "goal_start",
        {"project": "rai-speech-toolkit", "brief": "train", "interval_secs": 45},
        ctx,
    )

    assert out["ok"] is True
    assert calls["project"] == "rai-speech-toolkit"
    assert calls["cwd"] is None
    assert calls["interval_secs"] == 45
    assert calls["db"] is db


def test_goal_status_tool_lists_goal_sessions(tmp_path, monkeypatch):
    settings, db, ctx = _ctx(tmp_path)
    calls = {}

    class Session:
        def to_dict(self):
            return {
                "project_id": "p1",
                "worker": {"name": "goal-p1"},
                "watcher": {"status": "active"},
            }

    def fake_list_goal_sessions(settings_arg, **kwargs):
        calls.update({"settings": settings_arg, **kwargs})
        return [Session()]

    monkeypatch.setattr(
        "bemine.context.tool_registry.list_goal_sessions",
        fake_list_goal_sessions,
    )

    out = BemineToolRegistry().execute("goal_status", {"query": "p1"}, ctx)

    assert out["ok"] is True
    assert out["sessions"][0]["project_id"] == "p1"
    assert calls["settings"] is settings
    assert calls["status"] == "p1"
    assert calls["db"] is db


def test_discord_history_tools_read_local_redacted_store(tmp_path):
    from bemine.discord.store import Store

    settings, db, ctx = _ctx(tmp_path)
    store = Store(settings.db_path)
    store.record_inbound_message(
        message_id="m1",
        channel_id="c1",
        channel_name="voidash",
        category_name="Projects",
        guild_id="guild-1",
        author_id="user-1",
        author_name="voidash",
        content="Rai goal update with SSHPASS='letmein'",
        is_dm=False,
        created_at="2026-06-10T10:01:00+05:45",
    )
    store.record_outbound(
        discord_message_id="b1",
        channel_id="c1",
        content="Started goal worker goal-rai.",
        files=[],
    )

    reg = BemineToolRegistry()
    threads = reg.execute("discord_threads", {"query": "SSHPASS"}, ctx)
    assert threads["ok"] is True
    assert threads["threads"][0]["channel_id"] == "c1"
    assert threads["threads"][0]["channel_name"] == "voidash"
    assert "letmein" not in threads["threads"][0]["latest_content"]
    assert "SSHPASS='<secret>'" in threads["threads"][0]["latest_content"]

    transcript = reg.execute("discord_transcript", {"limit": 5}, ctx)
    assert transcript["ok"] is True
    assert [row["message_id"] for row in transcript["messages"]] == ["m1", "b1"]
    assert transcript["messages"][0]["channel_name"] == "voidash"
    assert transcript["messages"][-1]["role"] == "assistant"
    assert "letmein" not in str(transcript["messages"])


def test_channel_routes_tool_reports_config_db_and_bindings(tmp_path):
    import json

    settings, db, ctx = _ctx(tmp_path)
    configured_dir = tmp_path / "configured"
    indexed_dir = tmp_path / "indexed"
    configured_dir.mkdir()
    indexed_dir.mkdir()
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    (settings.state_dir / "discord-projects.json").write_text(
        json.dumps(
            {
                "my-route": {
                    "cwd": str(configured_dir),
                    "project_id": "p_route",
                    "project_name": "route-project",
                }
            }
        ),
        encoding="utf-8",
    )
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO projects(project_id, name, cwd, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, 1.0, 2.0)
            """,
            ("p_index", "rai", str(indexed_dir),),
        )
    db.upsert_bemine_session(
        session_id="s-route",
        channel_id="c-route",
        project_id="p_route",
        runtime="kimi",
        model="kimi-k2.6",
        device_id="local",
        cwd=str(configured_dir),
        status="active",
        summary="",
    )
    db.upsert_project_watcher(
        project_id="p_route",
        channel_id="c-watch",
        brief="keep going",
        interval_secs=600,
    )

    out = BemineToolRegistry().execute("channel_routes", {"query": "route"}, ctx)

    assert out["ok"] is True
    assert out["config_exists"] is True
    assert out["configured"][0]["channel_name"] == "my-route"
    assert out["configured"][0]["latest_channel_id"] == "c-route"
    assert out["configured"][0]["watcher_channel_id"] == "c-watch"

    indexed = BemineToolRegistry().execute("channel_routes", {"query": "rai"}, ctx)
    assert indexed["indexed"][0]["project_id"] == "p_index"
    assert indexed["indexed"][0]["channel_name"] == "rai"


def test_new_tools_convert_with_real_names(tmp_path):
    from bemine.providers._anthropic_adapter import convert_tools_to_anthropic
    schemas = BemineToolRegistry().openai_schemas()
    names = {t["name"] for t in convert_tools_to_anthropic(schemas)}
    assert {
        "bemine_config",
        "jobs_list",
        "health_status",
        "capabilities_status",
        "usage_report",
        "backlog_add",
        "brief_run",
        "dream_run",
        "goal_start",
        "goal_status",
        "discord_threads",
        "discord_transcript",
        "channel_routes",
    } <= names
    assert all(t["name"] for t in convert_tools_to_anthropic(schemas))
