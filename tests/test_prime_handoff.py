from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _fake_codex_session(root: Path, cwd: Path) -> None:
    _write_jsonl(
        root / ".codex" / "sessions" / "2026" / "06" / "04" / "rollout-2026-06-04T10-00-00-prime.jsonl",
        [
            {
                "timestamp": "2026-06-04T10:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "prime-session",
                    "timestamp": "2026-06-04T10:00:00Z",
                    "cwd": str(cwd),
                    "model": "gpt-5.5",
                },
            },
            {
                "timestamp": "2026-06-04T10:01:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "prime context is ready"}],
                },
            },
        ],
    )


def test_cli_prime_scans_and_renders_worker_startup_packet(tmp_path: Path) -> None:
    project = tmp_path / "repo" / "alpha"
    project.mkdir(parents=True)
    _fake_codex_session(tmp_path, project)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "bemine.cli",
            "--state-dir",
            str(tmp_path / "state"),
            "--vault",
            str(tmp_path / "vault"),
            "--codex-home",
            str(tmp_path / ".codex"),
            "--claude-home",
            str(tmp_path / ".claude"),
            "--kimi-home",
            str(tmp_path / ".kimi"),
            "--device",
            "testbox",
            "prime",
            "--cwd",
            str(project),
            "--json",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )

    payload = json.loads(result.stdout)
    assert payload["device_id"] == "testbox"
    assert payload["project_name"] == "alpha"
    assert payload["counts"]["sessions"] == 1
    assert payload["projects"][0]["name"] == "alpha"
    assert payload["statuses"][0]["session_id"] == "codex:prime-session"
    assert any(path.endswith("SOUL.md") for path in payload["harness_files"])
    assert any(path.endswith("SKILL.md") for path in payload["harness_files"])


def test_cli_handoff_writes_vault_files_and_audit_event(tmp_path: Path) -> None:
    project = tmp_path / "repo" / "bemine"
    project.mkdir(parents=True)
    state = tmp_path / "state"
    vault = tmp_path / "vault"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "bemine.cli",
            "--state-dir",
            str(state),
            "--vault",
            str(vault),
            "--device",
            "testbox",
            "handoff",
            "implemented prime and handoff",
            "--cwd",
            str(project),
            "--runtime",
            "codex",
            "--session-id",
            "codex:test",
            "--next",
            "wire Discord slash command",
            "--blocker",
            "no managed live session adapter yet",
            "--test",
            "uv run pytest -q",
            "--json",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )

    payload = json.loads(result.stdout)
    handoff_path = Path(payload["path"])
    assert handoff_path.exists()
    handoff = handoff_path.read_text(encoding="utf-8")
    assert "implemented prime and handoff" in handoff
    assert "wire Discord slash command" in handoff
    assert "no managed live session adapter yet" in handoff
    assert "`uv run pytest -q`" in handoff

    daily_files = list((vault / "Daily").glob("*.md"))
    assert len(daily_files) == 1
    assert "Handoff: bemine" in daily_files[0].read_text(encoding="utf-8")

    conn = sqlite3.connect(state / "bemine.db")
    try:
        row = conn.execute(
            "SELECT category, event_type, actor, session_id, payload_json FROM audit_events"
        ).fetchone()
    finally:
        conn.close()
    assert row[:4] == ("ritual", "handoff_recorded", "cli", "codex:test")
    audit_payload = json.loads(row[4])
    assert audit_payload["runtime"] == "codex"
    assert audit_payload["tests"] == ["uv run pytest -q"]
