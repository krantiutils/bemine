from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.workers.tmux_adapter import TmuxAdapter
from bemine.workers.workers import SshTmuxAdapter, WorkerController


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "bemine.cli",
            "--state-dir",
            str(tmp_path / "state"),
            "--vault",
            str(tmp_path / "vault"),
            "--device",
            "testbox",
            *args,
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
def test_workers_start_send_capture_list_and_stop(tmp_path: Path) -> None:
    name = f"bemine-worker-{uuid.uuid4().hex[:10]}"
    project = tmp_path / "repo"
    project.mkdir()

    try:
        started = json.loads(
            _run(
                tmp_path,
                "workers",
                "start",
                name,
                "--runtime",
                "shell",
                "--cwd",
                str(project),
                "--command",
                "bash",
                "--json",
            ).stdout
        )
        assert started["name"] == name
        assert started["runtime"] == "shell"
        assert started["status"] == "running"
        assert started["exists"] is True

        _run(tmp_path, "workers", "send", name, "printf 'WORKER_READY\\n'")
        captured = _capture_until(tmp_path, name, "WORKER_READY")
        assert "WORKER_READY" in captured

        listed = json.loads(_run(tmp_path, "workers", "list", "--json").stdout)
        assert listed[0]["name"] == name
        assert listed[0]["exists"] is True

        stopped = json.loads(_run(tmp_path, "workers", "stop", name, "--json").stdout)
        assert stopped["status"] == "stopped"
        assert stopped["exists"] is False

        forgotten = json.loads(_run(tmp_path, "workers", "forget", name, "--json").stdout)
        assert forgotten == {"forgotten": True, "name": name}
        assert json.loads(_run(tmp_path, "workers", "list", "--json").stdout) == []
    finally:
        TmuxAdapter().kill(name)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
def test_workers_can_adopt_existing_tmux_session(tmp_path: Path) -> None:
    name = f"bemine-adopt-{uuid.uuid4().hex[:10]}"
    project = tmp_path / "repo"
    project.mkdir()
    tmux = TmuxAdapter()
    try:
        tmux.start(name, "bash")
        adopted = json.loads(
            _run(
                tmp_path,
                "workers",
                "adopt",
                name,
                "--runtime",
                "claude",
                "--cwd",
                str(project),
                "--json",
            ).stdout
        )
        assert adopted["name"] == name
        assert adopted["runtime"] == "claude"
        assert adopted["status"] == "adopted"

        _run(tmp_path, "workers", "send", name, "printf 'ADOPTED_READY\\n'")
        captured = _capture_until(tmp_path, name, "ADOPTED_READY")
        assert "ADOPTED_READY" in captured
    finally:
        tmux.kill(name)


def test_workers_transfer_switches_runtime_reports_duplicates_and_audits(
    tmp_path: Path,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="testbox",
    )
    db = BemineDB(settings.db_path)
    tmux = FakeTmux()
    controller = WorkerController(settings, db=db, tmux=tmux)  # type: ignore[arg-type]

    tmux.start("source", "codex")
    controller.adopt(name="source", runtime="codex", cwd=project)
    tmux.captures["source"] = "source pane says: continue the migration\n"

    tmux.start("duplicate", "kimi")
    controller.adopt(name="duplicate", runtime="kimi", cwd=project)

    tmux.start("target", "claude")
    result = controller.transfer(
        source_name="source",
        target_name="target",
        target_runtime="claude",
        target_device="testbox",
    )

    assert result.source.runtime == "codex"
    assert result.target.runtime == "claude"
    assert result.target.status == "adopted"
    assert [record.name for record in result.duplicate_heads] == ["duplicate"]
    assert result.prompt in tmux.sent["target"][-1]
    assert "Source runtime: codex" in result.prompt
    assert "Target runtime: claude" in result.prompt
    assert "Target device: testbox" in result.prompt
    assert "source pane says: continue the migration" in result.prompt
    assert "duplicate [kimi]" in result.prompt
    assert "preserve source context" in result.prompt.lower()

    with sqlite3.connect(settings.db_path) as conn:
        row = conn.execute(
            """
            SELECT category, event_type, actor, device_id, payload_json
            FROM audit_events
            WHERE event_type = 'worker_transfer'
            """
        ).fetchone()
    assert row[:4] == ("worker", "worker_transfer", "worker-controller", "testbox")
    payload = json.loads(row[4])
    assert payload["source"]["name"] == "source"
    assert payload["target"]["runtime"] == "claude"
    assert payload["target_device"] == "testbox"
    assert payload["conflict_policy"] == "preserve_all_heads_with_explicit_handoff"
    assert [record["name"] for record in payload["duplicate_heads"]] == ["duplicate"]
    assert payload["source_capture_len"] == len(tmux.captures["source"])


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
def test_workers_transfer_injects_handoff_into_existing_tmux_target(
    tmp_path: Path,
) -> None:
    source = f"bemine-source-{uuid.uuid4().hex[:10]}"
    target = f"bemine-target-{uuid.uuid4().hex[:10]}"
    duplicate = f"bemine-dup-{uuid.uuid4().hex[:10]}"
    project = tmp_path / "repo"
    project.mkdir()
    tmux = TmuxAdapter()
    try:
        _run(
            tmp_path,
            "workers",
            "start",
            source,
            "--runtime",
            "codex",
            "--cwd",
            str(project),
            "--command",
            "bash",
            "--json",
        )
        _run(tmp_path, "workers", "send", source, "printf 'SOURCE_HANDOFF_READY\\n'")
        assert "SOURCE_HANDOFF_READY" in _capture_until(
            tmp_path, source, "SOURCE_HANDOFF_READY"
        )

        _run(
            tmp_path,
            "workers",
            "start",
            duplicate,
            "--runtime",
            "kimi",
            "--cwd",
            str(project),
            "--command",
            "cat",
            "--json",
        )
        tmux.start(target, "cat")

        transferred = json.loads(
            _run(
                tmp_path,
                "workers",
                "transfer",
                source,
                target,
                "--runtime",
                "claude",
                "--cwd",
                str(project),
                "--target-device",
                "testbox",
                "--json",
            ).stdout
        )
        assert transferred["source"]["name"] == source
        assert transferred["target"]["name"] == target
        assert transferred["target"]["runtime"] == "claude"
        assert [record["name"] for record in transferred["duplicate_heads"]] == [
            duplicate
        ]

        captured = _capture_until(tmp_path, target, "SOURCE_HANDOFF_READY")
        assert "Bemine worker transfer handoff." in captured
        assert "Source runtime: codex" in captured
        assert "Target runtime: claude" in captured
        assert "Target device: testbox" in captured
        assert "SOURCE_HANDOFF_READY" in captured
        assert duplicate in captured
    finally:
        for name in (source, target, duplicate):
            tmux.kill(name)


def test_workers_transfer_to_remote_device_uses_ssh_tmux_and_audits(
    tmp_path: Path,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="testbox",
    )
    db = BemineDB(settings.db_path)
    local_tmux = FakeTmux()
    remote_tmux = FakeTmux()
    remotes = {"mbp2": remote_tmux}
    controller = WorkerController(
        settings,
        db=db,
        tmux=local_tmux,  # type: ignore[arg-type]
        remote_tmux_factory=lambda host: remotes[host],  # type: ignore[arg-type]
    )

    local_tmux.start("source", "codex")
    controller.adopt(name="source", runtime="codex", cwd=project)
    local_tmux.captures["source"] = "remote transfer source context\n"
    local_tmux.start("duplicate", "kimi")
    controller.adopt(name="duplicate", runtime="kimi", cwd=project)
    remote_tmux.start("target", "cat")

    result = controller.transfer(
        source_name="source",
        target_name="target",
        target_runtime="claude",
        target_device="mbp2",
        cwd=project,
    )

    assert result.target.transport == "ssh-tmux"
    assert result.target.device_id == "mbp2"
    assert result.target.status == "adopted"
    assert "target" not in local_tmux.sent
    assert "Bemine worker transfer handoff." in remote_tmux.sent["target"][-1]
    assert "Source device: testbox" in remote_tmux.sent["target"][-1]
    assert "Target device: mbp2" in remote_tmux.sent["target"][-1]
    assert "remote transfer source context" in remote_tmux.sent["target"][-1]
    assert [record.name for record in result.duplicate_heads] == ["duplicate"]

    listed = {record.name: record for record in controller.list()}
    assert listed["target"].transport == "ssh-tmux"
    assert listed["target"].device_id == "mbp2"
    assert listed["target"].exists is True

    with sqlite3.connect(settings.db_path) as conn:
        row = conn.execute(
            """
            SELECT payload_json
            FROM audit_events
            WHERE event_type = 'worker_transfer'
            """
        ).fetchone()
    payload = json.loads(row[0])
    assert payload["target"]["transport"] == "ssh-tmux"
    assert payload["target"]["device_id"] == "mbp2"
    assert payload["duplicate_heads"][0]["name"] == "duplicate"


def test_workers_duplicate_heads_reports_active_local_and_remote_heads(
    tmp_path: Path,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="testbox",
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

    local_tmux.start("codex-head", "codex")
    controller.adopt(name="codex-head", runtime="codex", cwd=project)
    local_tmux.start("stopped-head", "kimi")
    controller.adopt(name="stopped-head", runtime="kimi", cwd=project)
    controller.stop("stopped-head")
    remote_tmux.start("claude-head", "claude")
    controller.transfer(
        source_name="codex-head",
        target_name="claude-head",
        target_runtime="claude",
        target_device="mbp2",
        cwd=project,
    )

    groups = controller.duplicate_heads()

    assert len(groups) == 1
    assert groups[0].project_id
    assert {record.name for record in groups[0].heads} == {
        "codex-head",
        "claude-head",
    }
    assert {record.transport for record in groups[0].heads} == {"tmux", "ssh-tmux"}
    assert "stopped-head" not in {record.name for record in groups[0].heads}


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
def test_workers_duplicates_cli_reports_real_tmux_heads(tmp_path: Path) -> None:
    first = f"bemine-dup-a-{uuid.uuid4().hex[:10]}"
    second = f"bemine-dup-b-{uuid.uuid4().hex[:10]}"
    project = tmp_path / "repo"
    project.mkdir()
    tmux = TmuxAdapter()
    try:
        for name in (first, second):
            _run(
                tmp_path,
                "workers",
                "start",
                name,
                "--runtime",
                "shell",
                "--cwd",
                str(project),
                "--command",
                "cat",
                "--json",
            )

        groups = json.loads(_run(tmp_path, "workers", "duplicates", "--json").stdout)

        assert len(groups) == 1
        assert {record["name"] for record in groups[0]["heads"]} == {first, second}
    finally:
        for name in (first, second):
            tmux.kill(name)


def test_ssh_tmux_send_pipes_handoff_through_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    adapter = SshTmuxAdapter("mbp2")

    adapter.send("target", "line one\nline two")

    assert calls[0][1]["input"] == "line one\nline two"
    assert calls[0][0][0][-1].endswith("tmux load-buffer -")
    assert calls[1][0][0][-1].endswith("tmux paste-buffer -t target")
    assert calls[2][0][0][-1].endswith("tmux send-keys -t target Enter")
    assert "line one" not in " ".join(str(call[0][0]) for call in calls)


def _capture_until(tmp_path: Path, name: str, needle: str) -> str:
    deadline = time.time() + 5
    captured = ""
    while time.time() < deadline:
        captured = _run(tmp_path, "workers", "capture", name).stdout
        if needle in captured:
            return captured
        time.sleep(0.1)
    return captured


class FakeTmux:
    def __init__(self) -> None:
        self.sessions: set[str] = set()
        self.captures: dict[str, str] = {}
        self.sent: dict[str, list[str]] = {}

    def start(self, name: str, command: str = "bash") -> None:
        if name in self.sessions:
            raise AssertionError(f"session already exists: {name}")
        self.sessions.add(name)
        self.captures.setdefault(name, "")
        self.sent.setdefault(name, [])

    def exists(self, name: str) -> bool:
        return name in self.sessions

    def send(self, name: str, text: str, *, enter: bool = True) -> None:
        if name not in self.sessions:
            raise AssertionError(f"session does not exist: {name}")
        suffix = "\n" if enter else ""
        self.sent.setdefault(name, []).append(text + suffix)
        self.captures[name] = self.captures.get(name, "") + text + suffix

    def capture(self, name: str) -> str:
        if name not in self.sessions:
            raise AssertionError(f"session does not exist: {name}")
        return self.captures.get(name, "")

    def kill(self, name: str) -> None:
        self.sessions.discard(name)
