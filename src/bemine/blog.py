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

import hashlib
import html
import json
import logging
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

log = logging.getLogger(__name__)


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
    _, text = split_front_matter(text)
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


def _origin_for_project(settings: BemineSettings, project: str) -> dict[str, Any]:
    """Where did this draft's material come from? Resolve via live fleet
    attribution: device + cwd of the freshest session for the project name."""
    if not project.strip():
        return {}
    from bemine.discord.dispatch import _channel_key, _fleet_channel_candidates

    key = _channel_key(project)
    candidates = [
        info
        for info in _fleet_channel_candidates(settings).values()
        if str(info.get("name")) == key
    ]
    if not candidates:
        return {}
    return min(candidates, key=lambda info: int(info["age"]))


def _live_origin_pane(
    settings: BemineSettings, device: str, cwd: str
) -> tuple[Any, str] | None:
    """A still-running agent pane in the origin cwd — the session that has the
    actual context that produced the draft."""
    from bemine.jobs.auto_recovery import _is_agent_pane
    from bemine.jobs.remote_tmux import RemoteTmux

    tmux = RemoteTmux(gateway_device=settings.device_id)
    target = "local" if device == settings.device_id else device
    try:
        for pane in tmux.list_panes(target):
            if str(pane.get("path")) == cwd and _is_agent_pane(pane.get("command")):
                return tmux, str(pane["pane_id"])
    except Exception:
        return None
    return None


def _context_request_message(slug: str, title: str, note: str) -> str:
    return "\n".join(
        [
            f"Bemine blog review: your draft `{slug}` ({title}) has reviewer "
            "comments that need YOUR context:",
            note.strip(),
            "",
            "You wrote this post, so you have the evidence. Revise the full "
            "markdown and push it back with:",
            f"  cat revised.md | ssh cdjk 'cd ~/bm && ~/.local/bin/uv run "
            f"bemine blog update {slug} -'",
            "(keep the same title; the preview refreshes automatically)",
        ]
    )


def _spawn_revision_worker(
    settings: BemineSettings,
    db: BemineDB,
    *,
    slug: str,
    note: str,
    row: Any,
    controller: Any = None,
) -> str:
    """Route the change request to whoever has the most context.

    1. the ORIGINATING session, if still alive (it produced the draft);
    2. a worker ON the origin device in the project repo (evidence is local);
    3. a local worker with the draft + notes (fallback)."""
    from bemine.workers.workers import WorkerController

    origin = _origin_for_project(settings, str(row["project"] or ""))
    if origin:
        live = _live_origin_pane(settings, str(origin["device"]), str(origin["cwd"]))
        if live is not None:
            tmux, pane_id = live
            target = (
                "local"
                if str(origin["device"]) == settings.device_id
                else str(origin["device"])
            )
            tmux.send_text(
                target,
                pane_id,
                _context_request_message(slug, str(row["title"]), note),
            )
            db.record_audit_event(
                category="blog",
                event_type="context_requested_from_origin",
                actor="bemine",
                device_id=settings.device_id,
                payload_json=json.dumps(
                    {"slug": slug, "device": str(origin["device"]), "pane": pane_id},
                    sort_keys=True,
                ),
            )
            return f"origin-session:{origin['device']}:{pane_id}"

    controller = controller or WorkerController(settings, db=db)
    name = f"blog-revise-{slug[:24]}".rstrip("-")
    existing = db.get_managed_session(name)
    if existing is not None and str(existing["status"]) in {"running", "adopted"}:
        return name  # already revising; the new note is in the draft record

    draft_path = Path(str(row["source_dir"])) / "post.md"
    remote_origin = origin and str(origin["device"]) != settings.device_id
    if remote_origin:
        # the evidence (repo, results) lives on the origin device — revise there
        brief = "\n".join(
            [
                "# Blog revision task (origin device)",
                f"Revise the Ampixa blog draft `{slug}` ({row['title']}).",
                f"The draft was written from THIS project: {origin['cwd']}",
                f"Reviewer comments: {note.strip()}",
                "",
                "Fetch the current draft:",
                f"  ssh cdjk 'cat {draft_path}'",
                "Use the repo/results here to address the comments with REAL "
                "numbers and links (ampixa-blog skill rules: artifact-tied "
                "claims, no hype). Then push the revised markdown back:",
                f"  cat revised.md | ssh cdjk 'cd ~/bm && ~/.local/bin/uv run "
                f"bemine blog update {slug} -'",
                "Do NOT publish.",
            ]
        )
        controller.start_on_device(
            name=name,
            runtime="claude",
            cwd=Path(str(origin["cwd"])),
            target_device=str(origin["device"]),
        )
    else:
        brief = "\n".join(
            [
                "# Blog revision task",
                f"Revise the Ampixa blog draft `{slug}` ({row['title']}).",
                f"Draft file: {draft_path}",
                f"Requested change: {note.strip()}",
                "",
                "Rules: follow the ampixa-blog skill (artifact-tied claims, no "
                "hype, text-first). Edit the draft file in place. Do NOT publish.",
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
        payload_json=json.dumps(
            {
                "slug": slug,
                "worker": name,
                "device": str(origin.get("device") or settings.device_id)
                if origin
                else settings.device_id,
            },
            sort_keys=True,
        ),
    )
    return name


def update_blog_draft(
    settings: BemineSettings,
    slug: str,
    file: Path | str,
    *,
    db: BemineDB | None = None,
    notify: bool = True,
    deploy: bool = True,
) -> dict[str, Any]:
    """The return path: the origin agent pushes its revised draft back.

    `file="-"` reads stdin (the ssh pipe from a remote device)."""
    db = db or BemineDB(settings.db_path)
    row = db.get_blog_post(slug)
    if row is None:
        raise ValueError(f"unknown blog post: {slug}")
    if str(file) == "-":
        import sys

        body = sys.stdin.read()
    else:
        body = Path(file).expanduser().read_text(encoding="utf-8")
    if len(body.strip()) < 50:
        raise ValueError("revised draft is suspiciously short; refusing to overwrite")
    (Path(str(row["source_dir"])) / "post.md").write_text(body, encoding="utf-8")
    return mark_blog_revised(
        settings, slug, "author updated the draft", db=db, notify=notify, deploy=deploy
    )


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

# Shared CSS/JS/fonts live in src/bemine/blog_static/ and are copied to <site>/blog/_static/ on every build.
# The deploy rsyncs blog/ with --delete, so they must be emitted under blog/; not under blog/preview/, which is
# behind basic auth and would 401 for public pages.
STATIC_SRC = Path(__file__).parent / "blog_static"
FONTS_CSS = (
    "https://fonts.googleapis.com/css2?family=Press+Start+2P&family=IBM+Plex+Mono:wght@400;600"
    "&family=Noto+Serif+Devanagari:wght@400;700&family=Noto+Sans+Devanagari:wght@400;600&display=swap"
)
POST_TYPES = ("release", "benchmark", "dataset", "lesson", "post")
_FRONT_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.S)
_DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")
# blocks the read-aloud player speaks, in reading order (tables, code and figures are skipped)
_SPOKEN_RE = re.compile(r"<(h2|p|li|figcaption)((?:\s[^>]*)?)>(.*?)</\1>", re.S)


def split_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Optional `---` block of `key: value` lines at the top of post.md; returns (meta, body)."""
    m = _FRONT_RE.match(text)
    if not m:
        return {}, text
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise ValueError(f"front matter line is not `key: value`: {line!r}")
        meta[key.strip().lower()] = value.strip()
    return meta, text[m.end():]


def _plain(html_text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", html_text))).strip()


def _post_meta(row: Any, body_html: str, body_md: str, meta: dict[str, str]) -> dict[str, Any]:
    """Per-post card/header data: front matter wins, then defaults derived from the post itself."""
    title = str(row["title"])
    summary = meta.get("summary", "")
    if not summary:
        for para in re.findall(r"<p>(.*?)</p>", body_html, flags=re.S):
            text = _plain(para)
            if len(re.sub(r"[^A-Za-z]", "", text)) >= 40:
                summary = text
                break
    summary = compact_text(summary, limit=180)
    kind = meta.get("type", "post").lower()
    if kind not in POST_TYPES:
        raise ValueError(f"front matter type {kind!r} is not one of {', '.join(POST_TYPES)}")
    glyph = meta.get("glyph", "")
    if not glyph:
        found = _DEVANAGARI_RE.findall(title)
        glyph = found[0] if found else "अ"
    links = []
    for key, label in (("model", "model"), ("code", "code"), ("dataset", "dataset"), ("demo", "demo"),
                       ("paper", "paper"), ("benchmark", "benchmark")):
        url = meta.get(key, "")
        if url:
            if not url.startswith("https://"):
                raise ValueError(f"front matter {key} must be an https:// URL, got {url!r}")
            links.append((label, url))
    project = meta.get("project") or str(row["project"] or "") or "notes"
    words = len(_plain(body_html).split())
    ts = float(row["published_at"] or row["updated_at"] or time.time())
    return {
        "slug": str(row["slug"]),
        "title": title,
        "summary": summary,
        "type": kind,
        "glyph": glyph,
        "project": project,
        "project_label": meta.get("project_label", project.replace("-", " ")),
        "links": links,
        "author": meta.get("author", "Ampixa Labs"),
        "date": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"),
        "minutes": max(1, round(words / 230)),
        "body_md_words": len(body_md.split()),
    }


def _copy_static(blog_dir: Path) -> dict[str, str]:
    """Copy blog_static/ into blog/_static/; css/js get content-hashed names. Returns their URLs."""
    if not STATIC_SRC.is_dir():
        raise RuntimeError(f"blog static assets missing at {STATIC_SRC}")
    out = blog_dir / "_static"
    if out.exists():
        shutil.rmtree(out)
    (out / "fonts").mkdir(parents=True)
    for font in (STATIC_SRC / "fonts").iterdir():
        if font.is_file() and font.suffix.lower() != ".md":
            shutil.copy2(font, out / "fonts" / font.name)
    urls = {}
    for name in ("blog.css", "blog.js"):
        data = (STATIC_SRC / name).read_bytes()
        stem, ext = name.rsplit(".", 1)
        hashed = f"{stem}.{hashlib.sha256(data).hexdigest()[:10]}.{ext}"
        (out / hashed).write_bytes(data)
        urls[ext] = f"/blog/_static/{hashed}"
    return urls


def _page(title: str, body_html: str, static: dict[str, str], *, noindex: bool = False,
          description: str = "", image: str = "", canonical: str = "") -> str:
    head = [f'<link rel="stylesheet" href="{static["css"]}">']
    if noindex:
        head.append('<meta name="robots" content="noindex, nofollow">')
    if description:
        head.append(f'<meta name="description" content="{html.escape(description)}">')
        head.append(f'<meta property="og:description" content="{html.escape(description)}">')
    if image:
        head.append(f'<meta property="og:image" content="{html.escape(image)}">')
        head.append('<meta name="twitter:card" content="summary_large_image">')
    if canonical:
        head.append(f'<link rel="canonical" href="{html.escape(canonical)}">')
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{html.escape(title)} — ampixa labs</title>
<meta property="og:title" content="{html.escape(title)}">
<link rel="icon" type="image/png" href="/assets/logo.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="{FONTS_CSS}">
{chr(10).join(head)}
</head>
<body>
<div class="wrap">
<header class="top"><a class="brand" href="/">ampixa<b>▪</b>labs</a>
<nav><a href="/">work</a><a href="/blog/" aria-current="page">blog</a><a href="https://github.com/Ampixa">github</a></nav></header>
{body_html}
</div>
<script src="{static["js"]}" defer></script>
</body>
</html>
"""


def _render_markdown(text: str) -> str:
    import markdown

    out = markdown.markdown(text, extensions=["fenced_code", "tables", "toc"])
    # a paragraph that is nothing but italics (`_caption_` under a figure or table) is a caption
    return re.sub(r"<p><em>((?:(?!</?p>|</em>).)*)</em></p>", r'<p class="cap"><em>\1</em></p>', out, flags=re.S)


def _tag_spoken_blocks(body_html: str) -> tuple[str, int]:
    """Number the blocks the read-aloud player speaks (data-a=N), same order the audio clips are rendered in."""
    count = 0

    def tag(m: re.Match) -> str:
        nonlocal count
        if len(re.sub(r"[^A-Za-zऀ-ॿ]", "", _plain(m.group(3)))) < 8:
            return m.group(0)
        out = f"<{m.group(1)}{m.group(2)} data-a=\"{count}\">{m.group(3)}</{m.group(1)}>"
        count += 1
        return out

    return _SPOKEN_RE.sub(tag, body_html), count


def spoken_blocks(text: str) -> list[str]:
    """Plain text of each block the player speaks, for rendering audio/<n>.mp3 outside Bemine."""
    _, body = split_front_matter(text)
    return [_plain(m.group(3)) for m in _SPOKEN_RE.finditer(_render_markdown(body))
            if len(re.sub(r"[^A-Za-zऀ-ॿ]", "", _plain(m.group(3)))) >= 8]


def _post_body(meta: dict[str, Any], body_html: str, *, audio_clips: int, banner: str = "") -> str:
    toc = re.findall(r'<h2 id="([^"]+)">(.*?)</h2>', body_html, flags=re.S)
    listen = ""
    if audio_clips:
        body_html, tagged = _tag_spoken_blocks(body_html)
        if tagged != audio_clips:
            raise ValueError(f"{meta['slug']}: audio/ has {audio_clips} clips but the post has {tagged} spoken blocks")
        info = f"read aloud · {meta['minutes']} min read · hover a paragraph and press ▶ to start there"
        listen = (f'<div class="listen" id="listen" data-info="{html.escape(info)}">'
                  '<button id="l-play" aria-label="Listen to this post" aria-pressed="false">▶ listen</button>'
                  f'<span class="l-info" id="l-info">{html.escape(info)}</span>'
                  '<span class="l-bar"><span id="l-prog"></span></span>'
                  '<button id="l-speed" aria-label="Playback speed">1×</button></div>')
    links = "".join(f'<a href="{html.escape(u)}">{html.escape(l)} ↗</a>' for l, u in meta["links"])
    toc_html = ""
    if len(toc) >= 3:
        toc_html = ('<nav class="toc" aria-label="Contents"><div class="label">contents</div><ol>'
                    + "".join(f'<li><a href="#{i}">{t}</a></li>' for i, t in toc) + "</ol></nav>")
    return f"""{banner}<a class="back" href="/blog/">← all posts</a>
<article>
<header class="phead">
  <span class="pback" aria-hidden="true">{html.escape(meta["glyph"])}</span>
  <div class="label">{html.escape(meta["type"])} · {html.escape(meta["project_label"])}</div>
  <h1 class="ptitle">{html.escape(meta["title"])}</h1>
  <div class="pmeta">{meta["date"]} · {html.escape(meta["author"])} · {meta["minutes"]} min read</div>
  {f'<div class="links">{links}</div>' if links else ""}
</header>
{listen}
<div class="pgrid{"" if toc_html else " notoc"}">{toc_html}<div class="prose" id="prose">
{body_html}
</div></div>
</article>"""


def _index_body(posts: list[dict[str, Any]]) -> str:
    cards = "\n".join(
        f'<li class="card" data-p="{html.escape(p["project"])}"><a href="/blog/{p["slug"]}/">'
        f'<span class="glyph" aria-hidden="true">{html.escape(p["glyph"])}</span>'
        f'<span class="label">{html.escape(p["type"])} · {html.escape(p["project_label"])}</span>'
        f'<h2>{html.escape(p["title"])}</h2><p>{html.escape(p["summary"])}</p>'
        f'<span class="meta"><span>{p["date"]}</span><span>→</span></span></a></li>'
        for p in posts
    )
    projects: dict[str, str] = {}
    for p in posts:
        projects.setdefault(p["project"], p["project_label"])
    data = {
        "projects": projects,
        "posts": [{"t": p["title"], "p": p["project"], "g": p["glyph"], "d": p["date"],
                   "url": f"/blog/{p['slug']}/"} for p in posts],
    }
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    if not posts:
        cards = ""
    return f"""<h1 class="ih1">blog<span class="cur">_</span><span class="hglyph" lang="ne">लेख</span></h1>
<p class="lede">Benchmarks, releases and the experiments that did not work, for the languages of Nepal. Every claim links to a repo, a model card or a dataset.</p>
<div class="bar"><div class="chips" id="chips" role="group" aria-label="Filter by project"></div>
<div class="toggle" role="group" aria-label="View"><button id="v-grid" aria-pressed="true">grid</button><button id="v-map" aria-pressed="false">map</button></div></div>
{'<p class="empty">Nothing published yet.</p>' if not posts else ""}
<ul class="grid" id="grid">
{cards}
</ul>
<div class="map" id="map" hidden><svg id="svg" role="img" aria-label="Posts grouped by project"></svg><div class="outline" id="outline"></div></div>
<p class="legend" id="legend" hidden>Click a project to light its posts. Lines join posts to the project they report on.</p>
<script type="application/json" id="posts-data">{blob}</script>"""


def _copy_assets(source_dir: Path, dest: Path) -> int:
    """Copy a post's images (never its markdown) and, if present, its audio/ clips. Returns the clip count."""
    for asset in source_dir.iterdir():
        if asset.suffix.lower() != ".md" and asset.is_file():  # post.md and provenance notes stay private
            shutil.copy2(asset, dest / asset.name)
    audio = source_dir / "audio"
    if not audio.is_dir():
        return 0
    clips = sorted(audio.glob("*.mp3"), key=lambda p: int(p.stem) if p.stem.isdigit() else -1)
    if any(not c.stem.isdigit() for c in clips) or [int(c.stem) for c in clips] != list(range(len(clips))):
        raise ValueError(f"{source_dir.name}: audio/ must hold 0.mp3 … N.mp3 with no gaps")
    (dest / "audio").mkdir(exist_ok=True)
    for clip in clips:
        shutil.copy2(clip, dest / "audio" / clip.name)
    return len(clips)


def build_blog_site(
    settings: BemineSettings, *, db: BemineDB | None = None
) -> dict[str, Any]:
    """Render /blog/index.html, every published post, and gated previews into the site dir."""
    db = db or BemineDB(settings.db_path)
    site = blog_site_dir()
    blog_dir = site / "blog"
    blog_dir.mkdir(parents=True, exist_ok=True)
    static = _copy_static(blog_dir)
    rows = db.list_blog_posts()
    published = [row for row in rows if str(row["status"]) == "published"]
    cards = []
    for row in sorted(published, key=lambda r: float(r["published_at"] or 0), reverse=True):
        slug = str(row["slug"])
        source_dir = Path(str(row["source_dir"]))
        meta_raw, body_md = split_front_matter((source_dir / "post.md").read_text(encoding="utf-8"))
        body = _render_markdown(body_md)
        meta = _post_meta(row, body, body_md, meta_raw)
        post_dir = blog_dir / slug
        post_dir.mkdir(parents=True, exist_ok=True)
        clips = _copy_assets(source_dir, post_dir)
        image = meta_raw.get("image", "")
        (post_dir / "index.html").write_text(
            _page(meta["title"], _post_body(meta, body, audio_clips=clips), static,
                  description=meta["summary"], canonical=f"https://ampixa.com/blog/{slug}/",
                  image=f"https://ampixa.com/blog/{slug}/{image}" if image else ""),
            encoding="utf-8",
        )
        cards.append(meta)
    # live previews for drafts under review: real rendered page, DRAFT banner, noindex, never linked from the
    # index — review happens on the actual site
    preview_root = blog_dir / "preview"
    if preview_root.exists():
        shutil.rmtree(preview_root)
    drafts = [row for row in rows if str(row["status"]) != "published"]
    failed: list[str] = []
    for row in drafts:
        slug = str(row["slug"])
        source_dir = Path(str(row["source_dir"]))
        source = source_dir / "post.md"
        if not source.is_file():
            continue
        try:
            meta_raw, body_md = split_front_matter(source.read_text(encoding="utf-8"))
            body = _render_markdown(body_md)
            meta = _post_meta(row, body, body_md, meta_raw)
            preview_dir = preview_root / slug
            preview_dir.mkdir(parents=True, exist_ok=True)
            clips = _copy_assets(source_dir, preview_dir)
            banner = (f'<div class="draft-banner">DRAFT PREVIEW — not published (status: '
                      f'{html.escape(str(row["status"]))}) · select text to comment</div>')
            (preview_dir / "index.html").write_text(
                _page(f"[draft] {meta['title']}",
                      _post_body(meta, body, audio_clips=clips, banner=banner) + _annotator_html(slug),
                      static, noindex=True),
                encoding="utf-8",
            )
        except (ValueError, OSError) as exc:
            # one broken draft must not take down every other preview; it is listed as broken on the dashboard
            log.error("blog preview %s failed: %s", slug, exc)
            failed.append(slug)
    # gated drafts dashboard at /blog/preview/ (the bare directory 403'd)
    if drafts:
        draft_items = []
        for row in sorted(drafts, key=lambda r: float(r["updated_at"] or 0), reverse=True):
            slug = str(row["slug"])
            source = Path(str(row["source_dir"])) / "post.md"
            check = "missing draft file"
            if slug in failed:
                check = "preview failed to render — see bemine log"
            elif source.is_file():
                _, body_md = split_front_matter(source.read_text(encoding="utf-8"))
                problems = validate_blog_draft(body_md, str(row["title"]))
                check = "; ".join(problems) if problems else "style: clean"
            note = compact_text(str(row["notes"] or "").strip(), limit=160)
            draft_items.append(
                f'<li><a href="/blog/preview/{slug}/">{html.escape(str(row["title"]))}</a>'
                f'<br><span class="pmeta">[{row["status"]}] {html.escape(check)}'
                f"{' · ' + html.escape(note) if note else ''}</span></li>"
            )
        dashboard = (
            '<h1 class="ih1">drafts under review</h1>\n<ul class="prose">\n' + "\n".join(draft_items) + "\n</ul>"
            '<p class="pmeta">publish: say <code>publish blog &lt;slug&gt;</code> in #ampixa-blog · changes: '
            "<code>request changes on &lt;slug&gt;: &lt;note&gt;</code></p>"
        )
    else:
        dashboard = '<h1 class="ih1">drafts under review</h1><p class="empty">Queue is empty.</p>'
    preview_root.mkdir(parents=True, exist_ok=True)
    (preview_root / "index.html").write_text(_page("drafts", dashboard, static, noindex=True), encoding="utf-8")
    (blog_dir / "index.html").write_text(
        _page("blog", _index_body(cards), static, canonical="https://ampixa.com/blog/",
              description="Benchmarks, releases and lessons from Ampixa Labs, for the languages of Nepal."),
        encoding="utf-8",
    )
    return {
        "published": len(published),
        "previews": len(drafts) - len(failed),
        "preview_failures": failed,
        "site_dir": str(blog_dir),
    }


def deploy_blog_site(settings: BemineSettings) -> dict[str, Any]:
    import os

    # HARD guard: a test run once rsynced tmp-dir content over the live site.
    # PYTEST_CURRENT_TEST is set by pytest and inherited by subprocesses, so
    # this closes every path a monkeypatch fixture cannot reach.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError("refusing to deploy the blog from inside a test run")
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
    return {
        "host": host,
        "remote": f"{remote}/blog/",
        "git": mirror_blog_to_git(local_blog),
    }


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {proc.stderr.strip()[:400]}")
    return proc.stdout


def mirror_blog_to_git(local_blog: Path) -> dict[str, Any]:
    """Keep Ampixa/ampixa-site in parity with what was just deployed.

    The live deploy already succeeded, so a git failure is logged and reported
    in the result instead of raised (raising would mask the live deploy)."""
    repo = Path(_cfg("git_repo_dir", "~/github/ampixa-site")).expanduser()
    try:
        if not (repo / ".git").is_dir():
            raise RuntimeError(f"site repo clone missing at {repo}")
        _git(repo, "pull", "--rebase", "-q")
        (repo / "site" / "blog").mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["rsync", "-a", "--delete", f"{local_blog}/", f"{repo}/site/blog/"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"rsync to repo failed: {proc.stderr.strip()[:400]}")
        _git(repo, "add", "-A", "site/blog")
        if not _git(repo, "status", "--porcelain", "site/blog").strip():
            return {"ok": True, "commit": None}
        _git(repo, "commit", "-q", "-m", "Sync blog from Bemine deploy")
        _git(repo, "push", "-q")
        return {"ok": True, "commit": _git(repo, "rev-parse", "--short", "HEAD").strip()}
    except Exception as exc:
        log.error("blog git mirror failed (live deploy succeeded): %s", exc)
        return {"ok": False, "error": str(exc)}


def _annotator_html(slug: str) -> str:
    """Notion-style inline comments on gated previews: select text, write a
    note, it posts to #ampixa-blog through a webhook (form-data POST avoids a
    CORS preflight). The gateway records each comment on the draft; saying
    `revise blog <slug>` then applies all of them with one worker."""
    webhook = _cfg("comment_webhook", "").strip()
    if not webhook:
        return ""
    return f"""
<div id="cmt-panel" style="display:none;position:fixed;bottom:0;left:0;right:0;
background:#fff;border-top:3px solid {CRIMSON};padding:14px 24px;
box-shadow:0 -4px 16px rgba(0,0,0,.12);z-index:50;">
  <div style="font-size:.8em;color:#777;margin-bottom:6px;">commenting on:
    <em id="cmt-quote"></em></div>
  <textarea id="cmt-text" rows="2" placeholder="what should change here?"
    style="width:100%;font-family:inherit;font-size:.95em;padding:8px;
    box-sizing:border-box;border:1px solid #ccc;"></textarea>
  <div style="margin-top:8px;">
    <button id="cmt-send" style="background:{CRIMSON};color:#fff;border:0;
      padding:8px 18px;font-family:inherit;cursor:pointer;">send comment</button>
    <button id="cmt-cancel" style="background:#eee;border:0;padding:8px 14px;
      font-family:inherit;cursor:pointer;">cancel</button>
    <span id="cmt-status" style="margin-left:12px;font-size:.85em;color:#777;"></span>
  </div>
</div>
<script>
(function () {{
  var SLUG = {json.dumps(slug)};
  var HOOK = {json.dumps(webhook)};
  var panel = document.getElementById('cmt-panel');
  var quoteEl = document.getElementById('cmt-quote');
  var textEl = document.getElementById('cmt-text');
  var statusEl = document.getElementById('cmt-status');
  var currentQuote = '';
  document.addEventListener('mouseup', function () {{
    setTimeout(function () {{
      var sel = window.getSelection().toString().trim();
      if (sel.length > 3 && !panel.contains(document.activeElement)) {{
        currentQuote = sel.slice(0, 300);
        quoteEl.textContent = '"' + currentQuote.slice(0, 140)
          + (currentQuote.length > 140 ? '…' : '') + '"';
        statusEl.textContent = '';
        panel.style.display = 'block';
        textEl.focus();
      }}
    }}, 10);
  }});
  document.getElementById('cmt-cancel').onclick = function () {{
    panel.style.display = 'none'; textEl.value = '';
  }};
  document.getElementById('cmt-send').onclick = function () {{
    var note = textEl.value.trim();
    if (!note) {{ statusEl.textContent = 'write a note first'; return; }}
    var content = 'blog-comment ' + SLUG + '\\n> ' + currentQuote
      + '\\n' + note;
    var form = new FormData();
    form.append('payload_json', JSON.stringify({{content: content}}));
    statusEl.textContent = 'sending…';
    fetch(HOOK, {{method: 'POST', body: form}}).then(function (r) {{
      if (r.ok) {{
        statusEl.textContent = 'sent to #ampixa-blog ✓';
        textEl.value = '';
        setTimeout(function () {{ panel.style.display = 'none'; }}, 900);
      }} else {{ statusEl.textContent = 'failed (' + r.status + ')'; }}
    }}).catch(function () {{ statusEl.textContent = 'network error'; }});
  }};
}})();
</script>
"""


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
