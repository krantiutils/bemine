from pathlib import Path
from bemine.config import BemineSettings
from bemine.commands import registry, run_command
from bemine.db import BemineDB
from bemine.discord.store import Store


def _settings(tmp_path):
    return BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")


def test_registry_lists_deterministic_ops():
    names = set(registry().keys())
    assert {
        "status",
        "rundown",
        "jobs.list",
        "backlog.list",
        "config.show",
        "subagents.list",
        "subagents.show",
        "phantom.status",
        "phantom.build",
        "phantom.install",
        "smm.draft",
        "usage",
        "capabilities",
        "continuity",
        "help",
        "discord.routes",
        "discord.sync_plan",
    } <= names


def test_run_command_is_pure_and_deterministic(tmp_path):
    settings = _settings(tmp_path)
    out = run_command("jobs.list", settings, {})
    assert isinstance(out.text, str)
    assert isinstance(out.data, dict)


def test_run_command_usage(tmp_path):
    settings = _settings(tmp_path)
    out = run_command("usage", settings, {})
    assert isinstance(out.text, str)


def test_run_command_continuity(tmp_path):
    settings = _settings(tmp_path)
    out = run_command("continuity", settings, {})
    assert "Bemine continuity:" in out.text
    assert isinstance(out.data, dict)


def test_run_command_config_show(tmp_path, monkeypatch):
    config = tmp_path / "bemine.toml"
    config.write_text("[backlog]\ndefault_owner = \"PA\"\n", encoding="utf-8")
    monkeypatch.setenv("BEMINE_CONFIG", str(config))
    settings = _settings(tmp_path)
    out = run_command("config.show", settings, {})
    assert "[backlog]" in out.text
    assert out.data["path"] == str(config)
    assert out.data["config"]["backlog"]["default_owner"] == "PA"


def test_run_command_subagents_list_and_show(tmp_path, monkeypatch):
    config = tmp_path / "bemine.toml"
    profile_dir = tmp_path / "subagents"
    profile_dir.mkdir()
    (profile_dir / "PA.md").write_text("# PA\nplan the day", encoding="utf-8")
    (profile_dir / "SMM.md").write_text("# SMM\npost ideas", encoding="utf-8")
    config.write_text(
        """
[subagents]
files = ["subagents/PA.md", "subagents/SMM.md"]
context_limit = 1000
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("BEMINE_CONFIG", str(config))
    settings = _settings(tmp_path)

    listed = run_command("subagents.list", settings, {})
    shown = run_command("subagents.show", settings, {"name": "SMM"})

    assert "Subagent profiles:" in listed.text
    assert listed.data["profiles"][0]["name"] == "PA"
    assert "post ideas" in shown.text
    assert shown.data["name"] == "SMM"


def test_run_command_phantom_status_smoke(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    payload = {"ok": True, "status": "ready"}
    monkeypatch.setattr("bemine.commands.phantom_status", lambda **kwargs: payload)
    monkeypatch.setattr("bemine.commands.render_phantom_status", lambda data: "phantom ok\n")

    out = run_command("phantom.status", settings, {})

    assert out.text == "phantom ok\n"
    assert out.data == payload


def test_run_command_backlog_list(tmp_path):
    settings = _settings(tmp_path)
    out = run_command("backlog.list", settings, {})
    assert "backlog" in out.text.lower()
    assert out.data["owner"] == "SMM"


def test_run_command_discord_sync_plan_is_dry_run(tmp_path):
    settings = _settings(tmp_path)
    project = tmp_path / "rai"
    project.mkdir()
    db = BemineDB(settings.db_path)
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO projects(project_id, name, cwd, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, 1.0, 2.0)
            """,
            ("proj_rai", "rai", str(project)),
        )
    Store(settings.db_path).record_channel_metadata(
        channel_id="chan-old",
        guild_id="guild-1",
        channel_name="old-project",
        category_name="Bemine Projects",
        channel_type=0,
    )

    out = run_command("discord.sync_plan", settings, {"prune": True})

    assert "Discord channel sync plan:" in out.text
    assert "mode: dry-run only" in out.text
    assert out.data["dry_run"] is True
    assert out.data["would_create"][0]["channel_name"] == "rai"
    assert out.data["would_prune"][0]["channel_name"] == "old-project"


def test_run_command_help_summarizes_configurable_surfaces(tmp_path, monkeypatch):
    config = tmp_path / "bemine.toml"
    config.write_text(
        """
[goal]
runtime = "shell"
interval_secs = 123

[continuity]
enabled = false
devices = ["cdjk", "mbp"]

[backlog]
default_owner = "PA"
owners = ["PA", "SMM"]

[phantom]
project_root = "/tmp/phantom"
application_id = "dev.phantom.service"
accessibility_service = ".PhantomAccessibilityService"
java_home = "/usr/lib/jvm/java-17-openjdk-amd64"
android_home = "/home/cdjk/Android/Sdk"

[dispatch]
runtime = "kimi"

[kimi]
model = "kimi-k2.6"
backend = "native"

[subagents]
files = ["subagents/PA.md", "subagents/SMM.md", "subagents/style.md"]
context_limit = 1000
""",
        encoding="utf-8",
    )
    subagents = tmp_path / "subagents"
    subagents.mkdir()
    for name in ("PA.md", "SMM.md", "style.md"):
        (subagents / name).write_text(f"# {name}\n", encoding="utf-8")
    monkeypatch.setenv("BEMINE_CONFIG", str(config))
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    BemineDB(settings.db_path).upsert_job(
        job_id="morning_plan",
        name="Morning plan",
        enabled=True,
        trigger_type="cron",
        schedule="30 8 * * *",
        config_json='{"type":"morning_plan"}',
        next_run_at="2026-06-11T08:30:00+05:45",
    )

    out = run_command("help", settings, {})

    assert "Bemine help:" in out.text
    assert "runtime=shell interval=123s" in out.text
    assert "enabled=False devices=cdjk, mbp" in out.text
    assert "goal.runtime=shell" in out.text
    assert "jobs.session_fallback_channel=session-updates" in out.text
    assert "recovery.poll_interval_secs=300" in out.text
    assert "discord routes:" in out.text
    assert "discord sync plan:" in out.text
    assert "inspect ops:" in out.text
    assert "goal sessions: `goal <project> <brief>`" in out.text
    assert "continuity:" in out.text
    assert "wake sessions after reboot?" in out.text
    assert "focus today:" in out.text
    assert "dream: `dream run`" in out.text
    assert "watchers: active=0" in out.text
    assert "backlog: `backlog add` / `backlog list` / `show backlog` / `show PA backlog` / `show SMM backlog` / `remember this for SMM|PA: ...`" in out.text
    assert "backlog.default_owner=PA" in out.text
    assert "backlog.owners=PA, SMM" in out.text
    assert "subagents.files=subagents/PA.md, subagents/SMM.md, subagents/style.md" in out.text
    assert "subagents.context_limit=1000" in out.text
    assert "phantom.application_id=dev.phantom.service" in out.text
    assert "phantom.accessibility_service=.PhantomAccessibilityService" in out.text
    assert "phantom.java_home=/usr/lib/jvm/java-17-openjdk-amd64" in out.text
    assert "phantom.android_home=/home/cdjk/Android/Sdk" in out.text
    assert "config: `config show` / `what config is set?`" in out.text
    assert "social posts:" in out.text
    assert "social posts ask:" in out.text
    assert "subagents: PA, SMM, style" in out.text
    assert "style convention:" in out.text
    assert "phantom: project_root=/tmp/phantom" in out.text
    assert "phantom status" in out.text
    assert "phantom build" in out.text
    assert "Configured jobs:" in out.text
    assert "morning_plan" in out.text
    assert out.data["goal"]["runtime"] == "shell"
    assert out.data["dispatch"]["runtime"] == "kimi"
    assert out.data["backlog"]["default_owner"] == "PA"
    assert out.data["backlog"]["owners"] == ["PA", "SMM"]
    assert out.data["config"]["path"].endswith("bemine.toml")
    assert out.data["config"]["config"]["backlog"]["default_owner"] == "PA"
    assert out.data["subagents_config"]["context_limit"] == 1000
    assert out.data["watchers"]["active"] == 0
    assert "phantom.project_root=/tmp/phantom" in out.data["config_knobs"]
    assert "subagents.files=subagents/PA.md, subagents/SMM.md, subagents/style.md" in out.data["config_knobs"]
    assert "phantom.application_id=dev.phantom.service" in out.data["config_knobs"]
    assert any(job["job_id"] == "morning_plan" for job in out.data["jobs"])


def test_run_command_discord_routes_smoke(tmp_path):
    settings = _settings(tmp_path)
    out = run_command("discord.routes", settings, {})
    assert "Discord route management:" in out.text
    assert isinstance(out.data, dict)
