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

    assert "Bemine health:" in health
    assert "goal sessions" in goals.lower()


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


def test_slash_unknown_command(tmp_path):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    text = slash_command_text(settings, "nope", {})
    assert "Unknown" in text
