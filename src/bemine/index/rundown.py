from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .models import ProjectRundown


def render_rundown(projects: list[ProjectRundown], *, vault_dir: Path | None = None) -> str:
    lines = [
        "# Project Rundown",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
    ]
    if not projects:
        lines.append("No projects indexed yet. Run `bemine scan` first.")
        return "\n".join(lines)

    for project in projects:
        page = None
        if vault_dir:
            candidate = vault_dir / "Projects" / f"{project.name}.md"
            if candidate.exists():
                page = candidate
        lines.extend(
            [
                f"## {project.name}",
                "",
                f"- State: {'active' if project.session_count else 'unknown'}",
                f"- Path: `{project.cwd}`",
                f"- Devices: {', '.join(project.devices) if project.devices else 'unknown'}",
                f"- Runtimes: {', '.join(project.runtimes) if project.runtimes else 'unknown'}",
                f"- Sessions: {project.session_count}",
                f"- Latest activity: {project.latest_at or 'unknown'}",
            ]
        )
        if page:
            lines.append(f"- Vault: `{page}`")
        lines.append("")
        if project.summaries:
            lines.append("Recent:")
            lines.extend(f"- {summary}" for summary in project.summaries)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"

