from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bemine.blog import (
    build_blog_site,
    mirror_blog_to_git,
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


@pytest.fixture(autouse=True)
def _never_deploy(monkeypatch):
    """submit/revise auto-deploy previews; tests must NEVER rsync to prod —
    a test preview leaked to the live server once."""
    monkeypatch.setattr(
        "bemine.blog.deploy_blog_site", lambda settings: {"skipped": "test"}
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
    css = next((tmp_path / "site" / "blog" / "_static").glob("blog.*.css"))
    assert f"/blog/_static/{css.name}" in post
    assert "#DC143C" in css.read_text(encoding="utf-8")  # crimson accent, ampixa style
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


def test_build_renders_draft_previews_with_noindex(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    db = BemineDB(settings.db_path)
    _submit(settings, tmp_path, db)

    out = build_blog_site(settings, db=db)

    assert out["previews"] == 1
    preview = (
        tmp_path / "site" / "blog" / "preview" / "we-measured-nepalimos" / "index.html"
    ).read_text(encoding="utf-8")
    assert "DRAFT PREVIEW" in preview
    assert "noindex" in preview
    index = (tmp_path / "site" / "blog" / "index.html").read_text(encoding="utf-8")
    assert "preview" not in index  # drafts never linked from the public index


def test_request_changes_spawns_revision_worker(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    db = BemineDB(settings.db_path)
    _submit(settings, tmp_path, db)

    class _FakeController:
        def __init__(self):
            self.started = []
            self.sent = []

        def start(self, *, name, runtime, cwd, command=""):
            self.started.append((name, runtime))

        def send_when_ready(self, name, text, **kwargs):
            self.sent.append((name, text))

    controller = _FakeController()
    out = request_blog_changes(
        settings,
        "we-measured-nepalimos",
        "add the score table",
        db=db,
        notify=False,
        controller=controller,
    )

    assert out["reviser"].startswith("blog-revise-")
    assert controller.started[0][1] == "claude"
    brief = controller.sent[0][1]
    assert "add the score table" in brief
    assert "bemine blog revised we-measured-nepalimos" in brief
    assert "Do NOT publish" in brief


def test_mark_revised_returns_to_draft_and_rebuilds(tmp_path, monkeypatch):
    from bemine.blog import mark_blog_revised

    settings = _settings(tmp_path, monkeypatch)
    db = BemineDB(settings.db_path)
    _submit(settings, tmp_path, db)
    request_blog_changes(
        settings,
        "we-measured-nepalimos",
        "tighten the intro",
        db=db,
        notify=False,
        spawn_reviser=False,
    )

    out = mark_blog_revised(
        settings,
        "we-measured-nepalimos",
        "tightened intro",
        db=db,
        notify=False,
        deploy=False,
    )

    assert out["status"] == "draft"
    row = db.get_blog_post("we-measured-nepalimos")
    assert row["status"] == "draft"
    assert "revised: tightened intro" in row["notes"]
    assert (
        tmp_path / "site" / "blog" / "preview" / "we-measured-nepalimos" / "index.html"
    ).is_file()


def test_gateway_routes_request_changes_phrase():
    from bemine.discord import gateway as gw

    assert gw._direct_blog_command(
        "request changes on my-post: add the benchmark table please"
    ) == {
        "command": "blog.request_changes",
        "slug": "my-post",
        "note": "add the benchmark table please",
    }


def test_deploy_refuses_to_run_under_pytest(tmp_path, monkeypatch):
    # bypass the autouse stub to hit the real function: the env guard must trip
    import importlib

    import bemine.blog as blog_module

    real = importlib.reload(blog_module).deploy_blog_site
    settings = BemineSettings.load(
        state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v"
    )

    with pytest.raises(RuntimeError, match="inside a test run"):
        real(settings)


def test_preview_dashboard_lists_drafts_and_exists_when_empty(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    db = BemineDB(settings.db_path)

    build_blog_site(settings, db=db)
    dash = (tmp_path / "site" / "blog" / "preview" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "Queue is empty" in dash

    _submit(settings, tmp_path, db)
    build_blog_site(settings, db=db)
    dash = (tmp_path / "site" / "blog" / "preview" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "/blog/preview/we-measured-nepalimos/" in dash
    assert "style: clean" in dash
    assert "noindex" in dash


def test_preview_embeds_annotator_when_webhook_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "bemine.blog._cfg",
        lambda key, default: (
            "https://discord.com/api/webhooks/x/y"
            if key == "comment_webhook"
            else default
        ),
    )
    settings = _settings(tmp_path, monkeypatch)
    db = BemineDB(settings.db_path)
    _submit(settings, tmp_path, db)

    build_blog_site(settings, db=db)

    preview = (
        tmp_path / "site" / "blog" / "preview" / "we-measured-nepalimos" / "index.html"
    ).read_text(encoding="utf-8")
    assert "cmt-panel" in preview
    assert "blog-comment " in preview
    assert "discord.com/api/webhooks/x/y" in preview
    # published pages never carry the annotator
    publish_blog_post(
        settings, "we-measured-nepalimos", db=db, deploy=False, notify=False
    )
    build_blog_site(settings, db=db)
    post = (
        tmp_path / "site" / "blog" / "we-measured-nepalimos" / "index.html"
    ).read_text(encoding="utf-8")
    assert "cmt-panel" not in post


def test_gateway_parses_webhook_comment_and_revise_phrase():
    from bemine.discord import gateway as gw

    assert gw._parse_blog_comment(
        "blog-comment my-post\n> the selected text\nplease tighten this"
    ) == ("my-post", "> the selected text\nplease tighten this")
    assert gw._parse_blog_comment("hello there") is None
    assert gw._direct_blog_command("revise blog my-post") == {
        "command": "blog.revise",
        "slug": "my-post",
    }


def test_blog_revise_command_spawns_worker_with_all_notes(tmp_path, monkeypatch):
    from bemine.commands import run_command

    monkeypatch.setattr("bemine.blog.blog_site_dir", lambda: tmp_path / "site")
    settings = BemineSettings.load(
        state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v", device_id="cdjk"
    )
    db = BemineDB(settings.db_path)
    draft = tmp_path / "d.md"
    draft.write_text(GOOD_DRAFT, encoding="utf-8")
    submit_blog_draft(settings, file=draft, title="Revise me", db=db, notify=False)
    request_blog_changes(
        settings, "revise-me", "first comment", db=db, notify=False, spawn_reviser=False
    )
    request_blog_changes(
        settings,
        "revise-me",
        "second comment",
        db=db,
        notify=False,
        spawn_reviser=False,
    )
    started = {}
    monkeypatch.setattr(
        "bemine.blog._spawn_revision_worker",
        lambda s, d, *, slug, note, row, controller=None: (
            started.update({"slug": slug, "note": note}) or "blog-revise-revise-me"
        ),
    )

    out = run_command("blog.revise", settings, {"slug": "revise-me"})

    assert "blog-revise-revise-me" in out.text
    assert "first comment" in started["note"]
    assert "second comment" in started["note"]


def test_request_changes_routes_to_live_origin_session(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    db = BemineDB(settings.db_path)
    _submit(settings, tmp_path, db)  # project=neptts-bench
    monkeypatch.setattr(
        "bemine.blog._origin_for_project",
        lambda s, p: {
            "device": "mbp",
            "cwd": "/Users/cdjk/neptts-bench",
            "age": 5,
            "name": "neptts-bench",
        },
    )
    sent = {}

    class _FakeRT:
        def send_text(self, device, pane, text):
            sent.update({"device": device, "pane": pane, "text": text})

    monkeypatch.setattr(
        "bemine.blog._live_origin_pane", lambda s, d, c: (_FakeRT(), "%9")
    )

    out = request_blog_changes(
        settings,
        "we-measured-nepalimos",
        "need the real MOS table",
        db=db,
        notify=False,
    )

    assert out["reviser"] == "origin-session:mbp:%9"
    assert sent["pane"] == "%9"
    assert "need the real MOS table" in sent["text"]
    assert "blog update we-measured-nepalimos -" in sent["text"]


def test_request_changes_spawns_worker_on_origin_device(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    db = BemineDB(settings.db_path)
    _submit(settings, tmp_path, db)
    monkeypatch.setattr(
        "bemine.blog._origin_for_project",
        lambda s, p: {
            "device": "mbp",
            "cwd": "/Users/cdjk/neptts-bench",
            "age": 5,
            "name": "neptts-bench",
        },
    )
    monkeypatch.setattr("bemine.blog._live_origin_pane", lambda s, d, c: None)

    class _FakeController:
        def __init__(self):
            self.calls = []

        def start_on_device(self, *, name, runtime, cwd, target_device, command=""):
            self.calls.append(("remote", name, str(cwd), target_device))

        def start(self, **kwargs):
            self.calls.append(("local", kwargs))

        def send_when_ready(self, name, text, **kwargs):
            self.calls.append(("brief", text))

    controller = _FakeController()
    out = request_blog_changes(
        settings,
        "we-measured-nepalimos",
        "add numbers",
        db=db,
        notify=False,
        controller=controller,
    )

    assert out["reviser"].startswith("blog-revise-")
    kind, _name, cwd, device = controller.calls[0]
    assert kind == "remote" and device == "mbp"
    assert cwd == "/Users/cdjk/neptts-bench"
    brief = controller.calls[1][1]
    assert "ssh cdjk" in brief and "blog update we-measured-nepalimos -" in brief


def test_update_blog_draft_replaces_content_and_reverts_to_draft(tmp_path, monkeypatch):
    from bemine.blog import update_blog_draft

    settings = _settings(tmp_path, monkeypatch)
    db = BemineDB(settings.db_path)
    _submit(settings, tmp_path, db)
    request_blog_changes(
        settings,
        "we-measured-nepalimos",
        "fix it",
        db=db,
        notify=False,
        spawn_reviser=False,
    )
    revised = tmp_path / "revised.md"
    revised.write_text(GOOD_DRAFT + "\n\nAdded the full MOS table.", encoding="utf-8")

    out = update_blog_draft(
        settings, "we-measured-nepalimos", revised, db=db, notify=False, deploy=False
    )

    assert out["status"] == "draft"
    row = db.get_blog_post("we-measured-nepalimos")
    text = (Path(str(row["source_dir"])) / "post.md").read_text(encoding="utf-8")
    assert "Added the full MOS table." in text
    with pytest.raises(ValueError, match="suspiciously short"):
        short = tmp_path / "short.md"
        short.write_text("oops", encoding="utf-8")
        update_blog_draft(settings, "we-measured-nepalimos", short, db=db, notify=False)


def test_mirror_blog_to_git_commits_and_pushes(tmp_path, monkeypatch) -> None:
    def git(*args: str, cwd: Path) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout

    remote = tmp_path / "remote.git"
    git("init", "-q", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    repo = tmp_path / "clone"
    git("clone", "-q", str(remote), str(repo), cwd=tmp_path)
    git("config", "user.email", "t@t", cwd=repo)
    git("config", "user.name", "t", cwd=repo)
    (repo / "README").write_text("x")
    git("add", ".", cwd=repo)
    git("commit", "-qm", "init", cwd=repo)
    git("push", "-q", "-u", "origin", "main", cwd=repo)
    monkeypatch.setattr(
        "bemine.blog._cfg",
        lambda key, default: str(repo) if key == "git_repo_dir" else default,
    )
    blog = tmp_path / "blog"
    (blog / "preview").mkdir(parents=True)
    (blog / "preview" / "index.html").write_text("drafts")

    first = mirror_blog_to_git(blog)
    assert first["ok"] and first["commit"]
    assert git("log", "-1", "--format=%s", "main", cwd=remote).strip() == (
        "Sync blog from Bemine deploy"
    )
    assert mirror_blog_to_git(blog) == {"ok": True, "commit": None}

    monkeypatch.setattr("bemine.blog._cfg", lambda key, default: str(tmp_path / "nope"))
    assert mirror_blog_to_git(blog)["ok"] is False


def test_build_never_publishes_markdown_from_draft_dir(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = BemineDB(settings.db_path)
    result = _submit(settings, tmp_path, db)
    src = Path(result["source_dir"])
    (src / "README.md").write_text("internal provenance", encoding="utf-8")
    (src / "chart.png").write_bytes(b"png")
    build_blog_site(settings, db=db)
    preview = tmp_path / "site" / "blog" / "preview" / result["slug"]
    assert (preview / "chart.png").is_file()
    assert not list(preview.glob("*.md"))


def test_front_matter_feeds_the_card_and_never_renders_as_text(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    db = BemineDB(settings.db_path)
    draft = tmp_path / "fm.md"
    draft.write_text(
        "---\ntype: benchmark\nglyph: य\nsummary: One line card summary.\n"
        "code: https://github.com/Ampixa/neptts-bench\n---\n" + GOOD_DRAFT,
        encoding="utf-8",
    )
    fm = submit_blog_draft(
        settings, file=draft, title="Front matter post", project="neptts-bench",
        author="t", db=db, notify=False,
    )
    plain = _submit(settings, tmp_path, db)
    blog = tmp_path / "site" / "blog"
    preview = (blog / "preview" / fm["slug"] / "index.html").read_text(encoding="utf-8")
    assert "noindex" in preview and "type: benchmark" not in preview
    assert "benchmark · neptts bench" in preview
    assert 'href="https://github.com/Ampixa/neptts-bench">code' in preview

    publish_blog_post(settings, fm["slug"], db=db, deploy=False, notify=False)
    live = (blog / fm["slug"] / "index.html").read_text(encoding="utf-8")
    assert "noindex" not in live and "One line card summary." in live
    index = (blog / "index.html").read_text(encoding="utf-8")
    assert "One line card summary." in index and fm["slug"] in index
    assert plain["slug"] not in index  # drafts never appear on the public index
    assert list((blog / "_static").glob("blog.*.css"))
    assert (blog / "_static" / "fonts" / "nithya-ranjana-du.woff2").is_file()


def test_pages_carry_full_social_metadata(tmp_path, monkeypatch) -> None:
    import struct
    import zlib

    settings = _settings(tmp_path, monkeypatch)
    db = BemineDB(settings.db_path)
    draft = tmp_path / "og.md"
    draft.write_text("---\nimage: card.png\n---\n" + GOOD_DRAFT, encoding="utf-8")
    result = submit_blog_draft(settings, file=draft, title="Social post", db=db, notify=False)
    raw = zlib.compress(b"\x00" * (1 + 3 * 1200) * 630)
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    (Path(result["source_dir"]) / "card.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1200, 630, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", raw) + chunk(b"IEND", b""))
    publish_blog_post(settings, result["slug"], db=db, deploy=False, notify=False)
    blog = tmp_path / "site" / "blog"
    post = (blog / result["slug"] / "index.html").read_text(encoding="utf-8")
    for tag in ('og:type" content="article"', 'og:site_name" content="Ampixa Labs"',
                f'og:url" content="https://ampixa.com/blog/{result["slug"]}/"',
                f'og:image" content="https://ampixa.com/blog/{result["slug"]}/card.png"',
                'og:image:width" content="1200"', 'og:image:height" content="630"',
                'twitter:title" content="Social post"', "article:published_time", "apple-touch-icon"):
        assert tag in post, tag
    index = (blog / "index.html").read_text(encoding="utf-8")
    assert 'og:image" content="https://ampixa.com/assets/og-card.png' in index  # site card fallback
    assert (blog / "_static" / "icons" / "favicon.svg").is_file()
