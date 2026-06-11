from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bemine.config import BemineSettings
from bemine.cli import main
from bemine.db import BemineDB
from bemine.goals import list_goal_sessions, render_goal_sessions, start_goal
from bemine.index.models import SessionRecord
from bemine.index.status import SessionStatus
from bemine.workers.workers import (
    WorkerController,
    link_worker_runtime_sessions,
    resume_missing_local_workers,
)


def test_start_goal_starts_worker_sends_prompt_and_registers_watcher(
    tmp_path: Path,
) -> None:
    project = tmp_path / "rai-speech-toolkit"
    project.mkdir()
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="testbox",
    )
    db = BemineDB(settings.db_path)
    tmux = FakeTmux()
    controller = WorkerController(settings, db=db, tmux=tmux)  # type: ignore[arg-type]

    result = start_goal(
        settings,
        project="rai-speech-toolkit",
        brief="train the first Rai TTS checkpoint from the 14 rulebooks",
        cwd=project,
        runtime="shell",
        worker_name="goal-rai",
        interval_secs=300,
        channel_id="channel-rai",
        command="cat",
        db=db,
        controller=controller,
    )

    assert result.worker.name == "goal-rai"
    assert result.worker.runtime == "shell"
    assert result.worker.cwd == str(project)
    assert "goal-rai" in tmux.sessions
    sent = tmux.sent["goal-rai"][-1]
    assert "# Bemine Goal Session" in sent
    assert "train the first Rai TTS checkpoint" in sent
    assert "Do not hand commands back to the user" not in sent
    assert "do not hand commands back to the user" in sent

    watcher = db.get_project_watcher(result.worker.project_id)
    assert watcher is not None
    assert watcher["status"] == "active"
    assert watcher["channel_id"] == "channel-rai"
    assert watcher["interval_secs"] == 300
    assert (
        watcher["brief"] == "train the first Rai TTS checkpoint from the 14 rulebooks"
    )

    with sqlite3.connect(settings.db_path) as conn:
        row = conn.execute(
            "SELECT payload_json FROM audit_events WHERE event_type = 'goal_started'"
        ).fetchone()
    payload = json.loads(row[0])
    assert payload["worker"]["name"] == "goal-rai"
    assert payload["watcher"]["channel_id"] == "channel-rai"


def test_resume_missing_local_workers_restarts_running_cdjk_worker(
    tmp_path: Path,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="cdjk",
    )
    db = BemineDB(settings.db_path)
    db.upsert_managed_session(
        name="goal-restart",
        runtime="shell",
        transport="tmux",
        device_id="cdjk",
        project_id="proj_restart",
        cwd=str(project),
        command="cat",
        status="running",
    )
    tmux = FakeTmux()

    resumed = resume_missing_local_workers(
        settings,
        db=db,
        tmux=tmux,  # type: ignore[arg-type]
        nudge_sleep=lambda _: None,
    )

    assert [record.name for record in resumed] == ["goal-restart"]
    assert "goal-restart" in tmux.sessions
    assert "cd " in tmux.started_commands["goal-restart"]
    assert "cat" in tmux.started_commands["goal-restart"]
    with sqlite3.connect(settings.db_path) as conn:
        events = [
            row[0]
            for row in conn.execute(
                "SELECT event_type FROM audit_events ORDER BY id"
            ).fetchall()
        ]
    assert "worker_resumed_after_outage" in events


def test_resume_missing_local_workers_restarts_missing_cdjk_worker(
    tmp_path: Path,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="cdjk",
    )
    db = BemineDB(settings.db_path)
    db.upsert_managed_session(
        name="goal-restart",
        runtime="shell",
        transport="tmux",
        device_id="cdjk",
        project_id="proj_restart",
        cwd=str(project),
        command="cat",
        status="missing",
    )
    tmux = FakeTmux()

    resumed = resume_missing_local_workers(
        settings,
        db=db,
        tmux=tmux,  # type: ignore[arg-type]
        nudge_sleep=lambda _: None,
    )

    assert [record.name for record in resumed] == ["goal-restart"]
    assert "goal-restart" in tmux.sessions
    assert resumed[0].status == "running"


def _index_session(
    db: BemineDB,
    *,
    session_id: str,
    runtime: str,
    cwd: str,
    updated_at: str,
    device_id: str = "cdjk",
    write_transcript: bool = True,
) -> None:
    transcript = Path(cwd) / f"{session_id.split(':', 1)[-1]}.jsonl"
    if write_transcript:
        transcript.write_text("{}\n", encoding="utf-8")
    db.upsert_device(device_id)
    db.upsert_session(
        SessionRecord(
            session_id=session_id,
            runtime=runtime,
            device_id=device_id,
            cwd=cwd,
            source_path=transcript,
            started_at=updated_at,
            updated_at=updated_at,
            model="",
            title="t",
            summary="s",
            message_count=1,
            tool_count=0,
        )
    )


def _managed_worker(
    db: BemineDB, *, cwd: str, runtime: str, command: str, name: str = "goal-resume"
) -> None:
    db.upsert_managed_session(
        name=name,
        runtime=runtime,
        transport="tmux",
        device_id="cdjk",
        project_id="proj_resume",
        cwd=cwd,
        command=command,
        status="running",
    )


def test_resume_missing_local_workers_resumes_claude_conversation(
    tmp_path: Path,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="cdjk",
    )
    db = BemineDB(settings.db_path)
    cwd = str(project.resolve())
    _managed_worker(
        db, cwd=cwd, runtime="claude", command="claude --dangerously-skip-permissions"
    )
    _index_session(
        db,
        session_id="claude:0aa6aa5e-3a64-4a32-9a2b-9f4b1f2a3c4d",
        runtime="claude",
        cwd=cwd,
        updated_at="2026-06-11T08:00:00+00:00",
    )
    tmux = FakeTmux()

    resumed = resume_missing_local_workers(
        settings,
        db=db,
        tmux=tmux,  # type: ignore[arg-type]
        nudge_sleep=lambda _: None,
    )

    assert [record.name for record in resumed] == ["goal-resume"]
    started = tmux.started_commands["goal-resume"]
    assert (
        "claude --dangerously-skip-permissions --resume "
        "0aa6aa5e-3a64-4a32-9a2b-9f4b1f2a3c4d" in started
    )
    assert tmux.sent["goal-resume"], "expected a continue nudge after resume"
    assert any("continue" in text.lower() for text in tmux.sent["goal-resume"])
    record = db.get_managed_session("goal-resume")
    assert record is not None
    assert record["command"] == "claude --dangerously-skip-permissions"
    with sqlite3.connect(settings.db_path) as conn:
        row = conn.execute(
            "SELECT payload_json FROM audit_events"
            " WHERE event_type = 'worker_resumed_after_outage'"
        ).fetchone()
    payload = json.loads(row[0])
    assert payload["resumed_runtime_session"] == "0aa6aa5e-3a64-4a32-9a2b-9f4b1f2a3c4d"


def test_resume_missing_local_workers_resumes_codex_conversation(
    tmp_path: Path,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="cdjk",
    )
    db = BemineDB(settings.db_path)
    cwd = str(project.resolve())
    _managed_worker(db, cwd=cwd, runtime="codex", command="codex --yolo")
    _index_session(
        db,
        session_id="codex:7bb7bb6f-4b75-4b43-8b3c-0a5c2a3b4c5d",
        runtime="codex",
        cwd=cwd,
        updated_at="2026-06-11T08:00:00+00:00",
    )
    tmux = FakeTmux()

    resumed = resume_missing_local_workers(
        settings,
        db=db,
        tmux=tmux,  # type: ignore[arg-type]
        nudge_sleep=lambda _: None,
    )

    assert [record.name for record in resumed] == ["goal-resume"]
    started = tmux.started_commands["goal-resume"]
    assert (
        "codex resume 7bb7bb6f-4b75-4b43-8b3c-0a5c2a3b4c5d"
        " --dangerously-bypass-approvals-and-sandbox" in started
    )
    assert "--yolo" not in started


def test_resume_missing_local_workers_picks_latest_indexed_session(
    tmp_path: Path,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="cdjk",
    )
    db = BemineDB(settings.db_path)
    cwd = str(project.resolve())
    _managed_worker(
        db, cwd=cwd, runtime="claude", command="claude --dangerously-skip-permissions"
    )
    _index_session(
        db,
        session_id="claude:11111111-1111-4111-8111-111111111111",
        runtime="claude",
        cwd=cwd,
        updated_at="2026-06-10T08:00:00+00:00",
    )
    _index_session(
        db,
        session_id="claude:22222222-2222-4222-8222-222222222222",
        runtime="claude",
        cwd=cwd,
        updated_at="2026-06-11T09:30:00+00:00",
    )
    tmux = FakeTmux()

    resume_missing_local_workers(
        settings,
        db=db,
        tmux=tmux,  # type: ignore[arg-type]
        nudge_sleep=lambda _: None,
    )

    assert (
        "--resume 22222222-2222-4222-8222-222222222222"
        in tmux.started_commands["goal-resume"]
    )


def test_resume_missing_local_workers_falls_back_without_indexed_session(
    tmp_path: Path,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="cdjk",
    )
    db = BemineDB(settings.db_path)
    cwd = str(project.resolve())
    _managed_worker(
        db, cwd=cwd, runtime="claude", command="claude --dangerously-skip-permissions"
    )
    tmux = FakeTmux()

    resumed = resume_missing_local_workers(
        settings,
        db=db,
        tmux=tmux,  # type: ignore[arg-type]
        nudge_sleep=lambda _: None,
    )

    assert [record.name for record in resumed] == ["goal-resume"]
    started = tmux.started_commands["goal-resume"]
    assert "--resume" not in started
    assert tmux.sent["goal-resume"] == []


def test_resume_missing_local_workers_ignores_synthetic_session_ids(
    tmp_path: Path,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="cdjk",
    )
    db = BemineDB(settings.db_path)
    cwd = str(project.resolve())
    _managed_worker(
        db, cwd=cwd, runtime="claude", command="claude --dangerously-skip-permissions"
    )
    _index_session(
        db,
        session_id="claude:claude_a1b2c3",
        runtime="claude",
        cwd=cwd,
        updated_at="2026-06-11T08:00:00+00:00",
    )
    tmux = FakeTmux()

    resume_missing_local_workers(
        settings,
        db=db,
        tmux=tmux,  # type: ignore[arg-type]
        nudge_sleep=lambda _: None,
    )

    assert "--resume" not in tmux.started_commands["goal-resume"]


def test_resume_missing_local_workers_ignores_other_device_sessions(
    tmp_path: Path,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="cdjk",
    )
    db = BemineDB(settings.db_path)
    cwd = str(project.resolve())
    _managed_worker(
        db, cwd=cwd, runtime="claude", command="claude --dangerously-skip-permissions"
    )
    _index_session(
        db,
        session_id="claude:33333333-3333-4333-8333-333333333333",
        runtime="claude",
        cwd=cwd,
        updated_at="2026-06-11T08:00:00+00:00",
        device_id="mbp",
    )
    tmux = FakeTmux()

    resume_missing_local_workers(
        settings,
        db=db,
        tmux=tmux,  # type: ignore[arg-type]
        nudge_sleep=lambda _: None,
    )

    assert "--resume" not in tmux.started_commands["goal-resume"]


def test_worker_start_pins_claude_session_id(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="cdjk",
    )
    db = BemineDB(settings.db_path)
    tmux = FakeTmux()
    controller = WorkerController(settings, db=db, tmux=tmux)  # type: ignore[arg-type]

    record = controller.start(
        name="goal-pin",
        runtime="claude",
        cwd=project,
        command="claude --dangerously-skip-permissions",
    )

    started = tmux.started_commands["goal-pin"]
    assert "--session-id " in started
    assert record.runtime_session_id
    assert record.runtime_session_id in started
    assert uuid.UUID(record.runtime_session_id)
    row = db.get_managed_session("goal-pin")
    assert row is not None
    assert row["runtime_session_id"] == record.runtime_session_id
    assert row["command"] == "claude --dangerously-skip-permissions"


def test_worker_start_pins_kimi_session_id(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="cdjk",
    )
    db = BemineDB(settings.db_path)
    tmux = FakeTmux()
    controller = WorkerController(settings, db=db, tmux=tmux)  # type: ignore[arg-type]

    record = controller.start(name="goal-kimi", runtime="kimi", cwd=project)

    started = tmux.started_commands["goal-kimi"]
    assert "--session " in started
    assert record.runtime_session_id
    assert record.runtime_session_id in started
    assert uuid.UUID(record.runtime_session_id)


def test_resume_kimi_uses_stored_session_id(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="cdjk",
    )
    db = BemineDB(settings.db_path)
    cwd = str(project.resolve())
    _managed_worker(db, cwd=cwd, runtime="kimi", command="kimi")
    db.set_worker_runtime_session("goal-resume", "70fa9938-8d44-49d6-8a4d-61b2e3307b20")
    tmux = FakeTmux()

    resume_missing_local_workers(
        settings,
        db=db,
        tmux=tmux,  # type: ignore[arg-type]
        nudge_sleep=lambda _: None,
    )

    started = tmux.started_commands["goal-resume"]
    # kimi --session creates-or-resumes, so no on-disk transcript check needed
    assert "kimi --session 70fa9938-8d44-49d6-8a4d-61b2e3307b20" in started
    assert any("continue" in text.lower() for text in tmux.sent["goal-resume"])


def test_worker_restart_resets_stale_runtime_session_id(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="cdjk",
    )
    db = BemineDB(settings.db_path)
    cwd = str(project.resolve())
    _managed_worker(db, cwd=cwd, runtime="codex", command="codex --yolo")
    db.set_worker_runtime_session("goal-resume", "11111111-1111-4111-8111-111111111111")
    tmux = FakeTmux()
    controller = WorkerController(settings, db=db, tmux=tmux)  # type: ignore[arg-type]

    # user re-creates the worker (new conversation, even a new runtime)
    record = controller.start(
        name="goal-resume", runtime="codex", cwd=project, command="codex --yolo"
    )

    assert record.runtime_session_id == ""
    row = db.get_managed_session("goal-resume")
    assert row is not None
    # the old conversation id must not survive a fresh start, or continuity
    # would resurrect the previous conversation instead of the new one
    assert row["runtime_session_id"] == ""


def test_resume_nudge_answers_kimi_upgrade_prompt_with_skip(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="cdjk",
    )
    db = BemineDB(settings.db_path)
    cwd = str(project.resolve())
    _managed_worker(db, cwd=cwd, runtime="kimi", command="kimi")
    db.set_worker_runtime_session("goal-resume", "70fa9938-8d44-49d6-8a4d-61b2e3307b20")
    tmux = FakeTmux()
    tmux.capture = lambda name: (  # type: ignore[method-assign]
        "[Enter]  Upgrade now  (uv tool upgrade kimi-cli)\n"
        "[q]      Not now, remind me next time\n"
        "[s]      Skip reminders for version 1.47.0\n"
    )

    resume_missing_local_workers(
        settings,
        db=db,
        tmux=tmux,  # type: ignore[arg-type]
        nudge_sleep=lambda _: None,
    )

    sent = tmux.sent["goal-resume"]
    # a blind Enter on the upgrade prompt would run `uv tool upgrade`; the
    # nudge must dismiss the prompt with `s` before typing
    assert sent[0] == "s", f"expected upgrade prompt dismissed first, got {sent[:2]}"
    assert any("continue" in text.lower() for text in sent)


def test_worker_start_resolves_runtime_binary_for_launch(
    tmp_path: Path, monkeypatch
) -> None:
    from bemine.workers import workers as workers_mod

    monkeypatch.setattr(
        workers_mod, "_resolve_runtime_bin", lambda runtime_key: f"/abs/{runtime_key}"
    )
    project = tmp_path / "repo"
    project.mkdir()
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="cdjk",
    )
    db = BemineDB(settings.db_path)
    tmux = FakeTmux()
    controller = WorkerController(settings, db=db, tmux=tmux)  # type: ignore[arg-type]

    record = controller.start(name="goal-path", runtime="claude", cwd=project)

    started = tmux.started_commands["goal-path"]
    # the daemon's systemd PATH lacks ~/.local/bin etc.; post-reboot the first
    # tmux server inherits it, so bare CLI names would die "command not found"
    assert "exec /abs/claude --dangerously-skip-permissions" in started
    row = db.get_managed_session("goal-path")
    assert row is not None
    assert row["command"] == "claude --dangerously-skip-permissions"
    assert record.runtime_session_id in started


def test_resume_resolves_runtime_binary_for_launch(tmp_path: Path, monkeypatch) -> None:
    from bemine.workers import workers as workers_mod

    monkeypatch.setattr(
        workers_mod, "_resolve_runtime_bin", lambda runtime_key: f"/abs/{runtime_key}"
    )
    project = tmp_path / "repo"
    project.mkdir()
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="cdjk",
    )
    db = BemineDB(settings.db_path)
    cwd = str(project.resolve())
    _managed_worker(db, cwd=cwd, runtime="kimi", command="kimi")
    db.set_worker_runtime_session("goal-resume", "70fa9938-8d44-49d6-8a4d-61b2e3307b20")
    tmux = FakeTmux()

    resume_missing_local_workers(
        settings,
        db=db,
        tmux=tmux,  # type: ignore[arg-type]
        nudge_sleep=lambda _: None,
    )

    started = tmux.started_commands["goal-resume"]
    assert "exec /abs/kimi --session 70fa9938-8d44-49d6-8a4d-61b2e3307b20" in started


def test_launchable_command_leaves_custom_and_absolute_commands(tmp_path) -> None:
    from bemine.workers.workers import _launchable_command

    assert _launchable_command("kimi", "cat") == "cat"
    assert _launchable_command("claude", "/x/claude --flag") == "/x/claude --flag"


def test_worker_start_does_not_pin_custom_commands(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="cdjk",
    )
    db = BemineDB(settings.db_path)
    tmux = FakeTmux()
    controller = WorkerController(settings, db=db, tmux=tmux)  # type: ignore[arg-type]

    # runtime kimi but a custom command: CLI flags would be fatal for `cat`
    record = controller.start(
        name="goal-custom", runtime="kimi", cwd=project, command="cat"
    )

    assert "--session" not in tmux.started_commands["goal-custom"]
    assert record.runtime_session_id == ""


def test_worker_start_does_not_pin_codex_session_id(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="cdjk",
    )
    db = BemineDB(settings.db_path)
    tmux = FakeTmux()
    controller = WorkerController(settings, db=db, tmux=tmux)  # type: ignore[arg-type]

    record = controller.start(
        name="goal-codex", runtime="codex", cwd=project, command="codex --yolo"
    )

    assert "--session-id" not in tmux.started_commands["goal-codex"]
    assert record.runtime_session_id == ""


def test_link_worker_runtime_sessions_links_session_started_after_worker(
    tmp_path: Path,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="cdjk",
    )
    db = BemineDB(settings.db_path)
    cwd = str(project.resolve())
    _managed_worker(db, cwd=cwd, runtime="codex", command="codex --yolo")
    future = datetime.now(timezone.utc) + timedelta(seconds=60)
    _index_session(
        db,
        session_id="codex:44444444-4444-4444-8444-444444444444",
        runtime="codex",
        cwd=cwd,
        updated_at=future.isoformat(),
    )

    linked = link_worker_runtime_sessions(settings, db=db)

    assert linked == [("goal-resume", "44444444-4444-4444-8444-444444444444")]
    row = db.get_managed_session("goal-resume")
    assert row is not None
    assert row["runtime_session_id"] == "44444444-4444-4444-8444-444444444444"


def test_link_worker_runtime_sessions_ignores_session_started_before_worker(
    tmp_path: Path,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="cdjk",
    )
    db = BemineDB(settings.db_path)
    cwd = str(project.resolve())
    _managed_worker(db, cwd=cwd, runtime="codex", command="codex --yolo")
    _index_session(
        db,
        session_id="codex:55555555-5555-4555-8555-555555555555",
        runtime="codex",
        cwd=cwd,
        updated_at="2020-01-01T00:00:00+00:00",
    )

    linked = link_worker_runtime_sessions(settings, db=db)

    assert linked == []
    row = db.get_managed_session("goal-resume")
    assert row is not None
    assert row["runtime_session_id"] == ""


def test_link_worker_runtime_sessions_skips_workers_with_session(
    tmp_path: Path,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="cdjk",
    )
    db = BemineDB(settings.db_path)
    cwd = str(project.resolve())
    _managed_worker(db, cwd=cwd, runtime="codex", command="codex --yolo")
    db.set_worker_runtime_session("goal-resume", "66666666-6666-4666-8666-666666666666")
    future = datetime.now(timezone.utc) + timedelta(seconds=60)
    _index_session(
        db,
        session_id="codex:77777777-7777-4777-8777-777777777777",
        runtime="codex",
        cwd=cwd,
        updated_at=future.isoformat(),
    )

    assert link_worker_runtime_sessions(settings, db=db) == []
    row = db.get_managed_session("goal-resume")
    assert row is not None
    assert row["runtime_session_id"] == "66666666-6666-4666-8666-666666666666"


def _write_runtime_transcript(
    settings: BemineSettings, runtime: str, session_id: str
) -> Path:
    if runtime == "claude":
        path = settings.claude_home / "projects" / "proj" / f"{session_id}.jsonl"
    else:
        path = (
            settings.codex_home
            / "sessions"
            / "2026"
            / "06"
            / "11"
            / f"rollout-2026-06-11T00-00-00-{session_id}.jsonl"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")
    return path


def test_resume_prefers_stored_runtime_session_id_over_index(
    tmp_path: Path,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        claude_home=tmp_path / "claude_home",
        codex_home=tmp_path / "codex_home",
        device_id="cdjk",
    )
    db = BemineDB(settings.db_path)
    cwd = str(project.resolve())
    _managed_worker(
        db, cwd=cwd, runtime="claude", command="claude --dangerously-skip-permissions"
    )
    db.set_worker_runtime_session("goal-resume", "88888888-8888-4888-8888-888888888888")
    _write_runtime_transcript(
        settings, "claude", "88888888-8888-4888-8888-888888888888"
    )
    _index_session(
        db,
        session_id="claude:99999999-9999-4999-8999-999999999999",
        runtime="claude",
        cwd=cwd,
        updated_at="2026-06-11T09:00:00+00:00",
    )
    tmux = FakeTmux()

    resume_missing_local_workers(
        settings,
        db=db,
        tmux=tmux,  # type: ignore[arg-type]
        nudge_sleep=lambda _: None,
    )

    started = tmux.started_commands["goal-resume"]
    assert "--resume 88888888-8888-4888-8888-888888888888" in started
    assert "99999999" not in started
    row = db.get_managed_session("goal-resume")
    assert row is not None
    assert row["runtime_session_id"] == "88888888-8888-4888-8888-888888888888"


def test_resume_stored_claude_id_missing_on_disk_pins_fresh(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        claude_home=tmp_path / "claude_home",
        codex_home=tmp_path / "codex_home",
        device_id="cdjk",
    )
    db = BemineDB(settings.db_path)
    cwd = str(project.resolve())
    _managed_worker(
        db, cwd=cwd, runtime="claude", command="claude --dangerously-skip-permissions"
    )
    db.set_worker_runtime_session("goal-resume", "88888888-8888-4888-8888-888888888888")
    tmux = FakeTmux()

    resume_missing_local_workers(
        settings,
        db=db,
        tmux=tmux,  # type: ignore[arg-type]
        nudge_sleep=lambda _: None,
    )

    started = tmux.started_commands["goal-resume"]
    # resuming an id with no transcript exits 1 and crash-loops; must start fresh
    assert "--resume" not in started
    assert "--session-id " in started
    row = db.get_managed_session("goal-resume")
    assert row is not None
    assert row["runtime_session_id"] != "88888888-8888-4888-8888-888888888888"
    assert row["runtime_session_id"] in started


def test_resume_stored_codex_id_missing_on_disk_falls_back_to_index(
    tmp_path: Path,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        claude_home=tmp_path / "claude_home",
        codex_home=tmp_path / "codex_home",
        device_id="cdjk",
    )
    db = BemineDB(settings.db_path)
    cwd = str(project.resolve())
    _managed_worker(db, cwd=cwd, runtime="codex", command="codex --yolo")
    db.set_worker_runtime_session("goal-resume", "00000000-0000-4000-8000-000000000000")
    _index_session(
        db,
        session_id="codex:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        runtime="codex",
        cwd=cwd,
        updated_at="2026-06-11T09:00:00+00:00",
    )
    tmux = FakeTmux()

    resume_missing_local_workers(
        settings,
        db=db,
        tmux=tmux,  # type: ignore[arg-type]
        nudge_sleep=lambda _: None,
    )

    started = tmux.started_commands["goal-resume"]
    assert "codex resume aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa" in started
    assert "00000000" not in started


def test_resume_heuristic_skips_deleted_transcript(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        claude_home=tmp_path / "claude_home",
        codex_home=tmp_path / "codex_home",
        device_id="cdjk",
    )
    db = BemineDB(settings.db_path)
    cwd = str(project.resolve())
    _managed_worker(
        db, cwd=cwd, runtime="claude", command="claude --dangerously-skip-permissions"
    )
    _index_session(
        db,
        session_id="claude:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        runtime="claude",
        cwd=cwd,
        updated_at="2026-06-11T09:00:00+00:00",
        write_transcript=False,
    )
    tmux = FakeTmux()

    resume_missing_local_workers(
        settings,
        db=db,
        tmux=tmux,  # type: ignore[arg-type]
        nudge_sleep=lambda _: None,
    )

    started = tmux.started_commands["goal-resume"]
    assert "--resume" not in started
    assert "--session-id " in started


def test_resume_nudge_waits_then_sends_text_and_enters(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        claude_home=tmp_path / "claude_home",
        codex_home=tmp_path / "codex_home",
        device_id="cdjk",
    )
    db = BemineDB(settings.db_path)
    cwd = str(project.resolve())
    _managed_worker(
        db, cwd=cwd, runtime="claude", command="claude --dangerously-skip-permissions"
    )
    db.set_worker_runtime_session("goal-resume", "88888888-8888-4888-8888-888888888888")
    _write_runtime_transcript(
        settings, "claude", "88888888-8888-4888-8888-888888888888"
    )
    tmux = FakeTmux()
    sleeps: list[float] = []

    resume_missing_local_workers(
        settings,
        db=db,
        tmux=tmux,  # type: ignore[arg-type]
        nudge_sleep=sleeps.append,
    )

    sent = tmux.sent["goal-resume"]
    # text first (no Enter), then Enter separately, then a second idempotent
    # Enter in case the first landed before the TUI was ready
    assert sent[0].rstrip("\n") == sent[0], "nudge text must be sent without Enter"
    assert "continue" in sent[0].lower()
    assert sent[1:] == ["\n", "\n"]
    assert sleeps, "nudge must wait for the CLI to boot before typing"


def test_resume_claude_fallback_pins_fresh_session_id(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="cdjk",
    )
    db = BemineDB(settings.db_path)
    cwd = str(project.resolve())
    _managed_worker(
        db, cwd=cwd, runtime="claude", command="claude --dangerously-skip-permissions"
    )
    tmux = FakeTmux()

    resume_missing_local_workers(
        settings,
        db=db,
        tmux=tmux,  # type: ignore[arg-type]
        nudge_sleep=lambda _: None,
    )

    started = tmux.started_commands["goal-resume"]
    assert "--resume" not in started
    assert "--session-id " in started
    row = db.get_managed_session("goal-resume")
    assert row is not None
    assert row["runtime_session_id"]
    assert row["runtime_session_id"] in started
    assert tmux.sent["goal-resume"] == []


def test_list_goal_sessions_joins_goal_worker_and_active_watcher(
    tmp_path: Path,
) -> None:
    project = tmp_path / "rai-speech-toolkit"
    project.mkdir()
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="cdjk",
    )
    db = BemineDB(settings.db_path)
    db.upsert_managed_session(
        name="goal-rai",
        runtime="shell",
        transport="tmux",
        device_id="cdjk",
        project_id="proj_rai",
        cwd=str(project),
        command="cat",
        status="running",
    )
    db.upsert_project_watcher(
        project_id="proj_rai",
        channel_id="chan-rai",
        brief="train Rai checkpoint",
        interval_secs=600,
    )
    tmux = FakeTmux()
    tmux.sessions.add("goal-rai")
    controller = WorkerController(settings, db=db, tmux=tmux)  # type: ignore[arg-type]

    sessions = list_goal_sessions(settings, db=db, controller=controller)

    assert len(sessions) == 1
    assert sessions[0].project_id == "proj_rai"
    assert sessions[0].worker is not None
    assert sessions[0].worker.name == "goal-rai"
    assert sessions[0].watcher is not None
    assert sessions[0].watcher["channel_id"] == "chan-rai"
    assert list_goal_sessions(
        settings, status="checkpoint", db=db, controller=controller
    )
    rendered = render_goal_sessions(sessions)
    assert "goal-rai" in rendered
    assert "chan-rai" in rendered


def test_start_goal_resolves_remote_project_from_fleet_status(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="cdjk",
    )
    db = BemineDB(settings.db_path)
    local_tmux = FakeTmux()
    remote_tmux = FakeTmux()
    controller = WorkerController(
        settings,
        db=db,
        tmux=local_tmux,  # type: ignore[arg-type]
        remote_tmux_factory=lambda _host: remote_tmux,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        "bemine.goals.collect_fleet_status",
        lambda *a, **k: (
            [
                SessionStatus(
                    device_id="mbp",
                    runtime="claude",
                    session_id="s1",
                    cwd="/Users/cdjk/github/llm/rai-speech-toolkit",
                    source_path="",
                    last_interaction_at=100.0,
                    age_seconds=5,
                    last_role="assistant",
                    asks_user=False,
                    ask_summary="",
                    latest_summary="",
                )
            ],
            [],
        ),
    )

    result = start_goal(
        settings,
        project="rai-speech-toolkit",
        brief="resume training",
        runtime="shell",
        worker_name="goal-rai",
        command="cat",
        db=db,
        controller=controller,
    )

    assert result.worker.device_id == "mbp"
    assert result.worker.transport == "ssh-tmux"
    assert result.worker.cwd == "/Users/cdjk/github/llm/rai-speech-toolkit"
    assert "goal-rai" in remote_tmux.sessions
    assert local_tmux.sessions == set()


def test_cli_goal_with_command_option_starts_worker_and_watcher(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    calls = {}

    class FakeController:
        def start(self, **kwargs):
            return self.start_on_device(**kwargs)

        def start_on_device(self, **kwargs):
            calls["start"] = kwargs
            return _Record(
                name=kwargs["name"],
                runtime=kwargs["runtime"],
                cwd=str(kwargs["cwd"]),
                command=kwargs["command"],
            )

        def send(self, name, text):
            calls["send"] = (name, text)

    monkeypatch.setattr(
        "bemine.goals.WorkerController",
        lambda settings, db=None: FakeController(),
    )

    main(
        [
            "--state-dir",
            str(tmp_path / "state"),
            "--vault",
            str(tmp_path / "vault"),
            "--device",
            "testbox",
            "goal",
            "demo",
            "smoke goal",
            "--cwd",
            str(project),
            "--runtime",
            "shell",
            "--worker",
            "goal-demo",
            "--command",
            "cat",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["worker"]["name"] == "goal-demo"
    assert payload["worker"]["command"] == "cat"
    assert payload["watcher"]["brief"] == "smoke goal"


def test_cli_goal_list_json_reports_empty_sessions(tmp_path: Path, capsys) -> None:
    main(
        [
            "--state-dir",
            str(tmp_path / "state"),
            "--vault",
            str(tmp_path / "vault"),
            "--device",
            "testbox",
            "goal",
            "--list",
            "--json",
        ]
    )

    assert json.loads(capsys.readouterr().out) == []


def test_cli_goal_requires_project_and_brief_for_start(tmp_path: Path) -> None:
    try:
        main(
            [
                "--state-dir",
                str(tmp_path / "state"),
                "--vault",
                str(tmp_path / "vault"),
                "--device",
                "testbox",
                "goal",
            ]
        )
    except SystemExit as exc:
        assert "goal requires PROJECT and brief" in str(exc)
    else:
        raise AssertionError("goal without project/brief should exit")


class FakeTmux:
    def __init__(self) -> None:
        self.sessions: set[str] = set()
        self.sent: dict[str, list[str]] = {}
        self.started_commands: dict[str, str] = {}

    def start(self, name: str, command: str = "bash") -> None:
        self.sessions.add(name)
        self.started_commands[name] = command
        self.sent.setdefault(name, [])

    def exists(self, name: str) -> bool:
        return name in self.sessions

    def send(self, name: str, text: str, *, enter: bool = True) -> None:
        suffix = "\n" if enter else ""
        self.sent.setdefault(name, []).append(text + suffix)

    def capture(self, name: str) -> str:
        return ""

    def kill(self, name: str) -> None:
        self.sessions.discard(name)


class _Record:
    def __init__(self, *, name: str, runtime: str, cwd: str, command: str) -> None:
        self.name = name
        self.runtime = runtime
        self.transport = "tmux"
        self.device_id = "testbox"
        self.project_id = "proj_demo"
        self.cwd = cwd
        self.command = command
        self.status = "running"
        self.exists = True

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "runtime": self.runtime,
            "transport": self.transport,
            "device_id": self.device_id,
            "project_id": self.project_id,
            "cwd": self.cwd,
            "command": self.command,
            "status": self.status,
            "exists": self.exists,
        }
