from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .models import ProjectRundown
from ..util import safe_filename


STATUS_START = "<!-- bemine:status:start -->"
STATUS_END = "<!-- bemine:status:end -->"


class ObsidianVault:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ("Projects", "Daily", "Sessions", "Devices", "Handoffs", "Decisions", "Jobs", "Artifacts"):
            (self.root / name).mkdir(parents=True, exist_ok=True)

    def project_page_path(self, project_name: str) -> Path:
        return self.root / "Projects" / f"{safe_filename(project_name, 'project')}.md"

    def update_project_page(self, rundown: ProjectRundown) -> Path:
        path = self.project_page_path(rundown.name)
        existing = path.read_text(encoding="utf-8") if path.exists() else f"# {rundown.name}\n\n"
        status = self._render_status_block(rundown)
        path.write_text(_replace_block(existing, status), encoding="utf-8")
        return path

    def write_handoff(self, *, project_name: str, text: str, when: datetime | None = None) -> Path:
        when = when or datetime.now(timezone.utc)
        stamp = when.strftime("%Y-%m-%d-%H%M%S")
        path = self.root / "Handoffs" / f"{stamp}-{safe_filename(project_name, 'project')}.md"
        path.write_text(text.rstrip() + "\n", encoding="utf-8")
        return path

    def append_daily_entry(self, *, when: datetime | None = None, title: str, text: str) -> Path:
        when = when or datetime.now(timezone.utc)
        path = self.root / "Daily" / f"{when.date().isoformat()}.md"
        existing = path.read_text(encoding="utf-8") if path.exists() else f"# {when.date().isoformat()}\n"
        entry = "\n".join(["", f"## {title}", "", text.rstrip(), ""])
        path.write_text(existing.rstrip() + "\n" + entry, encoding="utf-8")
        return path

    def _render_status_block(self, rundown: ProjectRundown) -> str:
        generated = datetime.now(timezone.utc).isoformat()
        lines = [
            STATUS_START,
            "## Bemine Status",
            "",
            f"- Project ID: `{rundown.project_id}`",
            f"- Path: `{rundown.cwd}`",
            f"- Devices: {', '.join(rundown.devices) if rundown.devices else 'unknown'}",
            f"- Runtimes: {', '.join(rundown.runtimes) if rundown.runtimes else 'unknown'}",
            f"- Sessions: {rundown.session_count}",
            f"- Latest activity: {rundown.latest_at or 'unknown'}",
            f"- Updated by Bemine: {generated}",
            "",
            "### Recent Session Signals",
            "",
        ]
        if rundown.summaries:
            lines.extend(f"- {summary}" for summary in rundown.summaries)
        else:
            lines.append("- No session summaries yet.")
        lines.extend(["", STATUS_END, ""])
        return "\n".join(lines)


def _replace_block(existing: str, block: str) -> str:
    if STATUS_START in existing and STATUS_END in existing:
        before, rest = existing.split(STATUS_START, 1)
        _, after = rest.split(STATUS_END, 1)
        return before.rstrip() + "\n\n" + block + after
    return existing.rstrip() + "\n\n" + block
