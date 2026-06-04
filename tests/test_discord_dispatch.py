from __future__ import annotations

import json
import subprocess
from pathlib import Path

from bemine.config import BemineSettings
from bemine.db import BemineDB
from mayor_discord.dispatch import (
    PROJECT_CATEGORY_NAME,
    CodexDispatcher,
    DispatchRequest,
    ProjectChannel,
    infer_project_route,
    load_project_channels,
    project_channels_from_db,
    resolve_project_channel,
    write_project_channel_config,
)


def test_project_channel_config_resolves_only_inside_project_category(tmp_path: Path) -> None:
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


def test_general_channel_routes_by_content_mentions_and_mayor_fallback(tmp_path: Path) -> None:
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


def test_project_channels_from_db_make_per_project_names_unique(tmp_path: Path) -> None:
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
            [(project_id, name, str(cwd), last_seen) for project_id, name, cwd, last_seen in rows],
        )

    channels = project_channels_from_db(db)
    names = [channel.channel_name for channel in channels]

    assert len(names) == len(set(names))
    assert "web-clients-alpha" in names
    assert "web-clients-beta" in names
    assert "neptts-bench" in names
    assert "aux" in names


def test_codex_dispatch_runs_in_project_root_with_images_and_audit(tmp_path: Path) -> None:
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
        output_path.write_text("finished without ghp_abcdefghijklmnopqrstuvwxyz", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "stdout fallback", "")

    dispatcher = CodexDispatcher(settings, runner=fake_runner, timeout_seconds=42)
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
    assert command[command.index("--cd") + 1] == str(project_root)
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert command[command.index("--ask-for-approval") + 1] == "never"
    assert str(image_path) in command
    assert str(note_path) not in command
    assert "Project channel: #bemine" in str(seen["input"])
    assert str(note_path) in str(seen["input"])
    assert seen["timeout"] == 42
    assert seen["check"] is False

    db = BemineDB(settings.db_path)
    with db.connect() as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM audit_events ORDER BY id")]
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

    result = CodexDispatcher(settings, runner=timeout_runner, timeout_seconds=1).dispatch(
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
