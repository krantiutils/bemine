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


def test_project_channel_topic_describes_root_and_behavior(tmp_path):
    from bemine.discord.dispatch import ProjectChannel, _project_channel_topic

    channel = ProjectChannel(
        channel_name="repo",
        cwd=tmp_path / "repo",
        project_id="proj_x",
        project_name="repo",
    )

    topic = _project_channel_topic(channel)

    assert "Bemine project `repo`" in topic
    assert str(tmp_path / "repo") in topic
    assert len(topic) <= 1024


def test_plan_reports_would_archive_for_stale_channels(tmp_path, monkeypatch):
    from bemine.discord.dispatch import plan_project_channel_sync

    settings = BemineSettings.load(
        state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v", device_id="cdjk"
    )

    class _FakeStore:
        def __init__(self, db_path):
            pass

        def channel_metadata(self):
            return [
                {
                    "channel_name": "old-project",
                    "category_name": "Bemine Projects",
                    "channel_type": 0,
                    "channel_id": "123",
                    "guild_id": "g1",
                }
            ]

    monkeypatch.setattr("bemine.discord.dispatch.Store", _FakeStore)

    plan = plan_project_channel_sync(settings, archive_stale=True)

    assert [c["channel_name"] for c in plan["would_archive"]] == ["old-project"]
    assert plan["would_prune"] == []
    # prune wins over archive when both are requested
    plan2 = plan_project_channel_sync(settings, archive_stale=True, prune=True)
    assert plan2["would_archive"] == []
    assert [c["channel_name"] for c in plan2["would_prune"]] == ["old-project"]


def test_device_channel_layout_attributes_and_adds_remote_projects(tmp_path):
    from bemine.discord.dispatch import ProjectChannel, _device_channel_layout

    settings = BemineSettings.load(
        state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v", device_id="cdjk"
    )
    local = [
        ProjectChannel(
            channel_name="bemine",
            cwd=Path("/home/cdjk/bm"),
            project_id="p1",
            project_name="bemine",
        ),
        ProjectChannel(
            channel_name="nanoclaw",
            cwd=Path("/home/cdjk/nanoclaw"),
            project_id="p2",
            project_name="nanoclaw",
        ),
    ]
    attribution = {
        "/home/cdjk/bm": {
            "cwd": "/home/cdjk/bm",
            "device": "cdjk",
            "age": 10,
            "name": "bm",
        },
        "/Users/cdjk/g2p": {
            "cwd": "/Users/cdjk/g2p",
            "device": "mbp",
            "age": 100,
            "name": "g2p",
        },
        "/Users/cdjk/himalMT": {
            "cwd": "/Users/cdjk/himalMT",
            "device": "mbp2",
            "age": 50,
            "name": "himalmt",
        },
        # name collision with a local channel -> remote skipped
        "/Users/cdjk/nanoclaw": {
            "cwd": "/Users/cdjk/nanoclaw",
            "device": "mbp",
            "age": 5,
            "name": "nanoclaw",
        },
        # too old -> excluded
        "/Users/cdjk/ancient": {
            "cwd": "/Users/cdjk/ancient",
            "device": "mbp",
            "age": 90 * 86400,
            "name": "ancient",
        },
    }

    device_for, remote = _device_channel_layout(settings, local, {}, attribution)

    assert device_for["bemine"] == "cdjk"
    assert device_for["nanoclaw"] == "cdjk"  # no fleet hit for its cwd -> local
    assert device_for["g2p"] == "mbp"
    assert device_for["himalmt"] == "mbp2"
    names = {str(info["name"]) for info in remote}
    assert names == {"g2p", "himalmt"}


def test_fleet_channel_candidates_filters_and_picks_freshest(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from bemine.discord.dispatch import _fleet_channel_candidates

    settings = BemineSettings.load(
        state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v", device_id="cdjk"
    )

    def fake_fleet(s, **kwargs):
        mk = lambda dev, cwd, age: SimpleNamespace(
            device_id=dev, cwd=cwd, age_seconds=age
        )
        return [
            mk("local", "/home/cdjk/bm", 500),
            mk("local", "/home/cdjk/bm", 100),  # fresher duplicate wins
            mk("mbp", "/Users/cdjk/Downloads", 10),  # excluded name
            mk("mbp2", "/Users/cdjk/himalMT", 60),
        ], []

    monkeypatch.setattr("bemine.index.status.collect_fleet_status", fake_fleet)

    best = _fleet_channel_candidates(settings)

    assert best["/home/cdjk/bm"]["age"] == 100
    assert best["/home/cdjk/bm"]["device"] == "cdjk"
    assert "/Users/cdjk/Downloads" not in best
    assert best["/Users/cdjk/himalMT"]["device"] == "mbp2"
