from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.discord.store import Store
from bemine.dreams import build_dream_datapoint, run_dream
from bemine.jobs import run_job_by_id, seed_default_jobs


def test_build_dream_datapoint_extracts_signals_and_friction() -> None:
    data = build_dream_datapoint(
        inbound=[
            {"content": "what model are we using right now"},
            {"content": "goal rai-speech-toolkit: train this"},
            {"content": "SMM idea: post the benchmark"},
        ],
        outbound=[
            {"content": "Dispatch failed: Reached maximum number of turns (3)"},
        ],
        audits=[
            {"category": "recovery", "event_type": "llm_send_keys"},
            {"category": "backlog", "event_type": "backlog_item_added"},
        ],
        generated_at=datetime(2026, 6, 10, tzinfo=timezone.utc),
    )

    assert data["schema"] == "bemine.dream.v1"
    assert any("Runtime/model identity" in item for item in data["signals"])
    assert any("long-running project work" in item for item in data["signals"])
    assert any("Social/post ideas" in item for item in data["signals"])
    assert any("turn caps" in item for item in data["friction"])
    assert data["audit_counts"]["recovery/llm_send_keys"] == 1


def test_run_dream_appends_jsonl_and_audits(tmp_path: Path) -> None:
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="testbox",
    )
    Store(settings.db_path).record_inbound_message(
        message_id="m1",
        channel_id="c1",
        guild_id=None,
        author_id="u1",
        author_name="voidash",
        content="status of this project",
        is_dm=True,
        created_at="2026-06-10T00:00:00+00:00",
    )

    result = run_dream(settings, limit=10)

    assert result.path == tmp_path / "vault" / "Dreams" / "dreams.jsonl"
    lines = result.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    saved = json.loads(lines[0])
    assert saved["schema"] == "bemine.dream.v1"
    assert saved["window"]["inbound_messages"] == 1
    rows = BemineDB(settings.db_path).list_audit_events(category="dream", limit=1)
    assert rows[0]["event_type"] == "dream_recorded"


def test_dream_job_is_seeded_and_runs(tmp_path: Path) -> None:
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="testbox",
    )
    db = BemineDB(settings.db_path)
    seed_default_jobs(db, settings=settings)

    assert db.get_job("dream") is not None
    result = run_job_by_id(settings, "dream", sender=lambda text: {"id": "", "channel_id": ""})

    assert result.status == "success"
    assert "Dream recorded:" in result.output
    assert (tmp_path / "vault" / "Dreams" / "dreams.jsonl").exists()
