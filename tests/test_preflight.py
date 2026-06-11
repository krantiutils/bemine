from __future__ import annotations

from pathlib import Path

from bemine.config import BemineSettings
from bemine.preflight import (
    preflight_issues,
    render_runtime_preflight,
    runtime_preflight,
)


def _settings(tmp_path):
    return BemineSettings.load(
        state_dir=tmp_path / ".bemine",
        vault_dir=tmp_path / "v",
        claude_home=tmp_path / "claude_home",
        codex_home=tmp_path / "codex_home",
    )


def _install_all(monkeypatch, tmp_path: Path) -> None:
    bins = tmp_path / "bins"
    bins.mkdir(exist_ok=True)
    for name in ("claude", "codex", "kimi"):
        (bins / name).write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(
        "bemine.preflight._resolve_runtime_bin", lambda runtime: str(bins / runtime)
    )


def _kimi_cli_home(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "kimi_cli"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr("bemine.preflight._kimi_cli_home", lambda: home)
    return home


def test_preflight_flags_unauthenticated_runtimes(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    _install_all(monkeypatch, tmp_path)
    kimi_home = _kimi_cli_home(monkeypatch, tmp_path)
    # claude logged in, codex not, kimi LLM not set
    settings.claude_home.mkdir(parents=True, exist_ok=True)
    (settings.claude_home / ".credentials.json").write_text("{}", encoding="utf-8")
    (kimi_home / "config.toml").write_text(
        'default_model = ""\n[models]\n[providers]\n', encoding="utf-8"
    )

    rows = {row["runtime"]: row for row in runtime_preflight(settings)}

    assert rows["claude"]["ok"] is True
    assert rows["codex"]["ok"] is False
    assert "not logged in" in rows["codex"]["detail"]
    assert rows["kimi"]["ok"] is False
    assert "LLM not set" in rows["kimi"]["detail"]


def test_preflight_kimi_ok_when_llm_configured(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    _install_all(monkeypatch, tmp_path)
    kimi_home = _kimi_cli_home(monkeypatch, tmp_path)
    (kimi_home / "config.toml").write_text(
        'default_model = "kimi-k2.6"\n', encoding="utf-8"
    )

    rows = {row["runtime"]: row for row in runtime_preflight(settings)}

    assert rows["kimi"]["ok"] is True


def test_preflight_issues_always_flag_goal_default(monkeypatch, tmp_path):
    rows = [
        {
            "runtime": "kimi",
            "installed": True,
            "authenticated": False,
            "ok": False,
            "goal_default": True,
            "detail": "LLM not set — run kimi and /login",
            "bin": "/x/kimi",
        },
        {
            "runtime": "codex",
            "installed": False,
            "authenticated": False,
            "ok": False,
            "goal_default": False,
            "detail": "binary not found",
            "bin": "",
        },
        {
            "runtime": "claude",
            "installed": True,
            "authenticated": True,
            "ok": True,
            "goal_default": False,
            "detail": "credentials present",
            "bin": "/x/claude",
        },
    ]

    issues = preflight_issues(rows)

    assert any("goal default runtime kimi is unusable" in i for i in issues)
    # a non-default runtime that simply isn't installed is not an issue
    assert not any("codex" in i for i in issues)
    rendered = render_runtime_preflight(rows)
    assert "kimi (goal default): UNUSABLE" in rendered


def test_health_includes_runtime_issues(monkeypatch, tmp_path):
    from bemine.db import BemineDB
    from bemine.deploy.heartbeat import write_heartbeat
    from bemine.deploy.state import DeployState, state_path
    from bemine.health import collect_health, render_health
    from bemine.jobs.systemd import UnitStatus

    settings = _settings(tmp_path)
    DeployState(phase="confirmed", target_sha="sha1").save(state_path(settings))
    write_heartbeat(settings, "daemon", "sha1")
    write_heartbeat(settings, "gateway", "sha1")
    bad = [
        {
            "runtime": "kimi",
            "installed": True,
            "authenticated": False,
            "ok": False,
            "goal_default": True,
            "detail": "LLM not set — run kimi and /login",
            "bin": "/x/kimi",
        },
    ]

    payload = collect_health(
        settings,
        db=BemineDB(settings.db_path),
        unit_statuses=[
            UnitStatus(
                "bemine-daemon.service", "loaded", "active", "running", "enabled", 1
            ),
            UnitStatus(
                "mayor-discord-gateway.service",
                "loaded",
                "active",
                "running",
                "enabled",
                2,
            ),
        ],
        runtime_rows=bad,
        now=123.0,
    )

    assert payload["ok"] is False
    assert any("goal default runtime kimi is unusable" in i for i in payload["issues"])
    rendered = render_health(payload)
    assert "Runtimes: kimi*=UNUSABLE" in rendered
