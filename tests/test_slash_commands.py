from pathlib import Path
from bemine.config import BemineSettings
from mayor_discord.gateway import slash_command_text


def test_slash_command_calls_registry(tmp_path):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    text = slash_command_text(settings, "jobs", {})
    assert isinstance(text, str) and text  # came from commands.run_command, no LLM


def test_slash_unknown_command(tmp_path):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    text = slash_command_text(settings, "nope", {})
    assert "Unknown" in text
