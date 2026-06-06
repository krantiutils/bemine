from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from bemine.index.status import SessionStatus, collect_local_status, render_status
from bemine.index.status import _dedupe_statuses
from bemine.index.status import _infer_ask


def test_status_orders_by_last_interaction_and_marks_agent_asks(tmp_path: Path) -> None:
    codex_file = tmp_path / "codex" / "sessions" / "2026" / "06" / "04" / (
        "rollout-2026-06-04T10-19-22-019e90e9-30d7-7720-91df-07fe8f5c6d3d.jsonl"
    )
    claude_file = tmp_path / "claude" / "projects" / "-repo-beta" / "claude-1.jsonl"
    codex_file.parent.mkdir(parents=True)
    claude_file.parent.mkdir(parents=True)
    _write_jsonl(
        codex_file,
        [
            {
                "type": "session_meta",
                "payload": {
                    "id": "019e90e9-30d7-7720-91df-07fe8f5c6d3d",
                    "cwd": "/repo/aws",
                    "model": "openai",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"text": "Can you approve AWS SSO with code 123456?"}],
                },
            },
        ],
    )
    _write_jsonl(
        claude_file,
        [
            {
                "sessionId": "claude-1",
                "cwd": "/repo/beta",
                "gitBranch": "main",
                "type": "assistant",
                "message": {"content": [{"text": "Tests passed and no input is needed."}]},
            }
        ],
    )
    now = time.time()
    os.utime(claude_file, (now - 120, now - 120))
    os.utime(codex_file, (now - 30, now - 30))

    statuses = collect_local_status(
        device_id="testbox",
        codex_home=tmp_path / "codex",
        claude_home=tmp_path / "claude",
        kimi_home=tmp_path / "kimi",
        limit_per_runtime=4,
    )

    assert [status.runtime for status in statuses[:2]] == ["codex", "claude"]
    assert statuses[0].cwd == "/repo/aws"
    assert statuses[0].asks_user is True
    assert "<code>" in statuses[0].ask_summary
    assert "123456" not in statuses[0].ask_summary
    assert statuses[1].asks_user is False

    rendered = render_status(statuses, limit=2)
    assert "ask: yes" in rendered
    assert "ask: no" in rendered
    assert "123456" not in rendered


def test_cli_status_local_only_outputs_recent_interaction_json(tmp_path: Path) -> None:
    codex_file = tmp_path / "codex" / "sessions" / "2026" / "06" / "04" / (
        "rollout-2026-06-04T10-19-22-019e90e9-30d7-7720-91df-07fe8f5c6d3d.jsonl"
    )
    codex_file.parent.mkdir(parents=True)
    _write_jsonl(
        codex_file,
        [
            {
                "type": "session_meta",
                "payload": {
                    "id": "019e90e9-30d7-7720-91df-07fe8f5c6d3d",
                    "cwd": "/repo/bemine",
                    "model": "openai",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"text": "please continue"}],
                },
            },
        ],
    )

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
            str(tmp_path / "codex"),
            "--claude-home",
            str(tmp_path / "claude"),
            "--kimi-home",
            str(tmp_path / "kimi"),
            "--device",
            "testbox",
            "status",
            "--local-only",
            "--json",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )

    payload = json.loads(result.stdout)
    assert payload["errors"] == []
    assert payload["statuses"][0]["device_id"] == "testbox"
    assert payload["statuses"][0]["cwd"] == "/repo/bemine"
    assert payload["statuses"][0]["asks_user"] is False
    assert "latest interaction is user input" in payload["statuses"][0]["ask_summary"]


def test_status_filters_ignored_project_roots(tmp_path: Path, monkeypatch) -> None:
    ignored_root = tmp_path / "legacy"
    kept_root = tmp_path / "github" / "active"
    monkeypatch.setenv("BEMINE_IGNORED_PROJECT_ROOTS", str(ignored_root))
    ignored_file = tmp_path / "codex" / "sessions" / "2026" / "06" / "04" / (
        "rollout-2026-06-04T10-19-22-ignored.jsonl"
    )
    kept_file = tmp_path / "claude" / "projects" / "-active" / "claude-1.jsonl"
    ignored_file.parent.mkdir(parents=True)
    kept_file.parent.mkdir(parents=True)
    _write_jsonl(
        ignored_file,
        [
            {
                "type": "session_meta",
                "payload": {
                    "id": "ignored-session",
                    "cwd": str(ignored_root / "project"),
                    "model": "openai",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"text": "ignored session"}],
                },
            },
        ],
    )
    _write_jsonl(
        kept_file,
        [
            {
                "sessionId": "kept-session",
                "cwd": str(kept_root),
                "gitBranch": "main",
                "type": "assistant",
                "message": {"content": [{"text": "kept session"}]},
            }
        ],
    )

    statuses = collect_local_status(
        device_id="testbox",
        codex_home=tmp_path / "codex",
        claude_home=tmp_path / "claude",
        kimi_home=tmp_path / "kimi",
        limit_per_runtime=4,
    )

    assert [status.cwd for status in statuses] == [str(kept_root)]


def test_status_ask_detection_ignores_explanatory_agent_text() -> None:
    asks, summary = _infer_ask(
        last_role="assistant",
        text=(
            "Here's how it actually works, based on the code. "
            "What the service does internally is allocate fixed ports and run tests."
        ),
    )
    assert asks is False
    assert summary == "latest agent turn does not appear to request input"

    asks, summary = _infer_ask(
        last_role="assistant",
        text="I'm running tests and then one full fleet status run again to confirm output.",
    )
    assert asks is False
    assert summary == "latest agent turn does not appear to request input"

    asks, summary = _infer_ask(
        last_role="assistant",
        text="## Can you run multiple tests at once?\nYes, the runner supports parallel jobs.",
    )
    assert asks is False
    assert summary == "latest agent turn does not appear to request input"

    asks, summary = _infer_ask(
        last_role="assistant",
        text="The deploy is blocked until you approve the AWS SSO request with code 123456.",
    )
    assert asks is True
    assert "<code>" in summary
    assert "123456" not in summary


def test_status_dedupes_same_device_runtime_session_id() -> None:
    newest = SessionStatus(
        device_id="mbp2",
        runtime="claude",
        session_id="session-1",
        cwd="/repo",
        source_path="/a.jsonl",
        last_interaction_at=20,
        age_seconds=1,
        last_role="assistant",
        asks_user=False,
        ask_summary="latest agent turn does not appear to request input",
        latest_summary="newer",
    )
    duplicate = SessionStatus(
        device_id="mbp2",
        runtime="claude",
        session_id="session-1",
        cwd="/repo",
        source_path="/b.jsonl",
        last_interaction_at=10,
        age_seconds=2,
        last_role="assistant",
        asks_user=False,
        ask_summary="latest agent turn does not appear to request input",
        latest_summary="older",
    )
    other_runtime = SessionStatus(
        device_id="mbp2",
        runtime="codex",
        session_id="session-1",
        cwd="/repo",
        source_path="/c.jsonl",
        last_interaction_at=9,
        age_seconds=3,
        last_role="assistant",
        asks_user=False,
        ask_summary="latest agent turn does not appear to request input",
        latest_summary="codex",
    )

    deduped = _dedupe_statuses([newest, duplicate, other_runtime])

    assert deduped == [newest, other_runtime]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
