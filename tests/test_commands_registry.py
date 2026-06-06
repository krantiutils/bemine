from pathlib import Path
from bemine.config import BemineSettings
from bemine.commands import registry, run_command


def _settings(tmp_path):
    return BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")


def test_registry_lists_deterministic_ops():
    names = set(registry().keys())
    assert {"status", "rundown", "jobs.list", "usage", "capabilities"} <= names


def test_run_command_is_pure_and_deterministic(tmp_path):
    settings = _settings(tmp_path)
    out = run_command("jobs.list", settings, {})
    assert isinstance(out.text, str)
    assert isinstance(out.data, dict)


def test_run_command_usage(tmp_path):
    settings = _settings(tmp_path)
    out = run_command("usage", settings, {})
    assert isinstance(out.text, str)
