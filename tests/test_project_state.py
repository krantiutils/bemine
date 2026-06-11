from __future__ import annotations

from pathlib import Path

from bemine.config import BemineSettings
from bemine.context.project_state import project_state_context
from bemine.db import BemineDB
from bemine.index.models import SessionRecord
from bemine.util import project_id_from_cwd


def _settings(tmp_path):
    return BemineSettings.load(
        state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "vault", device_id="cdjk"
    )


def test_project_state_reports_sessions_handoff_workers_watcher(tmp_path):
    settings = _settings(tmp_path)
    db = BemineDB(settings.db_path)
    project = tmp_path / "repo"
    project.mkdir()
    cwd = str(project.resolve())
    project_id = project_id_from_cwd(cwd)

    db.upsert_device("cdjk")
    db.upsert_session(
        SessionRecord(
            session_id="claude:11111111-1111-4111-8111-111111111111",
            runtime="claude",
            device_id="cdjk",
            cwd=cwd,
            source_path=project / "t.jsonl",
            started_at="2026-06-12T08:00:00+00:00",
            updated_at="2026-06-12T08:30:00+00:00",
            model="",
            title="fix tokenizer crash",
            summary="user: fix tokenizer | assistant: patched vocab loading",
            message_count=4,
            tool_count=2,
        )
    )
    handoffs = settings.vault_dir / "Handoffs"
    handoffs.mkdir(parents=True)
    (handoffs / f"2026-06-12-010101-{project.name}.md").write_text(
        "# Handoff\n\n## Summary\n\nShipped tokenizer fix.\n\n"
        "## Next Steps\n\n- benchmark v2\n",
        encoding="utf-8",
    )
    db.upsert_managed_session(
        name="goal-repo",
        runtime="codex",
        transport="tmux",
        device_id="cdjk",
        project_id=project_id,
        cwd=cwd,
        command="codex --yolo",
        status="running",
    )
    db.upsert_project_watcher(
        project_id=project_id,
        channel_id="chan-1",
        brief="ship the tokenizer benchmark",
        interval_secs=600,
    )

    text = project_state_context(settings, project_id=project_id, cwd=project)

    assert "patched vocab loading" in text
    assert "Shipped tokenizer fix." in text
    assert "benchmark v2" in text
    assert "goal-repo [codex] running" in text
    assert "ship the tokenizer benchmark" in text


def test_project_state_explicit_about_empty_state(tmp_path):
    settings = _settings(tmp_path)
    project = tmp_path / "repo"
    project.mkdir()

    text = project_state_context(settings, project_id="proj_none", cwd=project)

    assert "Recent sessions: none indexed" in text
    assert "Latest handoff: none recorded." in text
    assert "Live managed workers: none." in text
    assert "Active watcher: none." in text


def test_channel_system_prompt_includes_project_state(tmp_path, monkeypatch):
    from bemine.discord.dispatch import ProjectChannel, _build_channel_system_prompt

    settings = _settings(tmp_path)
    project = tmp_path / "repo"
    project.mkdir()
    monkeypatch.setattr(
        "bemine.context.project_state.project_state_context",
        lambda s, *, project_id, cwd, db=None: "Recent sessions: STATE-MARKER",
    )
    channel = ProjectChannel(
        channel_name="repo",
        cwd=project,
        project_id="proj_x",
        project_name="repo",
    )

    prompt = _build_channel_system_prompt(
        settings, channel, runtime="kimi", model="kimi-k2.6", local_project=True
    )

    assert "Current project state:" in prompt
    assert "STATE-MARKER" in prompt
