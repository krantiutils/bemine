from __future__ import annotations

from pathlib import Path

from bemine.config import BemineSettings, load_bemine_config
from mayor_discord.dispatch import (
    dispatch_status_payload,
    resolve_codex_model,
    resolve_dispatch_runtime,
)


def test_bemine_settings_load_from_single_config_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = tmp_path / "bemine.toml"
    config.write_text(
        """
[identity]
device_id = "configbox"

[paths]
state_dir = "state"
vault_dir = "vault"
codex_home = "codex"
claude_home = "claude"
kimi_home = "kimi"
hermes_home = "hermes"

[status]
devices = ["local", "k2"]
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BEMINE_CONFIG", str(config))

    settings = BemineSettings.load()

    assert load_bemine_config()["identity"]["device_id"] == "configbox"
    assert settings.device_id == "configbox"
    assert settings.state_dir == tmp_path / "state"
    assert settings.vault_dir == tmp_path / "vault"
    assert settings.codex_home == tmp_path / "codex"
    assert settings.claude_home == tmp_path / "claude"
    assert settings.kimi_home == tmp_path / "kimi"
    assert settings.hermes_home == tmp_path / "hermes"
    assert settings.status_devices == ["local", "k2"]


def test_dispatch_runtime_and_codex_model_load_from_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = tmp_path / "bemine.toml"
    config.write_text(
        """
[paths]
hermes_home = "hermes"

[dispatch]
runtime = "codex"

[codex]
model = "gpt-config-codex"
sandbox = "danger-full-access"
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BEMINE_CONFIG", str(config))
    monkeypatch.delenv("BEMINE_DISPATCH_RUNTIME", raising=False)
    monkeypatch.delenv("BEMINE_CODEX_MODEL", raising=False)
    settings = BemineSettings.load()

    payload = dispatch_status_payload(settings)

    assert resolve_dispatch_runtime() == "codex"
    assert resolve_codex_model() == "gpt-config-codex"
    assert payload["runtime_source"] == "bemine.toml"
    assert payload["model"] == "gpt-config-codex"
    assert payload["model_source"] == "bemine.toml"
