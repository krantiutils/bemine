from pathlib import Path
from bemine.config import BemineSettings
from bemine.discord.gateway import slash_command_text


def test_slash_command_calls_registry(tmp_path):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    text = slash_command_text(settings, "jobs", {})
    assert isinstance(text, str) and text  # came from commands.run_command, no LLM


def test_slash_health_and_goals_are_deterministic(tmp_path):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")

    health = slash_command_text(settings, "health", {})
    goals = slash_command_text(settings, "goals", {})
    backlog = slash_command_text(settings, "backlog", {})
    continuity = slash_command_text(settings, "continuity", {})
    help_text = slash_command_text(settings, "help", {})
    routes = slash_command_text(settings, "routes", {})

    assert "Bemine health:" in health
    assert "goal sessions" in goals.lower()
    assert "backlog" in backlog.lower()
    assert "Bemine continuity:" in continuity
    assert "Bemine help:" in help_text
    assert "Discord route management:" in routes


def test_slash_backlog_can_filter_by_owner(tmp_path, monkeypatch):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    calls = {}

    def fake_list_backlog_items(settings_arg, *, owner, limit):
        calls["settings"] = settings_arg
        calls["owner"] = owner
        calls["limit"] = limit
        return [f"{owner}-item"]

    def fake_render_backlog_items(owner, items):
        return f"{owner} backlog:\n" + "\n".join(items) + "\n"

    monkeypatch.setattr("bemine.commands.list_backlog_items", fake_list_backlog_items)
    monkeypatch.setattr("bemine.commands.render_backlog_items", fake_render_backlog_items)

    text = slash_command_text(settings, "backlog", {"owner": "PA", "limit": 1})

    assert calls["settings"] == settings
    assert calls["owner"] == "PA"
    assert calls["limit"] == 1
    assert "PA backlog:" in text
    assert "PA-item" in text


def test_slash_pa_plan_is_deterministic(tmp_path, monkeypatch):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    monkeypatch.setattr("bemine.pa.collect_fleet_status", lambda *a, **k: ([], []))

    text = slash_command_text(settings, "pa", {})

    assert "PA plan:" in text


def test_slash_dispatch_reports_runtime(tmp_path, monkeypatch):
    config = tmp_path / "bemine.toml"
    config.write_text(
        """
[dispatch]
runtime = "kimi"

[kimi]
model = "kimi-k2.6"
backend = "native"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("BEMINE_CONFIG", str(config))
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")

    text = slash_command_text(settings, "dispatch", {})

    assert "Dispatch runtime:" in text
    assert "- runtime: kimi" in text
    assert "- backend: native" in text


def test_slash_goal_starts_goal_session(tmp_path, monkeypatch):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")

    class FakeWorker:
        name = "goal-rai"
        runtime = "shell"
        device_id = "testbox"
        cwd = "/tmp/repo"
        project_id = "p"
        exists = True

        def to_dict(self):
            return {
                "name": self.name,
                "runtime": self.runtime,
                "device_id": self.device_id,
                "cwd": self.cwd,
                "project_id": self.project_id,
                "exists": self.exists,
            }

    class FakeResult:
        worker = FakeWorker()
        watcher = {"project_id": "p", "interval_secs": 600, "channel_id": "123"}

        def to_dict(self):
            return {"worker": self.worker.to_dict(), "watcher": dict(self.watcher)}

    monkeypatch.setattr("bemine.goals.start_goal", lambda *a, **k: FakeResult())
    monkeypatch.setattr(
        "bemine.goals.render_goal_start",
        lambda result: "Goal session started:\n- worker: `goal-rai` [shell] device=testbox `/tmp/repo`\n",
    )

    text = slash_command_text(settings, "goal", {"project": "rai", "brief": "train checkpoint"})

    assert "Goal session started:" in text
    assert "goal-rai" in text


def test_slash_unknown_command(tmp_path):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    text = slash_command_text(settings, "nope", {})
    assert "Unknown" in text
