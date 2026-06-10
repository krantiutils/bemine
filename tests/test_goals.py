from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from bemine.config import BemineSettings
from bemine.cli import main
from bemine.db import BemineDB
from bemine.goals import start_goal
from bemine.index.status import SessionStatus
from bemine.workers.workers import WorkerController, resume_missing_local_workers


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
    assert watcher["brief"] == "train the first Rai TTS checkpoint from the 14 rulebooks"

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

    resumed = resume_missing_local_workers(settings, db=db, tmux=tmux)  # type: ignore[arg-type]

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
