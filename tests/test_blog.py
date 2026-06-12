from __future__ import annotations

import json
from pathlib import Path

import pytest

from bemine.blog import (
    build_blog_site,
    publish_blog_post,
    render_blog_posts,
    request_blog_changes,
    submit_blog_draft,
    validate_blog_draft,
)
from bemine.cli import main
from bemine.config import BemineSettings
from bemine.db import BemineDB

GOOD_DRAFT = (
    "We measured NepaliMOS against human ratings on neptts-bench.\n\n"
    "The benchmark correlates 0.84 with native-listener MOS across 6 TTS "
    "systems; the harness, data, and scoring code are at "
    "https://github.com/Ampixa/neptts-bench. One detail that proves the work "
    "is real: the evaluation set includes code-switched and bad-microphone "
    "audio on purpose, because that is what Nepali speech actually sounds "
    "like in the field. We released the full per-system score table."
)


def _settings(tmp_path, monkeypatch):
    monkeypatch.setattr("bemine.blog.blog_site_dir", lambda: tmp_path / "site")
    return BemineSettings.load(
        state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "vault", device_id="cdjk"
    )


def _submit(settings, tmp_path, db, **kwargs):
    draft = tmp_path / "draft.md"
    draft.write_text(GOOD_DRAFT, encoding="utf-8")
    return submit_blog_draft(
        settings,
        file=draft,
        title=kwargs.pop("title", "We measured NepaliMOS"),
        project="neptts-bench",
        author="claude-session",
        db=db,
        notify=False,
        **kwargs,
    )


def test_submit_registers_draft_and_runs_style_check(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    db = BemineDB(settings.db_path)

    result = _submit(settings, tmp_path, db)

    assert result["slug"] == "we-measured-nepalimos"
    assert result["problems"] == []
    row = db.get_blog_post("we-measured-nepalimos")
    assert row is not None and row["status"] == "draft"
    assert (Path(result["source_dir"]) / "post.md").is_file()
    assert "we-measured-nepalimos" in render_blog_posts(db.list_blog_posts())


def test_validate_flags_hype_and_missing_artifacts():
    problems = validate_blog_draft(
        "A revolutionary world-class TTS that changes everything for Nepal. " * 12,
        "Big claims",
    )

    assert any("artifact" in p for p in problems)
    assert any("revolutionary" in p for p in problems)


def test_publish_blocks_on_style_then_force_renders_and_skips_deploy(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path, monkeypatch)
    db = BemineDB(settings.db_path)
    bad = tmp_path / "bad.md"
    bad.write_text("short hype, no links", encoding="utf-8")
    submit_blog_draft(settings, file=bad, title="Bad post", db=db, notify=False)

    with pytest.raises(ValueError, match="style validation failed"):
        publish_blog_post(settings, "bad-post", db=db, deploy=False, notify=False)

    result = publish_blog_post(
        settings, "bad-post", db=db, deploy=False, notify=False, force=True
    )
    assert result["url"].endswith("/blog/bad-post/")
    assert (tmp_path / "site" / "blog" / "bad-post" / "index.html").is_file()


def test_publish_good_draft_builds_index_and_post(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    db = BemineDB(settings.db_path)
    _submit(settings, tmp_path, db)

    result = publish_blog_post(
        settings, "we-measured-nepalimos", db=db, deploy=False, notify=False
    )

    assert result["problems"] == []
    index = (tmp_path / "site" / "blog" / "index.html").read_text(encoding="utf-8")
    assert "/blog/we-measured-nepalimos/" in index
    post = (
        tmp_path / "site" / "blog" / "we-measured-nepalimos" / "index.html"
    ).read_text(encoding="utf-8")
    assert "We measured NepaliMOS" in post
    assert "neptts-bench" in post
    assert "#DC143C" in post  # crimson accent, ampixa style
    row = db.get_blog_post("we-measured-nepalimos")
    assert row["status"] == "published"


def test_request_changes_marks_needs_input(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    db = BemineDB(settings.db_path)
    _submit(settings, tmp_path, db)

    out = request_blog_changes(
        settings,
        "we-measured-nepalimos",
        "add the per-system score table",
        db=db,
        notify=False,
    )

    assert out["status"] == "needs_input"
    row = db.get_blog_post("we-measured-nepalimos")
    assert row["status"] == "needs_input"
    assert "score table" in row["notes"]


def test_empty_blog_builds_index_with_empty_state(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    db = BemineDB(settings.db_path)

    out = build_blog_site(settings, db=db)

    assert out["published"] == 0
    index = (tmp_path / "site" / "blog" / "index.html").read_text(encoding="utf-8")
    assert "Nothing published yet" in index


def test_cli_blog_submit_and_list(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("bemine.blog.blog_site_dir", lambda: tmp_path / "site")
    monkeypatch.setattr("bemine.blog._notify_discord", lambda s, t: False)
    draft = tmp_path / "d.md"
    draft.write_text(GOOD_DRAFT, encoding="utf-8")
    args = ["--state-dir", str(tmp_path / "s"), "--vault", str(tmp_path / "v")]

    main(
        [
            *args,
            "blog",
            "submit",
            str(draft),
            "--title",
            "CLI post",
            "--project",
            "bm",
            "--author",
            "tester",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["slug"] == "cli-post"

    main([*args, "blog", "list", "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["slug"] == "cli-post"


def test_registry_exposes_blog_commands(tmp_path):
    from bemine.commands import registry

    names = set(registry().keys())
    assert {"blog.list", "blog.show", "blog.publish", "blog.request_changes"} <= names


def test_gateway_routes_blog_phrases():
    from bemine.discord import gateway as gw

    assert gw._direct_blog_command("blog queue") == {"command": "blog.list"}
    assert gw._direct_blog_command("publish blog my-post") == {
        "command": "blog.publish",
        "slug": "my-post",
    }
    assert gw._direct_blog_command("show blog my-post") == {
        "command": "blog.show",
        "slug": "my-post",
    }
    assert gw._direct_blog_command("backlog status") is None


def test_submit_reads_stdin_for_remote_pipes(tmp_path, monkeypatch):
    import io

    settings = _settings(tmp_path, monkeypatch)
    db = BemineDB(settings.db_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(GOOD_DRAFT))

    result = submit_blog_draft(
        settings,
        file="-",
        title="Piped post",
        author="mbp-agent",
        db=db,
        notify=False,
    )

    assert result["slug"] == "piped-post"
    text = (Path(result["source_dir"]) / "post.md").read_text(encoding="utf-8")
    assert "NepaliMOS" in text
