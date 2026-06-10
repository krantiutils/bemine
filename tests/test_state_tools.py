import asyncio
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


def test_job_run_tool_runs_job_without_notification_by_default(tmp_path, monkeypatch):
    settings, db, ctx = _ctx(tmp_path)
    calls = {}

    class Result:
        job_id = "dream"
        status = "success"
        output = "Dream recorded"
        notified = False

    def fake_run_job_by_id(settings_arg, job_id, **kwargs):
        calls["settings"] = settings_arg
        calls["job_id"] = job_id
        calls.update(kwargs)
        sent = kwargs["sender"]("output")
        calls["sender_result"] = sent
        return Result()

    monkeypatch.setattr("bemine.context.tool_registry.run_job_by_id", fake_run_job_by_id)

    out = BemineToolRegistry().execute("job_run", {"job_id": "dream"}, ctx)

    assert out["ok"] is True
    assert out["job_id"] == "dream"
    assert out["status"] == "success"
    assert out["notified"] is False
    assert out["output"] == "Dream recorded"
    assert calls["settings"] is settings
    assert calls["job_id"] == "dream"
    assert calls["sender_result"] == {"id": "", "channel_id": ""}


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


def test_phantom_status_tool_uses_shared_workflow(tmp_path, monkeypatch):
    settings, db, ctx = _ctx(tmp_path)
    calls = {}

    def fake_phantom_status(**kwargs):
        calls.update(kwargs)
        return {
            "schema": "bemine.phantom.v1",
            "ok": True,
            "status": "blocked",
            "blockers": ["Android device phone is unauthorized; authorize USB debugging."],
        }

    monkeypatch.setattr("bemine.context.tool_registry.phantom_status", fake_phantom_status)

    out = BemineToolRegistry().execute(
        "phantom_status",
        {"project_root": "/tmp/phantom", "serial": "phone", "java_home": "/jdk"},
        ctx,
    )

    assert out["schema"] == "bemine.phantom.v1"
    assert out["status"] == "blocked"
    assert calls["project_root"] == "/tmp/phantom"
    assert calls["serial"] == "phone"
    assert calls["java_home"] == "/jdk"


def test_phantom_install_tool_uses_shared_workflow(tmp_path, monkeypatch):
    settings, db, ctx = _ctx(tmp_path)
    calls = {}

    def fake_phantom_install(**kwargs):
        calls.update(kwargs)
        return {
            "ok": True,
            "status": "dry_run",
            "command": ["/usr/bin/adb", "-s", "phone", "install", "-r", "app.apk"],
        }

    monkeypatch.setattr("bemine.context.tool_registry.phantom_install", fake_phantom_install)

    out = BemineToolRegistry().execute(
        "phantom_install",
        {
            "project_root": "/tmp/phantom",
            "serial": "phone",
            "build": True,
            "dry_run": True,
            "java_home": "/jdk",
        },
        ctx,
    )

    assert out["ok"] is True
    assert out["status"] == "dry_run"
    assert calls["project_root"] == "/tmp/phantom"
    assert calls["serial"] == "phone"
    assert calls["build"] is True
    assert calls["dry_run"] is True
    assert calls["java_home"] == "/jdk"


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


def test_pa_plan_tool_uses_shared_planner(tmp_path, monkeypatch):
    settings, db, ctx = _ctx(tmp_path)
    calls = {}

    class Result:
        def to_dict(self):
            return {
                "generated_at": "2026-06-11T00:00:00+00:00",
                "items": [{"priority": 1, "text": "fix health"}],
                "evidence": {"statuses": 0},
                "path": "",
            }

    def fake_build_pa_plan(settings_arg, **kwargs):
        calls["settings"] = settings_arg
        calls.update(kwargs)
        return Result()

    monkeypatch.setattr("bemine.context.tool_registry.build_pa_plan", fake_build_pa_plan)

    out = BemineToolRegistry().execute(
        "pa_plan",
        {"limit": 4, "write": True},
        ctx,
    )

    assert out["ok"] is True
    assert out["result"]["items"][0]["text"] == "fix health"
    assert calls["settings"] is settings
    assert calls["limit"] == 4
    assert calls["write"] is True
    assert calls["db"] is db


def test_smm_drafts_tool_uses_shared_drafter(tmp_path, monkeypatch):
    settings, db, ctx = _ctx(tmp_path)
    calls = {}

    class Result:
        def to_dict(self):
            return {
                "generated_at": "2026-06-11T00:00:00+00:00",
                "drafts": [{"project": "rai", "hook": "rai shipped"}],
                "assets": [],
                "style_path": str(tmp_path / "subagents" / "style.md"),
                "style_exists": True,
                "path": "",
            }

    def fake_draft_smm_posts(settings_arg, **kwargs):
        calls["settings"] = settings_arg
        calls.update(kwargs)
        return Result()

    (tmp_path / "subagents").mkdir()
    (tmp_path / "subagents" / "style.md").write_text("# style\n", encoding="utf-8")
    monkeypatch.setattr("bemine.context.tool_registry.draft_smm_posts", fake_draft_smm_posts)

    out = BemineToolRegistry().execute(
        "smm_drafts",
        {"topic": "Rai checkpoint", "project": "rai", "limit": 2, "write": True},
        ctx,
    )

    assert out["ok"] is True
    assert out["result"]["drafts"][0]["hook"] == "rai shipped"
    assert calls["settings"] is settings
    assert calls["topic"] == "Rai checkpoint"
    assert calls["project"] == "rai"
    assert calls["limit"] == 2
    assert calls["write"] is True
    assert calls["db"] is db
    assert calls["project_root"] == tmp_path


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


def test_workers_status_tool_lists_workers_and_duplicates(tmp_path, monkeypatch):
    settings, db, ctx = _ctx(tmp_path)
    calls = {}

    class Worker:
        def __init__(self, name, project_id, runtime="shell", status="running"):
            self.name = name
            self.project_id = project_id
            self.runtime = runtime
            self.status = status

        def to_dict(self):
            return {
                "name": self.name,
                "runtime": self.runtime,
                "transport": "tmux",
                "device_id": "local",
                "project_id": self.project_id,
                "cwd": f"/tmp/{self.project_id}",
                "command": "cat",
                "status": self.status,
                "exists": self.status == "running",
            }

    class FakeController:
        def __init__(self, settings_arg, *, db=None):
            calls["settings"] = settings_arg
            calls["db"] = db

        def list(self):
            return [
                Worker("goal-rai", "rai"),
                Worker("rai-kimi", "rai", runtime="kimi"),
                Worker("stopped", "old", status="stopped"),
            ]

    monkeypatch.setattr("bemine.context.tool_registry.WorkerController", FakeController)

    out = BemineToolRegistry().execute("workers_status", {"query": "rai"}, ctx)

    assert out["ok"] is True
    assert [row["name"] for row in out["workers"]] == ["goal-rai", "rai-kimi"]
    assert out["duplicates"][0]["project_id"] == "rai"
    assert {row["name"] for row in out["duplicates"][0]["heads"]} == {"goal-rai", "rai-kimi"}
    assert calls["settings"] is settings
    assert calls["db"] is db


def test_watchers_status_tool_lists_active_redacted_watchers(tmp_path):
    settings, db, ctx = _ctx(tmp_path)
    db.upsert_project_watcher(
        project_id="rai",
        channel_id="c-rai",
        brief="train with TOKEN=abc123",
        interval_secs=600,
    )
    db.upsert_project_watcher(
        project_id="bm",
        channel_id="c-bm",
        brief="ship native tools",
        interval_secs=300,
    )

    out = BemineToolRegistry().execute("watchers_status", {"query": "rai"}, ctx)

    assert out["ok"] is True
    assert len(out["watchers"]) == 1
    watcher = out["watchers"][0]
    assert watcher["project_id"] == "rai"
    assert watcher["channel_id"] == "c-rai"
    assert watcher["awaiting_reply"] is False
    assert "abc123" not in watcher["brief"]
    assert "TOKEN=<secret>" in watcher["brief"]


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


def test_discord_refresh_channels_tool_updates_local_metadata(tmp_path, monkeypatch):
    settings, db, ctx = _ctx(tmp_path)
    calls = {}

    class FakeDiscordSettings:
        token = "bot-token"

        @classmethod
        def load(cls):
            return cls()

    def fake_default_guild_id(token):
        calls["default_token"] = token
        return "guild-default"

    def fake_refresh_channel_metadata(store, *, token, guild_id, channel_ids=None):
        calls["store_path"] = store.path
        calls["token"] = token
        calls["guild_id"] = guild_id
        calls["channel_ids"] = channel_ids
        return [
            {
                "channel_id": "c1",
                "guild_id": guild_id,
                "channel_name": "voidash",
                "category_name": "Projects",
                "channel_type": 0,
            },
            {
                "channel_id": "thread-1",
                "guild_id": guild_id,
                "channel_name": "launch-plan",
                "category_name": "voidash",
                "channel_type": 11,
            },
        ]

    monkeypatch.setattr("bemine.discord.config.Settings", FakeDiscordSettings)
    monkeypatch.setattr("bemine.discord.channel_index.default_guild_id", fake_default_guild_id)
    monkeypatch.setattr(
        "bemine.discord.channel_index.refresh_channel_metadata",
        fake_refresh_channel_metadata,
    )

    out = BemineToolRegistry().execute(
        "discord_refresh_channels",
        {"channel_ids": "thread-1,thread-2", "limit": 1},
        ctx,
    )

    assert out["ok"] is True
    assert out["guild_id"] == "guild-default"
    assert out["refreshed_count"] == 2
    assert len(out["channels"]) == 1
    assert calls["default_token"] == "bot-token"
    assert calls["store_path"] == settings.db_path
    assert calls["token"] == "bot-token"
    assert calls["guild_id"] == "guild-default"
    assert calls["channel_ids"] == ["thread-1", "thread-2"]


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


def test_channel_sync_plan_tool_returns_dry_run_plan(tmp_path, monkeypatch):
    settings, db, ctx = _ctx(tmp_path)
    calls = {}

    def fake_plan_project_channel_sync(settings_arg, **kwargs):
        calls["settings"] = settings_arg
        calls.update(kwargs)
        return {
            "dry_run": True,
            "would_create": [{"channel_name": "rai"}],
            "would_prune": [],
        }

    monkeypatch.setattr(
        "bemine.discord.dispatch.plan_project_channel_sync",
        fake_plan_project_channel_sync,
    )

    out = BemineToolRegistry().execute(
        "channel_sync_plan",
        {"limit": 10, "prune": True},
        ctx,
    )

    assert out["ok"] is True
    assert out["plan"]["dry_run"] is True
    assert out["plan"]["would_create"][0]["channel_name"] == "rai"
    assert calls["settings"] is settings
    assert calls["limit"] == 10
    assert calls["prune"] is True


def test_channel_sync_apply_requires_confirmation_and_returns_plan(tmp_path, monkeypatch):
    settings, db, ctx = _ctx(tmp_path)

    def fake_plan_project_channel_sync(settings_arg, **kwargs):
        return {
            "dry_run": True,
            "would_create": [{"channel_name": "rai"}],
            "would_prune": [],
            "limit": kwargs["limit"],
            "prune": kwargs["prune"],
        }

    monkeypatch.setattr(
        "bemine.discord.dispatch.plan_project_channel_sync",
        fake_plan_project_channel_sync,
    )

    out = BemineToolRegistry().execute(
        "channel_sync_apply",
        {"limit": 10, "prune": True, "confirm": False},
        ctx,
    )

    assert out["ok"] is False
    assert out["status"] == "confirmation_required"
    assert out["plan"]["would_create"][0]["channel_name"] == "rai"
    assert out["plan"]["prune"] is True


def test_channel_sync_apply_runs_confirmed_sync_inside_event_loop(tmp_path, monkeypatch):
    settings, db, ctx = _ctx(tmp_path)
    calls = {}

    def fake_plan_project_channel_sync(settings_arg, **kwargs):
        calls["plan_settings"] = settings_arg
        calls["plan_kwargs"] = kwargs
        return {"dry_run": True, "would_create": [], "would_prune": []}

    async def fake_sync_project_channels_to_discord(settings_arg, token, guild_id, **kwargs):
        calls["sync_settings"] = settings_arg
        calls["token"] = token
        calls["guild_id"] = guild_id
        calls["sync_kwargs"] = kwargs
        return {"guild_id": guild_id, "created": [{"name": "rai"}], "deleted": []}

    class FakeDiscordSettings:
        token = "bot-token"

        @classmethod
        def load(cls):
            return cls()

    monkeypatch.setattr(
        "bemine.discord.dispatch.plan_project_channel_sync",
        fake_plan_project_channel_sync,
    )
    monkeypatch.setattr(
        "bemine.discord.dispatch.sync_project_channels_to_discord",
        fake_sync_project_channels_to_discord,
    )
    monkeypatch.setattr("bemine.discord.config.Settings", FakeDiscordSettings)

    async def run_tool():
        return BemineToolRegistry().execute(
            "channel_sync_apply",
            {"limit": 25, "prune": True, "guild_id": "guild-1", "confirm": True},
            ctx,
        )

    out = asyncio.run(run_tool())

    assert out["ok"] is True
    assert out["status"] == "applied"
    assert out["result"]["created"][0]["name"] == "rai"
    assert calls["plan_settings"] is settings
    assert calls["sync_settings"] is settings
    assert calls["token"] == "bot-token"
    assert calls["guild_id"] == "guild-1"
    assert calls["sync_kwargs"] == {"limit": 25, "prune": True}


def test_new_tools_convert_with_real_names(tmp_path):
    from bemine.providers._anthropic_adapter import convert_tools_to_anthropic
    schemas = BemineToolRegistry().openai_schemas()
    names = {t["name"] for t in convert_tools_to_anthropic(schemas)}
    assert {
        "bemine_config",
        "jobs_list",
        "job_run",
        "health_status",
        "capabilities_status",
        "phantom_status",
        "phantom_install",
        "usage_report",
        "backlog_add",
        "brief_run",
        "pa_plan",
        "smm_drafts",
        "dream_run",
        "goal_start",
        "goal_status",
        "workers_status",
        "watchers_status",
        "discord_threads",
        "discord_transcript",
        "discord_refresh_channels",
        "channel_routes",
        "channel_sync_plan",
        "channel_sync_apply",
    } <= names
    assert all(t["name"] for t in convert_tools_to_anthropic(schemas))
