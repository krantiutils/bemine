from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.workers.native_runtime import _sanitize_message_history
from bemine.context.tool_registry import BemineToolRegistry
from bemine.discord.dispatch import (
    PROJECT_CATEGORY_NAME,
    CodexDispatcher,
    DispatchRequest,
    DispatchResult,
    KimiDispatcher,
    KimiNativeDispatcher,
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
    resolve_kimi_bin,
    resolve_meridian_base_url,
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
    assert loaded["tts"].runtime == ""
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
    assert {channel.runtime for channel in channels} == {""}


def test_kimi_dispatch_uses_cli_with_generated_config_and_audit(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "bemine.toml"
    config_path.write_text(
        """
[kimi]
provider = "kimi-coding"
model = "kimi-k2.6"
base_url = "https://api.kimi.com/coding"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("BEMINE_CONFIG", str(config_path))
    monkeypatch.setenv("KIMI_API_KEY", "sk-kimi-abcdefghijklmnopqrstuvwxyz")
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
    seen: dict[str, object] = {}

    def fake_runner(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="kimi finished without sk-kimi-abcdefghijklmnopqrstuvwxyz",
            stderr="",
        )

    dispatcher = KimiDispatcher(
        settings,
        runner=fake_runner,
        timeout_seconds=30,
        kimi_bin="kimi",
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
            conversation_context="- voidash: previous context",
        ),
    )

    assert result.ok is True
    assert "sk-kimi-abcdefghijklmnopqrstuvwxyz" not in result.output
    assert "<token>" in result.output
    command = seen["command"]
    assert command[:2] == ["kimi", "--work-dir"]
    assert str(project_root) in command
    assert "--config-file" in command
    assert "--yolo" in command
    assert "--print" in command
    assert "--prompt" in command
    prompt = command[command.index("--prompt") + 1]
    assert "You are Bemine's Kimi worker" in prompt
    assert "Recent channel context:" in prompt
    assert "previous context" in prompt
    assert str(note_path) in prompt
    assert "You may inspect and edit files using Kimi CLI tools" in prompt
    kwargs = seen["kwargs"]
    assert kwargs["timeout"] == 30
    assert kwargs["check"] is False
    assert kwargs["env"]["KIMI_API_KEY"] == "sk-kimi-abcdefghijklmnopqrstuvwxyz"
    cli_config = state_dir / "runtime" / "kimi-config.toml"
    assert cli_config.exists()
    rendered = cli_config.read_text(encoding="utf-8")
    assert 'default_model = "bemine"' in rendered
    assert 'model = "kimi-k2.6"' in rendered
    assert "sk-kimi-abcdefghijklmnopqrstuvwxyz" not in rendered
    assert 'api_key = ""' in rendered

    db = BemineDB(settings.db_path)
    with db.connect() as conn:
        events = [
            dict(row) for row in conn.execute("SELECT * FROM audit_events ORDER BY id")
        ]
    assert [row["event_type"] for row in events] == [
        "kimi_dispatch_started",
        "kimi_dispatch_finished",
    ]
    payload = json.loads(events[-1]["payload_json"])
    assert payload["ok"] is True
    assert payload["runtime"] == "kimi"
    assert payload["model"] == "kimi-k2.6"


def test_kimi_native_dispatch_runs_bemine_tools_and_records_session(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "bemine.toml"
    config_path.write_text(
        """
[kimi]
provider = "kimi-coding"
model = "kimi-k2.6"
base_url = "https://api.kimi.com/coding"
api_mode = "anthropic_messages"
dispatch_max_tokens = 800
session_message_window = 12
tool_max_turns = 4
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("BEMINE_CONFIG", str(config_path))
    monkeypatch.setenv("KIMI_API_KEY", "sk-kimi-abcdefghijklmnopqrstuvwxyz")
    state_dir = tmp_path / "state"
    project_root = tmp_path / "repo"
    project_root.mkdir()
    settings = BemineSettings.load(
        state_dir=state_dir,
        vault_dir=tmp_path / "vault",
        codex_home=tmp_path / ".codex",
        claude_home=tmp_path / ".claude",
        kimi_home=tmp_path / ".kimi",
        hermes_home=tmp_path / ".hermes",
        device_id="testbox",
    )
    requests: list[dict[str, object]] = []

    def fake_transport(url, headers, payload, timeout):
        requests.append(
            {
                "url": url,
                "headers": dict(headers),
                "payload": dict(payload),
                "timeout": timeout,
            }
        )
        if len(requests) == 1:
            return {
                "model": "kimi-k2.6",
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "content": [
                    {"type": "text", "text": "Checking the project."},
                    {
                        "type": "tool_use",
                        "id": "tool_1",
                        "name": "shell_run",
                        "input": {"command": "printf native-ok", "timeout": 5},
                    },
                ],
            }
        return {
            "model": "kimi-k2.6",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 12, "output_tokens": 7},
            "content": [{"type": "text", "text": "Done: native-ok"}],
        }

    progress = []
    dispatcher = KimiNativeDispatcher(
        settings,
        transport=fake_transport,
        timeout_seconds=30,
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
            content="run a tiny smoke",
            attachments=[],
            conversation_context="- voidash: previous context",
        ),
        progress_callback=progress.append,
    )

    assert result.ok is True
    assert result.output == "Done: native-ok"
    assert len(requests) == 2
    first_payload = requests[0]["payload"]
    assert isinstance(first_payload, dict)
    assert first_payload["model"] == "kimi-k2.6"
    assert "tools" in first_payload
    assert first_payload["messages"][-1]["role"] == "user"
    assert "previous context" in first_payload["messages"][-1]["content"]
    second_payload = requests[1]["payload"]
    assert isinstance(second_payload, dict)
    assert second_payload["messages"][-1]["content"][0]["type"] == "tool_result"
    assert "native-ok" in second_payload["messages"][-1]["content"][0]["content"]
    assert [item.event_type for item in progress] == [
        "tool.started",
        "tool.completed",
    ]
    assert progress[0].summary == "💻 shell: `printf native-ok`"
    assert progress[1].summary == (
        "✅ shell done: `printf native-ok`\nstdout: `native-ok`"
    )

    db = BemineDB(settings.db_path)
    with db.connect() as conn:
        session = conn.execute("SELECT * FROM bemine_sessions").fetchone()
        messages = conn.execute(
            "SELECT role, content_json FROM bemine_session_messages ORDER BY id"
        ).fetchall()
        tool_call = conn.execute("SELECT * FROM tool_calls").fetchone()
        usage_count = conn.execute("SELECT COUNT(*) FROM usage_samples").fetchone()[0]
        events = [
            dict(row) for row in conn.execute("SELECT * FROM audit_events ORDER BY id")
        ]

    assert session["session_id"] == "discord:channel-1:bemine:kimi"
    assert session["message_count"] == 4
    assert [row["role"] for row in messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert tool_call["tool_name"] == "shell_run"
    assert tool_call["status"] == "success"
    assert "native-ok" in tool_call["result_json"]
    assert usage_count == 2
    assert [row["event_type"] for row in events] == [
        "kimi_native_dispatch_started",
        "kimi_tool_call_finished",
        "kimi_native_dispatch_finished",
    ]


def test_kimi_native_rejected_tool_followup_does_not_store_pending_history(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "bemine.toml"
    config_path.write_text(
        """
[kimi]
provider = "kimi-coding"
model = "kimi-k2.6"
base_url = "https://api.kimi.com/coding"
api_mode = "anthropic_messages"
dispatch_max_tokens = 800
session_message_window = 12
tool_max_turns = 4
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("BEMINE_CONFIG", str(config_path))
    monkeypatch.setenv("KIMI_API_KEY", "sk-kimi-abcdefghijklmnopqrstuvwxyz")
    project_root = tmp_path / "repo"
    project_root.mkdir()
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        codex_home=tmp_path / ".codex",
        claude_home=tmp_path / ".claude",
        kimi_home=tmp_path / ".kimi",
        hermes_home=tmp_path / ".hermes",
        device_id="testbox",
    )
    requests: list[dict[str, object]] = []

    def fake_transport(url, headers, payload, timeout):
        requests.append(
            {
                "url": url,
                "headers": dict(headers),
                "payload": dict(payload),
                "timeout": timeout,
            }
        )
        if len(requests) == 1:
            return {
                "model": "kimi-k2.6",
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool_1",
                        "name": "shell_run",
                        "input": {"command": "printf native-ok", "timeout": 5},
                    },
                ],
            }
        raise RuntimeError(
            'Kimi request failed 400: {"error":{"message":"tool_call_id  is not found"}}'
        )

    dispatcher = KimiNativeDispatcher(
        settings,
        transport=fake_transport,
        timeout_seconds=30,
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
            content="run a tiny smoke",
            attachments=[],
        ),
    )

    assert result.ok is False
    assert "tool_call_id  is not found" in result.output
    assert len(requests) == 2

    db = BemineDB(settings.db_path)
    with db.connect() as conn:
        messages = conn.execute(
            "SELECT role, content_json FROM bemine_session_messages ORDER BY id"
        ).fetchall()

    assert [row["role"] for row in messages] == ["user"]
    assert "tool_result" not in messages[0]["content_json"]


def test_kimi_native_history_sanitizer_drops_unaccepted_tool_exchange() -> None:
    messages = [
        {"role": "user", "content": "first request"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "tool_1",
                    "name": "shell_run",
                    "input": {"command": "pwd"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool_1",
                    "content": '{"ok": true}',
                    "is_error": False,
                }
            ],
        },
        {"role": "user", "content": "test"},
    ]

    assert _sanitize_message_history(messages) == []


def test_kimi_native_history_sanitizer_drops_cascading_tool_exchanges() -> None:
    messages = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "tool_1",
                    "name": "shell_run",
                    "input": {"command": "pwd"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool_1",
                    "content": '{"ok": true}',
                    "is_error": False,
                }
            ],
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "tool_2",
                    "name": "shell_run",
                    "input": {"command": "date"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool_2",
                    "content": '{"ok": true}',
                    "is_error": False,
                }
            ],
        },
        {"role": "user", "content": "test"},
    ]

    assert _sanitize_message_history(messages) == []


def test_native_kimi_tool_names_are_api_safe() -> None:
    valid_name = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

    names = [tool["name"] for tool in BemineToolRegistry().schemas()]

    # Core tools must be present; the registry may add more (e.g. state tools).
    assert {
        "status_show", "audit_list", "shell_run", "file_read", "file_search",
        "bemine_config", "jobs_list", "usage_report",
    } <= set(names)
    assert all(valid_name.match(name) for name in names)


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


def test_project_dispatcher_routes_by_config_runtime_and_explicit_override(
    tmp_path: Path, monkeypatch
) -> None:
    config = tmp_path / "bemine.toml"
    config.write_text('[dispatch]\nruntime = "kimi"\n', encoding="utf-8")
    monkeypatch.setenv("BEMINE_CONFIG", str(config))
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

    config_result = dispatcher.dispatch(
        ProjectChannel(channel_name="bemine", cwd=tmp_path), request
    )
    codex_result = dispatcher.dispatch(
        ProjectChannel(channel_name="bemine", cwd=tmp_path, runtime="codex"),
        request,
    )

    assert config_result.output == "kimi-ok"
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
    assert "- backend: exec" in text


def test_dispatch_status_reports_codex_app_server_backend(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = tmp_path / "bemine.toml"
    config.write_text(
        """
[dispatch]
runtime = "codex"

[codex]
backend = "app-server"
model = "gpt-5.3-codex-spark"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("BEMINE_CONFIG", str(config))
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
    assert payload["backend"] == "app-server"
    assert payload["tool_calls"] == "requires Codex app-server adapter"
    assert "- backend: app-server" in text


def test_dispatch_status_reports_claude_meridian_backend(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = tmp_path / "bemine.toml"
    config.write_text(
        """
[dispatch]
runtime = "claude"

[claude]
backend = "meridian"
model = "claude-opus-4-6"
base_url = "http://127.0.0.1:3456"
api_key_env = "MERIDIAN_API_KEY"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("BEMINE_CONFIG", str(config))
    monkeypatch.setenv("MERIDIAN_API_KEY", "secret")
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

    assert payload["runtime"] == "claude"
    assert payload["provider"] == "claude"
    assert payload["model"] == "claude-opus-4-6"
    assert payload["backend"] == "meridian"
    assert payload["base_url"] == "http://127.0.0.1:3456"
    assert payload["api_key_present"] is True
    assert payload["tool_calls"] == "requires Meridian Anthropic-messages adapter"
    assert resolve_meridian_base_url() == "http://127.0.0.1:3456"
    assert "- runtime: claude" in text
    assert "- backend: meridian" in text
    assert "- base_url: http://127.0.0.1:3456" in text


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
    assert command[command.index("--sandbox") + 1] == "workspace-write"
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


def test_resolve_kimi_bin_uses_config_file(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "bemine.toml"
    config.write_text(
        """
[kimi]
bin = "/opt/kimi/bin/kimi"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("BEMINE_CONFIG", str(config))
    monkeypatch.delenv("BEMINE_KIMI_BIN", raising=False)

    assert resolve_kimi_bin() == "/opt/kimi/bin/kimi"


def test_resolve_kimi_bin_falls_back_to_user_local_bin(
    tmp_path: Path, monkeypatch
) -> None:
    kimi = tmp_path / ".local" / "bin" / "kimi"
    kimi.parent.mkdir(parents=True)
    kimi.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.delenv("BEMINE_KIMI_BIN", raising=False)
    monkeypatch.setattr("bemine.discord.dispatch.config_string", lambda *_args: "")
    monkeypatch.setattr("bemine.discord.dispatch.shutil.which", lambda _name: None)
    monkeypatch.setattr("bemine.discord.dispatch.Path.home", lambda: tmp_path)

    assert resolve_kimi_bin() == str(kimi)


def test_resolve_codex_sandbox_defaults_to_workspace_write(monkeypatch) -> None:
    monkeypatch.delenv("BEMINE_CODEX_SANDBOX", raising=False)

    assert resolve_codex_sandbox() == "workspace-write"


def test_resolve_codex_sandbox_can_be_overridden(monkeypatch) -> None:
    monkeypatch.setenv("BEMINE_CODEX_SANDBOX", "workspace-write")

    assert resolve_codex_sandbox() == "workspace-write"
