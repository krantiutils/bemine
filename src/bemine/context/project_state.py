"""Per-project state packet for Discord dispatch.

When the user talks in a project channel, the agent must already know where
that project stands — recent sessions, the latest handoff, live workers, the
watcher brief — instead of asking or guessing (SOUL.md operating contract:
"latest known project summary and last interaction state").
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from bemine.config import BemineSettings, config_int
from bemine.db import BemineDB
from bemine.util import compact_text

_DEFAULT_LIMIT = 3000


def _limit() -> int:
    return max(500, config_int("context", "project_state_limit", _DEFAULT_LIMIT))


def latest_handoff_for(vault_dir: Path, cwd: Path) -> Path | None:
    handoffs = vault_dir / "Handoffs"
    if not handoffs.is_dir():
        return None
    suffix = f"-{cwd.name}.md"
    candidates = sorted(
        (p for p in handoffs.iterdir() if p.name.endswith(suffix)),
        key=lambda p: p.name,
    )
    return candidates[-1] if candidates else None


def _handoff_excerpt(path: Path, *, max_chars: int = 900) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    sections = []
    for heading in ("## Summary", "## Next Steps", "## Blockers"):
        match = re.search(rf"{re.escape(heading)}\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
        if match and match.group(1).strip():
            sections.append(f"{heading.removeprefix('## ')}: {match.group(1).strip()}")
    return compact_text(" | ".join(sections), limit=max_chars)


def project_state_context(
    settings: BemineSettings,
    *,
    project_id: str,
    cwd: Path,
    db: BemineDB | None = None,
) -> str:
    """Bounded, plain-text project state. Empty-state lines are explicit so the
    agent knows the absence is real, not missing context."""
    db = db or BemineDB(settings.db_path)
    lines: list[str] = []

    sessions = db.sessions_for_project(project_id, limit=3)
    if sessions:
        lines.append("Recent sessions (newest first):")
        for row in sessions:
            summary = compact_text(str(row["summary"] or row["title"] or ""), limit=240)
            lines.append(
                f"- [{row['runtime']}] {row['updated_at']}: {summary or '(no summary)'}"
            )
    else:
        lines.append("Recent sessions: none indexed for this project.")

    handoff = latest_handoff_for(settings.vault_dir, cwd)
    if handoff is not None:
        excerpt = _handoff_excerpt(handoff)
        lines.append(f"Latest handoff ({handoff.name}): {excerpt or '(empty)'}")
    else:
        lines.append("Latest handoff: none recorded.")

    workers = [
        row
        for row in db.list_managed_sessions()
        if str(row["project_id"]) == project_id
        and str(row["status"]) in {"running", "adopted"}
    ]
    if workers:
        lines.append("Live managed workers:")
        for row in workers:
            lines.append(
                f"- {row['name']} [{row['runtime']}] {row['status']} on {row['device_id']}"
            )
    else:
        lines.append("Live managed workers: none.")

    watcher = db.get_project_watcher(project_id)
    if watcher and str(watcher.get("status")) == "active":
        brief = compact_text(str(watcher.get("brief") or ""), limit=240)
        lines.append(f"Active watcher: every {watcher.get('interval_secs')}s — {brief}")
    else:
        lines.append("Active watcher: none.")

    text = "\n".join(lines)
    return text[: _limit()]
