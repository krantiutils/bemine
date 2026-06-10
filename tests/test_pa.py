from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from bemine.backlog import add_backlog_item
from bemine.cli import main
from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.index.status import SessionStatus
from bemine.pa import build_pa_plan


def _settings(tmp_path: Path) -> tuple[BemineSettings, BemineDB]:
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="testbox",
    )
    return settings, BemineDB(settings.db_path)


def _status(**overrides) -> SessionStatus:
    data = {
        "device_id": "mbp",
        "runtime": "codex",
        "session_id": "s1",
        "cwd": "/Users/cdjk/github/llm/rai-speech-toolkit",
        "source_path": "",
        "last_interaction_at": 100.0,
        "age_seconds": 5,
        "last_role": "assistant",
        "asks_user": True,
        "ask_summary": "Need reviewer feedback?",
        "latest_summary": "",
    }
    data.update(overrides)
    return SessionStatus(**data)


def test_build_pa_plan_prioritizes_health_jobs_sessions_watchers_and_backlog(tmp_path: Path) -> None:
    settings, db = _settings(tmp_path)
    add_backlog_item(settings, owner="PA", text="review stale asks before noon", db=db)
    health = {
        "issues": ["gateway heartbeat is missing"],
        "jobs": {
            "failed_jobs": [
                {"job_id": "daily_digest", "failure_count": 2, "last_run_at": "now"}
            ]
        },
        "watchers": {
            "items": [
                {"project_id": "rai", "awaiting_reply": True},
            ]
        },
    }
    audits = [
        {"id": 9, "category": "recovery", "event_type": "llm_escalate", "project_id": "rai"}
    ]
    generated_at = datetime(2026, 6, 11, tzinfo=timezone.utc)

    result = build_pa_plan(
        settings,
        db=db,
        statuses=[_status()],
        health=health,
        audits=audits,
        limit=6,
        write=True,
        generated_at=generated_at,
    )

    texts = [item.text for item in result.items]
    assert texts[0].startswith("Fix Bemine health issue")
    assert any("daily_digest" in text for text in texts)
    assert any("Need reviewer feedback" in text for text in texts)
    assert any("Watcher for `rai` is awaiting" in text for text in texts)
    assert any("PA backlog" in text for text in texts)
    assert result.path == settings.vault_dir / "PA" / "Plans" / "2026-06-11-pa-plan.md"
    assert result.path.exists()
    assert "gateway heartbeat is missing" in result.path.read_text(encoding="utf-8")
    rows = db.list_audit_events(category="pa", limit=1)
    assert rows[0]["event_type"] == "pa_plan_recorded"


def test_cli_pa_plan_json(tmp_path: Path, capsys, monkeypatch) -> None:
    settings, db = _settings(tmp_path)
    add_backlog_item(settings, owner="PA", text="check active goal workers", db=db)
    monkeypatch.setattr("bemine.pa.collect_fleet_status", lambda *a, **k: ([], []))
    monkeypatch.setattr(
        "bemine.pa.collect_health",
        lambda *a, **k: {
            "issues": [],
            "jobs": {"failed_jobs": []},
            "watchers": {"items": []},
        },
    )

    main([
        "--state-dir",
        str(settings.state_dir),
        "--vault",
        str(settings.vault_dir),
        "pa",
        "plan",
        "--limit",
        "1",
        "--json",
    ])

    payload = json.loads(capsys.readouterr().out)
    assert len(payload["items"]) == 1
    assert "check active goal workers" in payload["items"][0]["text"]
    assert payload["evidence"]["pa_backlog_items"] == 1
