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
    continuity = slash_command_text(settings, "continuity", {})
    help_text = slash_command_text(settings, "help", {})

    assert "Bemine health:" in health
    assert "goal sessions" in goals.lower()
    assert "Bemine continuity:" in continuity
    assert "Bemine help:" in help_text


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
