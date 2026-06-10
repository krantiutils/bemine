from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from bemine.context.harness_context import load_harness_context
from bemine.context.operator_context import load_operator_context, redact_operator_context
from bemine.context.subagents import load_subagent_context


def test_operator_context_loads_deploy_md_and_redacts_secrets(tmp_path: Path) -> None:
    deploy = tmp_path / "deploy.md"
    deploy.write_text(
        "\n".join(
            [
                "# Deploy",
                "HF_TOKEN=hf_abcdefghijklmnopqrstuvwxyz",
                "PORKBUN_API_KEY=pk_test_secret_value_123456789",
                "PORKBUN_SECRET_API_KEY: secret-value",
                "ssh -i ~/.ssh/monitor.pem ubuntu@178.104.21.224",
            ]
        ),
        encoding="utf-8",
    )

    context = load_operator_context(files=[deploy])

    assert str(deploy) in context.text
    assert "hf_abcdefghijklmnopqrstuvwxyz" not in context.text
    assert "pk_test_secret_value_123456789" not in context.text
    assert "secret-value" not in context.text
    assert "ubuntu@178.104.21.224" in context.text


def test_operator_context_redacts_private_keys() -> None:
    text = redact_operator_context(
        "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"
    )

    assert text == "<private-key-redacted>"


def test_cli_context_show_uses_default_deploy_file(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "deploy.md").write_text("TOKEN=hf_abcdefghijklmnopqrstuvwxyz\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "bemine.cli",
            "--state-dir",
            str(tmp_path / "state"),
            "--vault",
            str(tmp_path / "vault"),
            "context",
            "show",
            "--json",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )

    payload = json.loads(result.stdout)
    assert payload["files"] == [str(home / "deploy.md")]
    assert "hf_abcdefghijklmnopqrstuvwxyz" not in payload["text"]
    assert "<redacted>" in payload["text"]


def test_harness_context_loads_soul_and_skill(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "SOUL.md").write_text("TOKEN=hf_abcdefghijklmnopqrstuvwxyz\nsoul", encoding="utf-8")
    (repo / "SKILL.md").write_text("---\nname: bemine-harness\n---\nskill", encoding="utf-8")

    context = load_harness_context(repo_dir=repo)

    assert context.files == [repo / "SOUL.md", repo / "SKILL.md"]
    assert "soul" in context.text
    assert "skill" in context.text
    assert "hf_abcdefghijklmnopqrstuvwxyz" not in context.text
    assert "<redacted>" in context.text


def test_subagent_context_loads_configured_profiles_and_redacts(
    tmp_path: Path, monkeypatch
) -> None:
    config = tmp_path / "bemine.toml"
    profile_dir = tmp_path / "subagents"
    profile_dir.mkdir()
    (profile_dir / "PA.md").write_text(
        "# PA\nTOKEN=hf_abcdefghijklmnopqrstuvwxyz\nplan the day",
        encoding="utf-8",
    )
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

    context = load_subagent_context()

    assert [profile.name for profile in context.profiles] == ["PA", "SMM"]
    assert str(profile_dir / "PA.md") in [str(p.path) for p in context.profiles]
    assert "plan the day" in context.text
    assert "post ideas" in context.text
    assert "hf_abcdefghijklmnopqrstuvwxyz" not in context.text
    assert "<redacted>" in context.text


def test_cli_subagents_list_and_show(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "bemine.toml"
    profile_dir = tmp_path / "subagents"
    profile_dir.mkdir()
    (profile_dir / "SMM.md").write_text("# SMM\npost ideas", encoding="utf-8")
    config.write_text(
        """
[subagents]
files = ["subagents/SMM.md"]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("BEMINE_CONFIG", str(config))

    listed = subprocess.run(
        [
            sys.executable,
            "-m",
            "bemine.cli",
            "--state-dir",
            str(tmp_path / "state"),
            "--vault",
            str(tmp_path / "vault"),
            "subagents",
            "list",
            "--json",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    payload = json.loads(listed.stdout)
    assert payload["profiles"][0]["name"] == "SMM"

    shown = subprocess.run(
        [
            sys.executable,
            "-m",
            "bemine.cli",
            "--state-dir",
            str(tmp_path / "state"),
            "--vault",
            str(tmp_path / "vault"),
            "subagents",
            "show",
            "SMM",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    assert "post ideas" in shown.stdout
