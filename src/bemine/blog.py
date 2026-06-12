"""Ampixa blog pipeline — Bemine is the orchestrator.

Any agent session in reach submits material with `uv run bemine blog submit`;
Bemine notifies Discord, where the bot (or the user) reviews, asks for more
context, validates against the Ampixa style (subagents/style.md), renders the
static pages, and deploys to ampixa.com/blog.

Style ground rules enforced here (claims discipline): every post must tie its
claims to an artifact (repo, dataset, model card, benchmark, demo), and hype
words without evidence are rejected at validation.
"""

from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bemine.config import BemineSettings, config_string
from bemine.db import BemineDB
from bemine.util import compact_text

BLOG_STATUSES = ("draft", "needs_input", "published")
_HYPE_WORDS = (
    "revolutionary",
    "groundbreaking",
    "world-class",
    "world class",
    "best in class",
    "best-in-class",
    "cutting-edge",
    "cutting edge",
    "game-changing",
    "unparalleled",
)
_ARTIFACT_RE = re.compile(
    r"https?://|github\.com|huggingface\.co|\bmodel card\b|\bbenchmark\b",
    re.IGNORECASE,
)

CRIMSON = "#DC143C"


def _cfg(key: str, default: str) -> str:
    return config_string("blog", key, default)


def blog_site_dir() -> Path:
    return Path(_cfg("site_dir", "~/ampixa/site")).expanduser()


def blog_drafts_dir(settings: BemineSettings) -> Path:
    return settings.vault_dir / "Blog" / "drafts"


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    return slug[:60] or "post"


def submit_blog_draft(
    settings: BemineSettings,
    *,
    file: Path,
    title: str,
    project: str = "",
    author: str = "",
    images: list[Path] | None = None,
    db: BemineDB | None = None,
    notify: bool = True,
) -> dict[str, Any]:
    """The path ANY codex/claude session uses to hand material to Bemine.

    `file="-"` reads the draft from stdin, so remote-device agents can pipe:
    cat draft.md | ssh cdjk 'cd ~/bm && uv run bemine blog submit - --title ...'
    """
    db = db or BemineDB(settings.db_path)
    title = title.strip()
    if not title:
        raise ValueError("blog title is required")
    body = ""
    if str(file) == "-":
        import sys

        body = sys.stdin.read()
        if not body.strip():
            raise ValueError("empty draft on stdin")
    else:
        file = Path(file).expanduser()
        if not file.is_file():
            raise ValueError(f"draft markdown not found: {file}")
    slug = _slugify(title)
    if db.get_blog_post(slug) is not None:
        raise ValueError(f"a blog post with slug `{slug}` already exists")
    dest = blog_drafts_dir(settings) / slug
    dest.mkdir(parents=True, exist_ok=True)
    if body:
        (dest / "post.md").write_text(body, encoding="utf-8")
    else:
        shutil.copy2(file, dest / "post.md")
    copied_images: list[str] = []
    for image in images or []:
        image = Path(image).expanduser()
        if image.is_file():
            shutil.copy2(image, dest / image.name)
            copied_images.append(image.name)
    db.upsert_blog_post(
        slug=slug,
        title=title,
        project=project.strip(),
        author=author.strip() or "unknown-session",
        status="draft",
        source_dir=str(dest),
    )
    db.record_audit_event(
        category="blog",
        event_type="draft_submitted",
        actor=author.strip() or "agent",
        device_id=settings.device_id,
        payload_json=json.dumps(
            {"slug": slug, "title": title, "project": project, "images": copied_images},
            sort_keys=True,
        ),
    )
    problems = validate_blog_draft(
        (dest / "post.md").read_text(encoding="utf-8"), title
    )
    preview_url = f"https://ampixa.com/blog/preview/{slug}/"
    # refresh live previews so the review link works immediately (best-effort)
    try:
        build_blog_site(settings, db=db)
        deploy_blog_site(settings)
    except Exception:
        preview_url = ""
    if notify:
        lines = [
            f"📝 New blog draft: **{title}** (`{slug}`)",
            f"- project: {project or 'unspecified'} · author: {author or 'unknown'}"
            f" · images: {len(copied_images)}",
        ]
        if preview_url:
            lines.append(f"- preview: {preview_url}")
        lines.append(
            f"- changes: say `request changes on {slug}: <note>` · "
            f"publish: `publish blog {slug}`"
        )
        if problems:
            lines.append("- style check: " + "; ".join(problems))
        else:
            lines.append("- style check: clean")
        _notify_discord(settings, "\n".join(lines))
    return {
        "slug": slug,
        "title": title,
        "source_dir": str(dest),
        "problems": problems,
        "preview_url": preview_url,
    }


def validate_blog_draft(text: str, title: str) -> list[str]:
    """Deterministic Ampixa claims-discipline checks (subagents/style.md)."""
    problems: list[str] = []
    body = text.strip()
    if len(body) < 400:
        problems.append("too short (<400 chars) — lead with the concrete artifact")
    if not _ARTIFACT_RE.search(body):
        problems.append(
            "no artifact link — tie claims to a repo/dataset/model card/benchmark"
        )
    low = body.lower()
    for word in _HYPE_WORDS:
        if word in low:
            problems.append(f"hype word `{word}` — prefer 'we built/we measured'")
    if len(title) > 90:
        problems.append("title too long (>90 chars)")
    return problems


def request_blog_changes(
    settings: BemineSettings,
    slug: str,
    note: str,
    *,
    db: BemineDB | None = None,
    notify: bool = True,
    spawn_reviser: bool = True,
    controller: Any = None,
) -> dict[str, Any]:
    """Record the change request AND put an agent on it: a managed claude
    worker revises the draft per the note, refreshes the preview, and reports
    back with `bemine blog revised`."""
    db = db or BemineDB(settings.db_path)
    row = db.get_blog_post(slug)
    if row is None:
        raise ValueError(f"unknown blog post: {slug}")
    notes = (str(row["notes"] or "") + f"\n- {note.strip()}").strip()
    db.upsert_blog_post(
        slug=slug,
        title=str(row["title"]),
        project=str(row["project"]),
        author=str(row["author"]),
        status="needs_input",
        source_dir=str(row["source_dir"]),
        notes=notes,
    )
    db.record_audit_event(
        category="blog",
        event_type="changes_requested",
        actor="bemine",
        device_id=settings.device_id,
        payload_json=json.dumps({"slug": slug, "note": note}, sort_keys=True),
    )
    reviser = ""
    if spawn_reviser:
        try:
            reviser = _spawn_revision_worker(
                settings, db, slug=slug, note=note, row=row, controller=controller
            )
        except Exception:
            reviser = ""
    if notify:
        text = f"🔁 Changes requested on `{slug}`: {note.strip()}"
        if reviser:
            text += f"\n- revision worker `{reviser}` is on it"
        _notify_discord(settings, text)
    return {
        "slug": slug,
        "status": "needs_input",
        "notes": notes,
        "reviser": reviser,
    }


def _spawn_revision_worker(
    settings: BemineSettings,
    db: BemineDB,
    *,
    slug: str,
    note: str,
    row: Any,
    controller: Any = None,
) -> str:
    from bemine.workers.workers import WorkerController

    controller = controller or WorkerController(settings, db=db)
    name = f"blog-revise-{slug[:24]}".rstrip("-")
    existing = db.get_managed_session(name)
    if existing is not None and str(existing["status"]) in {"running", "adopted"}:
        return name  # already revising; the new note is in the draft record
    draft_path = Path(str(row["source_dir"])) / "post.md"
    brief = "\n".join(
        [
            "# Blog revision task",
            f"Revise the Ampixa blog draft `{slug}` ({row['title']}).",
            f"Draft file: {draft_path}",
            f"Requested change: {note.strip()}",
            "",
            "Rules: follow the ampixa-blog skill (artifact-tied claims, no hype,",
            "text-first). Edit the draft file in place. Do NOT publish.",
            "When done, run:",
            f"  uv run --directory /home/cdjk/bm bemine blog revised {slug} "
            f'--summary "<one line on what you changed>"',
            "That refreshes the live preview and notifies the reviewer.",
        ]
    )
    controller.start(name=name, runtime="claude", cwd=Path("/home/cdjk/bm"))
    controller.send_when_ready(name, brief)
    db.record_audit_event(
        category="blog",
        event_type="revision_worker_started",
        actor="bemine",
        device_id=settings.device_id,
        payload_json=json.dumps({"slug": slug, "worker": name}, sort_keys=True),
    )
    return name


def mark_blog_revised(
    settings: BemineSettings,
    slug: str,
    summary: str,
    *,
    db: BemineDB | None = None,
    notify: bool = True,
    deploy: bool = True,
) -> dict[str, Any]:
    """Called by the revision worker when its edit is done."""
    db = db or BemineDB(settings.db_path)
    row = db.get_blog_post(slug)
    if row is None:
        raise ValueError(f"unknown blog post: {slug}")
    notes = (str(row["notes"] or "") + f"\n- revised: {summary.strip()}").strip()
    db.upsert_blog_post(
        slug=slug,
        title=str(row["title"]),
        project=str(row["project"]),
        author=str(row["author"]),
        status="draft",
        source_dir=str(row["source_dir"]),
        notes=notes,
    )
    text = (Path(str(row["source_dir"])) / "post.md").read_text(encoding="utf-8")
    problems = validate_blog_draft(text, str(row["title"]))
    build_blog_site(settings, db=db)
    if deploy:
        try:
            deploy_blog_site(settings)
        except Exception:
            pass
    db.record_audit_event(
        category="blog",
        event_type="draft_revised",
        actor="revision-worker",
        device_id=settings.device_id,
        payload_json=json.dumps({"slug": slug, "summary": summary}, sort_keys=True),
    )
    if notify:
        lines = [
            f"✏️ Draft revised: `{slug}` — {summary.strip()}",
            f"- preview: https://ampixa.com/blog/preview/{slug}/",
            f"- style check: {'; '.join(problems) if problems else 'clean'}",
            f"- publish: `publish blog {slug}` · more changes: "
            f"`request changes on {slug}: <note>`",
        ]
        _notify_discord(settings, "\n".join(lines))
    return {"slug": slug, "status": "draft", "problems": problems}


def publish_blog_post(
    settings: BemineSettings,
    slug: str,
    *,
    db: BemineDB | None = None,
    deploy: bool = True,
    notify: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    db = db or BemineDB(settings.db_path)
    row = db.get_blog_post(slug)
    if row is None:
        raise ValueError(f"unknown blog post: {slug}")
    source = Path(str(row["source_dir"])) / "post.md"
    text = source.read_text(encoding="utf-8")
    problems = validate_blog_draft(text, str(row["title"]))
    if problems and not force:
        raise ValueError(
            "style validation failed (use --force to override):\n- "
            + "\n- ".join(problems)
        )
    db.upsert_blog_post(
        slug=slug,
        title=str(row["title"]),
        project=str(row["project"]),
        author=str(row["author"]),
        status="published",
        source_dir=str(row["source_dir"]),
        notes=str(row["notes"] or ""),
        published_at=time.time(),
    )
    build_blog_site(settings, db=db)
    deployed = deploy_blog_site(settings) if deploy else {"skipped": True}
    db.record_audit_event(
        category="blog",
        event_type="post_published",
        actor="bemine",
        device_id=settings.device_id,
        payload_json=json.dumps(
            {"slug": slug, "deploy": deployed, "forced": bool(problems)},
            sort_keys=True,
        ),
    )
    url = f"https://ampixa.com/blog/{slug}/"
    if notify:
        _notify_discord(settings, f"✅ Published: **{row['title']}** → {url}")
    return {"slug": slug, "url": url, "deploy": deployed, "problems": problems}


# --- rendering -----------------------------------------------------------


_PAGE_CSS = f"""
  body {{
    font-family: 'Lucida Console', Monaco, monospace;
    font-size: 17px; color: #111; background: #fff;
    max-width: 860px; margin: 0 auto; padding: 0 24px 64px;
    line-height: 1.6;
  }}
  a {{ color: {CRIMSON}; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  header {{ border-bottom: 1px solid #ddd; padding: 18px 0; margin-bottom: 28px; }}
  header a.home {{ color: #111; font-weight: bold; }}
  h1, h2, h3 {{ line-height: 1.25; }}
  h1 {{ font-size: 1.5em; }}
  .meta {{ color: #777; font-size: 0.85em; margin-bottom: 24px; }}
  pre {{ background: #f6f6f6; padding: 12px; overflow-x: auto; font-size: 0.85em; }}
  code {{ background: #f6f6f6; padding: 1px 4px; }}
  pre code {{ padding: 0; }}
  img {{ max-width: 100%; }}
  ul.posts {{ list-style: none; padding: 0; }}
  ul.posts li {{ margin: 14px 0; }}
  ul.posts .date {{ color: #777; margin-right: 12px; font-size: 0.85em; }}
  hr {{ border: 0; border-top: 1px solid #ddd; }}
"""


def _page(title: str, body_html: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{html.escape(title)} — ampixa labs</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/png" href="/assets/logo.png">
<style>{_PAGE_CSS}</style>
</head>
<body>
<header><a class="home" href="/">ampixa labs</a> · <a href="/blog/">blog</a></header>
{body_html}
</body>
</html>
"""


def _render_markdown(text: str) -> str:
    import markdown

    return markdown.markdown(text, extensions=["fenced_code", "tables"])


def build_blog_site(
    settings: BemineSettings, *, db: BemineDB | None = None
) -> dict[str, Any]:
    """Render /blog/index.html and every published post into the site dir."""
    db = db or BemineDB(settings.db_path)
    site = blog_site_dir()
    blog_dir = site / "blog"
    blog_dir.mkdir(parents=True, exist_ok=True)
    rows = db.list_blog_posts()
    published = [row for row in rows if str(row["status"]) == "published"]
    items = []
    for row in sorted(
        published, key=lambda r: float(r["published_at"] or 0), reverse=True
    ):
        slug = str(row["slug"])
        date = datetime.fromtimestamp(
            float(row["published_at"] or time.time()), tz=timezone.utc
        ).strftime("%Y-%m-%d")
        source_dir = Path(str(row["source_dir"]))
        body = _render_markdown((source_dir / "post.md").read_text(encoding="utf-8"))
        post_dir = blog_dir / slug
        post_dir.mkdir(parents=True, exist_ok=True)
        for asset in source_dir.iterdir():
            if asset.name != "post.md" and asset.is_file():
                shutil.copy2(asset, post_dir / asset.name)
        post_html = _page(
            str(row["title"]),
            f"<h1>{html.escape(str(row['title']))}</h1>"
            f'<div class="meta">{date}</div>\n{body}',
        )
        (post_dir / "index.html").write_text(post_html, encoding="utf-8")
        items.append(
            f'<li><span class="date">{date}</span>'
            f'<a href="/blog/{slug}/">{html.escape(str(row["title"]))}</a></li>'
        )
    # live previews for drafts under review: real rendered page, DRAFT banner,
    # noindex, never linked from the index — review happens on the actual site
    preview_root = blog_dir / "preview"
    if preview_root.exists():
        shutil.rmtree(preview_root)
    drafts = [row for row in rows if str(row["status"]) != "published"]
    for row in drafts:
        slug = str(row["slug"])
        source_dir = Path(str(row["source_dir"]))
        source = source_dir / "post.md"
        if not source.is_file():
            continue
        body = _render_markdown(source.read_text(encoding="utf-8"))
        preview_dir = preview_root / slug
        preview_dir.mkdir(parents=True, exist_ok=True)
        for asset in source_dir.iterdir():
            if asset.name != "post.md" and asset.is_file():
                shutil.copy2(asset, preview_dir / asset.name)
        banner = (
            f'<div style="background:{CRIMSON};color:#fff;padding:6px 12px;'
            f'margin-bottom:20px;">DRAFT PREVIEW — not published'
            f" (status: {row['status']})</div>"
        )
        preview_html = _page(
            f"[draft] {row['title']}",
            f"{banner}<h1>{html.escape(str(row['title']))}</h1>\n{body}",
        ).replace(
            "<style>", '<meta name="robots" content="noindex, nofollow">\n<style>', 1
        )
        (preview_dir / "index.html").write_text(preview_html, encoding="utf-8")
    index_body = "<h1>blog</h1>\n"
    if items:
        index_body += '<ul class="posts">\n' + "\n".join(items) + "\n</ul>"
    else:
        index_body += (
            "<p>Nothing published yet. Raw audio infrastructure notes land here.</p>"
        )
    (blog_dir / "index.html").write_text(_page("blog", index_body), encoding="utf-8")
    return {
        "published": len(published),
        "previews": len(drafts),
        "site_dir": str(blog_dir),
    }


def deploy_blog_site(settings: BemineSettings) -> dict[str, Any]:
    host = _cfg("deploy_host", "ubuntu@178.104.21.224")
    key = str(Path(_cfg("ssh_key", "~/.ssh/monitor.pem")).expanduser())
    remote = _cfg("remote_site_dir", "/home/ubuntu/ampixa/site")
    local_blog = blog_site_dir() / "blog"
    proc = subprocess.run(
        [
            "rsync",
            "-az",
            "--delete",
            "-e",
            f"ssh -i {key} -o BatchMode=yes -o ConnectTimeout=10",
            f"{local_blog}/",
            f"{host}:{remote}/blog/",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"blog deploy failed: {proc.stderr.strip()[:400]}")
    return {"host": host, "remote": f"{remote}/blog/"}


# --- discord -------------------------------------------------------------


def _notify_discord(settings: BemineSettings, text: str) -> bool:
    try:
        import asyncio

        from bemine.discord.config import Settings as DiscordSettings
        from bemine.discord.send import send_message

        ds = DiscordSettings.load()
        if not ds.token:
            return False
        channel_id = _ensure_blog_channel(ds.token)
        if not channel_id:
            return False
        asyncio.run(
            send_message(settings=ds, channel_id=channel_id, text=text, files=[])
        )
        return True
    except Exception:
        return False


def _ensure_blog_channel(token: str) -> str:
    guild_id = config_string("discord", "guild_id", "").strip()
    if not guild_id:
        return ""
    name = _cfg("channel", "ampixa-blog")

    def req(method: str, path: str, payload: dict | None = None) -> Any:
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"https://discord.com/api/v10{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json",
                "User-Agent": "DiscordBot (https://github.com/krantiutils/bemine, 1.0)",
            },
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read()
            return json.loads(body) if body else {}

    channels = req("GET", f"/guilds/{guild_id}/channels")
    ops_id = ""
    for channel in channels:
        if channel.get("type") == 0 and channel.get("name") == name:
            return str(channel["id"])
        if channel.get("type") == 4 and channel.get("name") == "Bemine Ops":
            ops_id = str(channel["id"])
    payload: dict[str, Any] = {"name": name, "type": 0}
    if ops_id:
        payload["parent_id"] = ops_id
    created = req("POST", f"/guilds/{guild_id}/channels", payload)
    return str(created.get("id") or "")


# --- rendering for CLI/Discord -------------------------------------------


def render_blog_posts(rows: list[Any]) -> str:
    if not rows:
        return "No blog posts yet. Submit: `uv run bemine blog submit <file.md> --title ...`\n"
    lines = ["Blog queue:"]
    for row in rows:
        lines.append(
            f"- `{row['slug']}` [{row['status']}] {row['title']}"
            f" (project={row['project'] or 'n/a'}, author={row['author']})"
        )
    return "\n".join(lines) + "\n"


def render_blog_post(row: Any, text: str) -> str:
    problems = validate_blog_draft(text, str(row["title"]))
    lines = [
        f"# {row['title']} (`{row['slug']}`)",
        f"status: {row['status']} · project: {row['project'] or 'n/a'}"
        f" · author: {row['author']}",
        f"style check: {'; '.join(problems) if problems else 'clean'}",
    ]
    if str(row["notes"] or "").strip():
        lines.append(f"notes: {compact_text(str(row['notes']), limit=400)}")
    lines.append("")
    lines.append(text)
    return "\n".join(lines)
