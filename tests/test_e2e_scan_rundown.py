from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _fake_codex(home: Path) -> None:
    _write_jsonl(
        home / ".codex" / "sessions" / "2026" / "06" / "04" / "rollout-2026-06-04T10-00-00-abc.jsonl",
        [
            {
                "timestamp": "2026-06-04T10:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "codex-session-1",
                    "timestamp": "2026-06-04T10:00:00Z",
                    "cwd": str(home / "repo" / "alpha"),
                    "model": "gpt-5.5",
                },
            },
            {
                "timestamp": "2026-06-04T10:01:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "build the alpha importer"}],
                },
            },
            {
                "timestamp": "2026-06-04T10:02:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "implemented importer tests"}],
                },
            },
        ],
    )


def _fake_claude(home: Path) -> None:
    _write_jsonl(
        home / ".claude" / "projects" / "-tmp-alpha" / "claude-session-1.jsonl",
        [
            {
                "type": "user",
                "sessionId": "claude-session-1",
                "timestamp": "2026-06-04T11:00:00Z",
                "cwd": str(home / "repo" / "alpha"),
                "gitBranch": "main",
                "message": {"role": "user", "content": "investigate alpha exporter"},
            },
            {
                "type": "assistant",
                "sessionId": "claude-session-1",
                "timestamp": "2026-06-04T11:03:00Z",
                "cwd": str(home / "repo" / "alpha"),
                "gitBranch": "main",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus",
                    "content": [{"type": "text", "text": "exporter blocked on schema choice"}],
                },
            },
        ],
    )


def test_scan_indexes_sessions_updates_vault_and_supports_fts(tmp_path: Path) -> None:
    _fake_codex(tmp_path)
    _fake_claude(tmp_path)
    state = tmp_path / "state"
    vault = tmp_path / "vault"

    scan = subprocess.run(
        [
            sys.executable,
            "-m",
            "bemine.cli",
            "--state-dir",
            str(state),
            "--vault",
            str(vault),
            "--codex-home",
            str(tmp_path / ".codex"),
            "--claude-home",
            str(tmp_path / ".claude"),
            "--kimi-home",
            str(tmp_path / ".kimi"),
            "--device",
            "testbox",
            "scan",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    result = json.loads(scan.stdout)
    assert result == {"devices": 1, "projects": 1, "sessions": 2}

    rundown = subprocess.run(
        [
            sys.executable,
            "-m",
            "bemine.cli",
            "--state-dir",
            str(state),
            "--vault",
            str(vault),
            "rundown",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout
    assert "# Project Rundown" in rundown
    assert "alpha" in rundown
    assert "codex" in rundown
    assert "claude" in rundown

    project_page = vault / "Projects" / "alpha.md"
    assert project_page.exists()
    page_text = project_page.read_text(encoding="utf-8")
    assert "Bemine Status" in page_text
    assert "exporter blocked on schema choice" in page_text

    search = subprocess.run(
        [
            sys.executable,
            "-m",
            "bemine.cli",
            "--state-dir",
            str(state),
            "--vault",
            str(vault),
            "search",
            "exporter",
            "--json",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout
    rows = json.loads(search)
    assert len(rows) == 1
    assert rows[0]["runtime"] == "claude"

    conn = sqlite3.connect(state / "bemine.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
    finally:
        conn.close()

