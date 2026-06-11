from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from bemine.briefs import (
    brief_status,
    build_brief_items,
    render_brief_status,
    run_brief,
)
from bemine.cli import main
from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.index.status import SessionStatus
from bemine.jobs import run_job_by_id, seed_default_jobs


def _status(**overrides) -> SessionStatus:
    data = {
        "device_id": "mbp",
        "runtime": "claude",
        "session_id": "s1",
        "cwd": "/Users/cdjk/github/llm/rai-speech-toolkit",
        "source_path": "",
        "last_interaction_at": 100.0,
        "age_seconds": 5,
        "last_role": "assistant",
        "asks_user": False,
        "ask_summary": "",
        "latest_summary": "",
    }
    data.update(overrides)
    return SessionStatus(**data)


def test_build_brief_items_finds_pa_smm_and_android_actions() -> None:
    items = build_brief_items(
        statuses=[
            _status(
                asks_user=True,
                ask_summary="Need AWS SSO approval?",
            ),
            _status(
                session_id="s2",
                latest_summary="Final report: Rai checkpoint chrF 24.78, benchmark passed",
            ),
        ],
        inbound=[{"message_id": "m1", "content": "what should I focus on today?"}],
        outbound=[
            {"id": 1, "content": "Dispatch failed: maximum turns"},
            {
                "id": 2,
                "content": "rai-speech-toolkit shipped the goal command and eval passed.",
            },
        ],
        audits=[{"id": 9, "category": "recovery", "event_type": "llm_escalate"}],
        tools={
            "adb": {"devices": [{"serial": "RZCTC0M17SJ", "state": "unauthorized"}]}
        },
    )

    pa = [item.text for item in items if item.owner == "PA"]
    smm = [item.text for item in items if item.owner == "SMM"]
    assert any("Need AWS SSO" in item for item in pa)
    assert any("Authorize Android USB debugging" in item for item in pa)
    assert any("today's focus" in item for item in pa)
    assert any("Rai checkpoint" in item for item in smm)
    assert any("goal command" in item for item in smm)


def test_run_brief_writes_vault_backlog_and_audit(tmp_path: Path) -> None:
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="testbox",
    )
    db = BemineDB(settings.db_path)
    generated_at = datetime(2026, 6, 11, tzinfo=timezone.utc)
    existing = settings.vault_dir / "Backlog" / "PA.md"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text(
        "\n".join(
            [
                "# PA Backlog",
                "",
                "- [ ] 2026-06-10T00:00:00+00:00 — Authorize Android USB debugging for device RZCTC0M17SJ, then install Phantom Service APK.",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = run_brief(
        settings,
        db=db,
        statuses=[
            _status(
                asks_user=True,
                ask_summary="Can you secure reviewer feedback?",
            ),
        ],
        inbound=[],
        outbound=[
            {"id": 1, "content": "rai-speech-toolkit shipped Rai benchmark chart."}
        ],
        audits=[],
        tools={
            "adb": {"devices": [{"serial": "RZCTC0M17SJ", "state": "unauthorized"}]}
        },
        generated_at=generated_at,
    )

    assert result.path == tmp_path / "vault" / "Briefs" / "2026-06-11-pa-smm.md"
    text = result.path.read_text(encoding="utf-8")
    assert "# PA/SMM Brief" in text
    assert "Can you secure reviewer feedback" in text
    assert "rai-speech-toolkit shipped Rai benchmark chart" in text
    assert len(result.backlog_entries) == 2
    assert all(
        "Android USB debugging" not in entry.text for entry in result.backlog_entries
    )
    assert (tmp_path / "vault" / "Backlog" / "PA.md").exists()
    assert (tmp_path / "vault" / "Backlog" / "SMM.md").exists()
    rows = db.list_audit_events(category="brief", limit=1)
    assert rows[0]["event_type"] == "pa_smm_brief_recorded"
    status = brief_status(settings)
    assert status["count"] == 1
    assert status["latest"]["path"] == str(result.path)
    assert status["latest"]["owner_counts"]["PA"] >= 1
    assert status["latest"]["owner_counts"]["SMM"] >= 1
    assert "PA/SMM brief status:" in render_brief_status(status)

    duplicate = run_brief(
        settings,
        db=db,
        statuses=[
            _status(
                asks_user=True,
                ask_summary="Can you secure reviewer feedback?",
            ),
        ],
        inbound=[],
        outbound=[
            {"id": 1, "content": "rai-speech-toolkit shipped Rai benchmark chart."}
        ],
        audits=[],
        tools={
            "adb": {"devices": [{"serial": "RZCTC0M17SJ", "state": "unauthorized"}]}
        },
        generated_at=generated_at,
    )
    assert duplicate.backlog_entries == []


def test_cli_brief_run_json_without_backlog(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr("bemine.briefs.collect_fleet_status", lambda *a, **k: ([], []))
    monkeypatch.setattr("bemine.briefs.detect_tool_capabilities", lambda: {})

    main(
        [
            "--state-dir",
            str(tmp_path / "state"),
            "--vault",
            str(tmp_path / "vault"),
            "brief",
            "run",
            "--no-backlog",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert Path(payload["path"]).exists()
    assert payload["backlog_entries"] == []


def test_pa_smm_brief_job_is_seeded_and_runs(tmp_path: Path, monkeypatch) -> None:
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="testbox",
    )
    db = BemineDB(settings.db_path)
    seed_default_jobs(db, settings=settings)
    monkeypatch.setattr("bemine.briefs.collect_fleet_status", lambda *a, **k: ([], []))
    monkeypatch.setattr("bemine.briefs.detect_tool_capabilities", lambda: {})

    assert db.get_job("pa_smm_brief") is not None
    result = run_job_by_id(
        settings,
        "pa_smm_brief",
        sender=lambda text: {"id": "", "channel_id": ""},
    )

    assert result.status == "success"
    assert "PA/SMM brief recorded:" in result.output
    assert (tmp_path / "vault" / "Briefs").exists()
