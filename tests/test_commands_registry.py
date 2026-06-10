from pathlib import Path
from bemine.config import BemineSettings
from bemine.commands import registry, run_command


def _settings(tmp_path):
    return BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")


def test_registry_lists_deterministic_ops():
    names = set(registry().keys())
    assert {"status", "rundown", "jobs.list", "usage", "capabilities", "continuity", "help", "discord.routes"} <= names


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

[phantom]
project_root = "/tmp/phantom"

[dispatch]
runtime = "kimi"

[kimi]
model = "kimi-k2.6"
backend = "native"

[subagents]
files = ["subagents/PA.md", "subagents/SMM.md", "subagents/style.md"]
""",
        encoding="utf-8",
    )
    subagents = tmp_path / "subagents"
    subagents.mkdir()
    for name in ("PA.md", "SMM.md", "style.md"):
        (subagents / name).write_text(f"# {name}\n", encoding="utf-8")
    monkeypatch.setenv("BEMINE_CONFIG", str(config))
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")

    out = run_command("help", settings, {})

    assert "Bemine help:" in out.text
    assert "runtime=shell interval=123s" in out.text
    assert "enabled=False devices=cdjk, mbp" in out.text
    assert "subagents: PA, SMM, style" in out.text
    assert "project_root=/tmp/phantom" in out.text
    assert out.data["goal"]["runtime"] == "shell"
    assert out.data["dispatch"]["runtime"] == "kimi"


def test_run_command_discord_routes_smoke(tmp_path):
    settings = _settings(tmp_path)
    out = run_command("discord.routes", settings, {})
    assert "Discord route management:" in out.text
    assert isinstance(out.data, dict)
