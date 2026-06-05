from __future__ import annotations

import json
import subprocess
from pathlib import Path

from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.kimi_handler import KimiMessageResult
from mayor_discord.dispatch import (
    PROJECT_CATEGORY_NAME,
    CodexDispatcher,
    DispatchRequest,
    DispatchResult,
    KimiDispatcher,
    ProjectDispatcher,
    ProjectChannel,
    codex_progress_from_event,
    dispatch_status_payload,
    infer_project_route,
    load_project_channels,
    project_channels_from_db,
    render_dispatch_status,
    resolve_project_channel,
    resolve_codex_bin,
    resolve_codex_sandbox,
    write_project_channel_config,
)


def test_project_channel_config_resolves_only_inside_project_category(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    project_root = tmp_path / "streaming-tts"
    project_root.mkdir()
    channel = ProjectChannel(
        channel_name="tts",
        cwd=project_root,
        project_id="proj_tts",
        project_name="streaming-tts",
    )

    path = write_project_channel_config(state_dir, [channel])

    assert path == state_dir / "discord-projects.json"
    loaded = load_project_channels(state_dir, repo_dir=tmp_path)
    assert loaded["tts"] == channel
    assert loaded["tts"].runtime == "kimi"
    assert (
        resolve_project_channel(
            channel_name="tts",
            category_name=PROJECT_CATEGORY_NAME,
            state_dir=state_dir,
            repo_dir=tmp_path,
        )
        == channel
    )
    assert (
        resolve_project_channel(
            channel_name="tts",
            category_name=f"{PROJECT_CATEGORY_NAME} 2",
            state_dir=state_dir,
            repo_dir=tmp_path,
        )
        == channel
    )
    assert (
        resolve_project_channel(
            channel_name="tts",
            category_name="general",
            state_dir=state_dir,
            repo_dir=tmp_path,
        )
        is None
    )


def test_general_channel_routes_by_content_mentions_and_mayor_fallback(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    bemine_root = tmp_path / "bm"
    tts_root = tmp_path / "streaming-tts"
    bemine_root.mkdir()
    tts_root.mkdir()
    bemine = ProjectChannel(channel_name="bemine", cwd=bemine_root, project_name="bm")
    tts = ProjectChannel(channel_name="tts", cwd=tts_root, project_name="streaming-tts")
    write_project_channel_config(state_dir, [bemine, tts])

    direct = infer_project_route(
        channel_name="tts",
        category_name=PROJECT_CATEGORY_NAME,
        content="what is blocked?",
        state_dir=state_dir,
        repo_dir=tmp_path,
    )
    assert direct is not None
    assert direct.project.channel_name == "tts"
    assert direct.reason == "project-channel"

    mentioned = infer_project_route(
        channel_name="general",
        category_name="Text Channels",
        content="can you check this?",
        mentioned_channels=[("tts", PROJECT_CATEGORY_NAME)],
        state_dir=state_dir,
        repo_dir=tmp_path,
    )
    assert mentioned is not None
    assert mentioned.project.channel_name == "tts"
    assert mentioned.reason == "mentioned-channel:tts"

    explicit = infer_project_route(
        channel_name="general",
        category_name="Text Channels",
        content="tts: summarize the current failures",
        state_dir=state_dir,
        repo_dir=tmp_path,
    )
    assert explicit is not None
    assert explicit.project.channel_name == "tts"
    assert explicit.reason == "content-project-reference"

    mayor = infer_project_route(
        channel_name="general",
        category_name="Text Channels",
        content="what should I look at next?",
        state_dir=state_dir,
        repo_dir=tmp_path,
    )
    assert mayor is not None
    assert mayor.project.channel_name == "bemine"
    assert mayor.reason == "default-mayor"


def test_project_channels_from_db_make_per_project_names_unique(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("BEMINE_CONFIG", str(tmp_path / "missing-bemine.toml"))
    db = BemineDB(tmp_path / "state" / "bemine.db")
    rows = [
        ("proj_aux", "aux", tmp_path / "tmp" / "aux", 40.0),
        ("proj_bm", "bm", tmp_path / "bm", 30.0),
        ("proj_web_a", "web", tmp_path / "clients" / "alpha" / "web", 20.0),
        ("proj_web_b", "web", tmp_path / "clients" / "beta" / "web", 10.0),
        ("proj_nep", "neptts_bench", tmp_path / "neptts-bench", 5.0),
    ]
    for _, _, cwd, _ in rows:
        cwd.mkdir(parents=True)
    with db.connect() as conn:
        conn.executemany(
            """
            INSERT INTO projects(project_id, name, cwd, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, 1.0, ?)
            """,
            [
                (project_id, name, str(cwd), last_seen)
                for project_id, name, cwd, last_seen in rows
            ],
        )

    channels = project_channels_from_db(db)
    names = [channel.channel_name for channel in channels]

    assert len(names) == len(set(names))
    assert "web-clients-alpha" in names
    assert "web-clients-beta" in names
    assert "neptts-bench" in names
    assert "aux" in names
    assert {channel.runtime for channel in channels} == {"kimi"}


class _FakeKimiHandler:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def ask(
        self,
        prompt: str,
        *,
        system: str = "",
        max_tokens: int = 512,
        timeout: float = 60.0,
    ) -> KimiMessageResult:
        self.calls.append(
            {
                "prompt": prompt,
                "system": system,
                "max_tokens": max_tokens,
                "timeout": timeout,
            }
        )
        return KimiMessageResult(
            text="kimi finished without sk-kimi-abcdefghijklmnopqrstuvwxyz",
            model="kimi-k2.6",
            provider="kimi-coding",
            api_mode="anthropic_messages",
            finish_reason="end_turn",
            input_tokens=11,
            output_tokens=13,
            total_tokens=24,
        )


def test_kimi_dispatch_records_usage_and_audit(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    project_root = tmp_path / "repo"
    note_path = tmp_path / "inbox" / "notes.txt"
    project_root.mkdir()
    note_path.parent.mkdir()
    note_path.write_text("note", encoding="utf-8")
    settings = BemineSettings.load(
        state_dir=state_dir,
        vault_dir=tmp_path / "vault",
        codex_home=tmp_path / ".codex",
        claude_home=tmp_path / ".claude",
        kimi_home=tmp_path / ".kimi",
        hermes_home=tmp_path / ".hermes",
        device_id="testbox",
    )
    handler = _FakeKimiHandler()
    dispatcher = KimiDispatcher(
        settings, handler=handler, timeout_seconds=30, max_tokens=256
    )

    result = dispatcher.dispatch(
        ProjectChannel(
            channel_name="bemine",
            cwd=project_root,
            project_id="proj_bm",
            project_name="bm",
            runtime="kimi",
        ),
        DispatchRequest(
            channel_id="channel-1",
            channel_name="bemine",
            message_id="message-1",
            author_name="voidash",
            content="please inspect this",
            attachments=[note_path],
        ),
    )

    assert result.ok is True
    assert "sk-kimi-abcdefghijklmnopqrstuvwxyz" not in result.output
    assert "<token>" in result.output
    assert handler.calls[0]["max_tokens"] == 256
    assert handler.calls[0]["timeout"] == 30.0
    assert "You are Bemine's Kimi worker" in str(handler.calls[0]["prompt"])
    assert str(note_path) in str(handler.calls[0]["prompt"])
    assert "API dispatch" in str(handler.calls[0]["prompt"])
    assert "Bemine harness context:" in str(handler.calls[0]["prompt"])
    assert "Credential and infrastructure rules:" in str(handler.calls[0]["prompt"])

    db = BemineDB(settings.db_path)
    with db.connect() as conn:
        events = [
            dict(row) for row in conn.execute("SELECT * FROM audit_events ORDER BY id")
        ]
        usage = dict(conn.execute("SELECT * FROM usage_samples").fetchone())
    assert [row["event_type"] for row in events] == [
        "kimi_dispatch_started",
        "kimi_dispatch_finished",
    ]
    payload = json.loads(events[-1]["payload_json"])
    assert payload["ok"] is True
    assert payload["runtime"] == "kimi"
    assert payload["model"] == "kimi-k2.6"
    assert usage["provider"] == "kimi"
    assert usage["runtime"] == "kimi"
    assert usage["input_tokens"] == 11
    assert usage["output_tokens"] == 13
    assert usage["total_tokens"] == 24


class _FakeDispatcher:
    def __init__(self, output: str) -> None:
        self.output = output
        self.calls: list[tuple[ProjectChannel, DispatchRequest]] = []

    def dispatch(
        self,
        project: ProjectChannel,
        request: DispatchRequest,
        progress_callback=None,
    ) -> DispatchResult:
        del progress_callback
        self.calls.append((project, request))
        return DispatchResult(
            ok=True, project=project, output=self.output, elapsed_seconds=0.01
        )


def test_project_dispatcher_routes_by_project_runtime(tmp_path: Path) -> None:
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        codex_home=tmp_path / ".codex",
        claude_home=tmp_path / ".claude",
        kimi_home=tmp_path / ".kimi",
        hermes_home=tmp_path / ".hermes",
        device_id="testbox",
    )
    codex = _FakeDispatcher("codex-ok")
    kimi = _FakeDispatcher("kimi-ok")
    dispatcher = ProjectDispatcher(
        settings, codex_dispatcher=codex, kimi_dispatcher=kimi
    )
    request = DispatchRequest(
        channel_id="channel-1",
        channel_name="bemine",
        message_id="message-1",
        author_name="voidash",
        content="hello",
        attachments=[],
    )

    kimi_result = dispatcher.dispatch(
        ProjectChannel(channel_name="bemine", cwd=tmp_path), request
    )
    codex_result = dispatcher.dispatch(
        ProjectChannel(channel_name="bemine", cwd=tmp_path, runtime="codex"),
        request,
    )

    assert kimi_result.output == "kimi-ok"
    assert codex_result.output == "codex-ok"
    assert len(kimi.calls) == 1
    assert len(codex.calls) == 1


def test_dispatch_status_makes_runtime_and_model_explicit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("BEMINE_DISPATCH_RUNTIME", "codex")
    monkeypatch.setenv("BEMINE_CODEX_MODEL", "gpt-test-codex")
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        codex_home=tmp_path / ".codex",
        claude_home=tmp_path / ".claude",
        kimi_home=tmp_path / ".kimi",
        hermes_home=tmp_path / ".hermes",
        device_id="testbox",
    )

    payload = dispatch_status_payload(settings)
    text = render_dispatch_status(payload)

    assert payload["runtime"] == "codex"
    assert payload["model"] == "gpt-test-codex"
    assert payload["model_source"] == "BEMINE_CODEX_MODEL"
    assert (
        payload["tool_calls"] == "available through codex exec --json progress events"
    )
    assert "- runtime: codex" in text
    assert "- model: gpt-test-codex" in text


def test_codex_progress_from_jsonl_command_events() -> None:
    started = codex_progress_from_event(
        {
            "type": "item.started",
            "item": {
                "id": "item_0",
                "type": "command_execution",
                "command": "/bin/bash -lc 'echo sk-kimi-abcdefghijklmnopqrstuvwxyz'",
                "status": "in_progress",
            },
        }
    )
    completed = codex_progress_from_event(
        {
            "type": "item.completed",
            "item": {
                "id": "item_0",
                "type": "command_execution",
                "command": "/bin/bash -lc pwd",
                "exit_code": 0,
                "status": "completed",
            },
        }
    )

    assert started is not None
    assert started.runtime == "codex"
    assert started.event_type == "item.started"
    assert "shell:" in started.summary
    assert "sk-kimi-abcdefghijklmnopqrstuvwxyz" not in started.summary
    assert "<token>" in started.summary
    assert completed is not None
    assert completed.summary == "✅ shell done: `/bin/bash -lc pwd`"


def test_codex_dispatch_parses_jsonl_progress_usage_and_audit(tmp_path: Path) -> None:
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        codex_home=tmp_path / ".codex",
        claude_home=tmp_path / ".claude",
        kimi_home=tmp_path / ".kimi",
        hermes_home=tmp_path / ".hermes",
        device_id="testbox",
    )
    project_root = tmp_path / "repo"
    project_root.mkdir()

    def fake_runner(
        command: list[str],
        *,
        input: str,
        text: bool,
        stdout: int,
        stderr: int,
        timeout: int,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        del input, text, stdout, stderr, timeout, check
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text("codex final", encoding="utf-8")
        stdout_jsonl = "\n".join(
            [
                json.dumps(
                    {
                        "type": "item.started",
                        "item": {
                            "id": "item_0",
                            "type": "command_execution",
                            "command": "/bin/bash -lc pwd",
                            "status": "in_progress",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "item_0",
                            "type": "command_execution",
                            "command": "/bin/bash -lc pwd",
                            "exit_code": 0,
                            "status": "completed",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 10,
                            "cached_input_tokens": 4,
                            "output_tokens": 3,
                            "reasoning_output_tokens": 1,
                        },
                    }
                ),
            ]
        )
        return subprocess.CompletedProcess(command, 0, stdout_jsonl, "")

    progress: list[str] = []
    result = CodexDispatcher(settings, runner=fake_runner, codex_bin="codex").dispatch(
        ProjectChannel(channel_name="bemine", cwd=project_root, runtime="codex"),
        DispatchRequest(
            channel_id="channel-1",
            channel_name="bemine",
            message_id="message-1",
            author_name="voidash",
            content="run pwd",
            attachments=[],
        ),
        lambda event: progress.append(event.summary),
    )

    assert result.ok is True
    assert result.output == "codex final"
    assert progress == [
        "💻 shell: `/bin/bash -lc pwd`",
        "✅ shell done: `/bin/bash -lc pwd`",
    ]
    db = BemineDB(settings.db_path)
    with db.connect() as conn:
        event_types = [
            row["event_type"]
            for row in conn.execute("SELECT event_type FROM audit_events ORDER BY id")
        ]
        usage = dict(conn.execute("SELECT * FROM usage_samples").fetchone())
    assert event_types == [
        "codex_dispatch_started",
        "codex_tool_event",
        "codex_tool_event",
        "codex_usage_recorded",
        "codex_dispatch_finished",
    ]
    assert usage["provider"] == "codex"
    assert usage["runtime"] == "codex"
    assert usage["input_tokens"] == 10
    assert usage["output_tokens"] == 3
    assert usage["total_tokens"] == 13


def test_codex_dispatch_runs_in_project_root_with_images_and_audit(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    project_root = tmp_path / "repo"
    image_path = tmp_path / "inbox" / "diagram.png"
    note_path = tmp_path / "inbox" / "notes.txt"
    project_root.mkdir()
    image_path.parent.mkdir()
    image_path.write_bytes(b"fake-png")
    note_path.write_text("note", encoding="utf-8")
    settings = BemineSettings.load(
        state_dir=state_dir,
        vault_dir=tmp_path / "vault",
        codex_home=tmp_path / ".codex",
        claude_home=tmp_path / ".claude",
        kimi_home=tmp_path / ".kimi",
        hermes_home=tmp_path / ".hermes",
        device_id="testbox",
    )
    seen: dict[str, object] = {}

    def fake_runner(
        command: list[str],
        *,
        input: str,
        text: bool,
        stdout: int,
        stderr: int,
        timeout: int,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        seen["command"] = command
        seen["input"] = input
        seen["text"] = text
        seen["stdout"] = stdout
        seen["stderr"] = stderr
        seen["timeout"] = timeout
        seen["check"] = check
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(
            "finished without ghp_abcdefghijklmnopqrstuvwxyz", encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 0, "stdout fallback", "")

    dispatcher = CodexDispatcher(
        settings,
        runner=fake_runner,
        timeout_seconds=42,
        codex_bin="codex",
    )
    result = dispatcher.dispatch(
        ProjectChannel(
            channel_name="bemine",
            cwd=project_root,
            project_id="proj_bm",
            project_name="bm",
        ),
        DispatchRequest(
            channel_id="channel-1",
            channel_name="bemine",
            message_id="message-1",
            author_name="voidash",
            content="please inspect this",
            attachments=[image_path, note_path],
        ),
    )

    command = seen["command"]
    assert result.ok is True
    assert "ghp_abcdefghijklmnopqrstuvwxyz" not in result.output
    assert "<token>" in result.output
    assert command[:2] == ["codex", "exec"]
    assert "--json" in command
    assert command[command.index("--cd") + 1] == str(project_root)
    assert command[command.index("--sandbox") + 1] == "danger-full-access"
    assert "--ask-for-approval" not in command
    assert str(image_path) in command
    assert str(note_path) not in command
    assert "Project channel: #bemine" in str(seen["input"])
    assert str(note_path) in str(seen["input"])
    assert "Bemine harness context:" in str(seen["input"])
    assert "Bemine Soul" in str(seen["input"])
    assert "Shared operator context:" in str(seen["input"])
    assert "Credential and infrastructure rules:" in str(seen["input"])
    assert seen["timeout"] == 42
    assert seen["check"] is False

    db = BemineDB(settings.db_path)
    with db.connect() as conn:
        rows = [
            dict(row) for row in conn.execute("SELECT * FROM audit_events ORDER BY id")
        ]
    assert [row["event_type"] for row in rows] == [
        "codex_dispatch_started",
        "codex_dispatch_finished",
    ]
    payload = json.loads(rows[-1]["payload_json"])
    assert payload["ok"] is True
    assert payload["project_channel"] == "bemine"
    assert payload["elapsed_seconds"] >= 0


def test_codex_dispatch_timeout_is_audited(tmp_path: Path) -> None:
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        codex_home=tmp_path / ".codex",
        claude_home=tmp_path / ".claude",
        kimi_home=tmp_path / ".kimi",
        hermes_home=tmp_path / ".hermes",
        device_id="testbox",
    )
    project_root = tmp_path / "repo"
    project_root.mkdir()

    def timeout_runner(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=1)

    result = CodexDispatcher(
        settings, runner=timeout_runner, timeout_seconds=1
    ).dispatch(
        ProjectChannel(channel_name="bemine", cwd=project_root),
        DispatchRequest(
            channel_id="channel-1",
            channel_name="bemine",
            message_id="message-1",
            author_name="voidash",
            content="hang",
            attachments=[],
        ),
    )

    assert result.ok is False
    assert result.timed_out is True
    assert "timed out" in result.output
    with BemineDB(settings.db_path).connect() as conn:
        payload = json.loads(
            conn.execute(
                "SELECT payload_json FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()["payload_json"]
        )
    assert payload["timed_out"] is True


def test_codex_dispatch_missing_binary_returns_discord_visible_failure(
    tmp_path: Path,
) -> None:
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        codex_home=tmp_path / ".codex",
        claude_home=tmp_path / ".claude",
        kimi_home=tmp_path / ".kimi",
        hermes_home=tmp_path / ".hermes",
        device_id="testbox",
    )
    project_root = tmp_path / "repo"
    project_root.mkdir()

    def missing_runner(*args, **kwargs):
        raise FileNotFoundError("codex")

    result = CodexDispatcher(
        settings,
        runner=missing_runner,
        codex_bin="/missing/codex",
    ).dispatch(
        ProjectChannel(channel_name="bemine", cwd=project_root),
        DispatchRequest(
            channel_id="channel-1",
            channel_name="bemine",
            message_id="message-1",
            author_name="voidash",
            content="hello",
            attachments=[],
        ),
    )

    assert result.ok is False
    assert "codex` binary was not found" in result.output
    with BemineDB(settings.db_path).connect() as conn:
        payload = json.loads(
            conn.execute(
                "SELECT payload_json FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()["payload_json"]
        )
    assert payload["ok"] is False
    assert payload["codex_bin"] == "/missing/codex"


def test_resolve_codex_bin_uses_explicit_env(monkeypatch) -> None:
    monkeypatch.setenv("BEMINE_CODEX_BIN", "/opt/codex")

    assert resolve_codex_bin() == "/opt/codex"


def test_resolve_codex_sandbox_defaults_to_systemd_safe_mode(monkeypatch) -> None:
    monkeypatch.delenv("BEMINE_CODEX_SANDBOX", raising=False)

    assert resolve_codex_sandbox() == "danger-full-access"


def test_resolve_codex_sandbox_can_be_overridden(monkeypatch) -> None:
    monkeypatch.setenv("BEMINE_CODEX_SANDBOX", "workspace-write")

    assert resolve_codex_sandbox() == "workspace-write"
