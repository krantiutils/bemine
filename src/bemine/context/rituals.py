from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..config import BemineSettings
from ..db import BemineDB
from .harness_context import load_harness_context
from .operator_context import load_operator_context
from .subagents import load_subagent_context
from ..index.rundown import render_rundown
from ..index.status import (
    SessionStatus,
    collect_fleet_status,
    collect_local_status,
    render_status,
)
from ..util import compact_text, project_id_from_cwd, project_name_from_cwd
from ..index.vault import ObsidianVault


@dataclass(frozen=True)
class PrimePacket:
    generated_at: str
    device_id: str
    cwd: str
    project_id: str
    project_name: str
    harness_files: list[str]
    operator_files: list[str]
    subagent_files: list[str]
    projects: list[dict]
    statuses: list[dict]
    errors: list[dict]
    counts: dict[str, int]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class HandoffResult:
    path: Path
    project_id: str
    project_name: str
    summary: str

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "project_id": self.project_id,
            "project_name": self.project_name,
            "summary": self.summary,
        }


def build_prime_packet(
    settings: BemineSettings,
    *,
    cwd: Path | None = None,
    limit: int = 8,
    fleet: bool = False,
) -> PrimePacket:
    cwd = (cwd or Path.cwd()).expanduser().resolve()
    db = BemineDB(settings.db_path)
    statuses, errors = (
        collect_fleet_status(settings, limit_per_runtime=max(2, min(limit, 12)))
        if fleet
        else (
            collect_local_status(
                device_id=settings.device_id,
                codex_home=settings.codex_home,
                claude_home=settings.claude_home,
                kimi_home=settings.kimi_home,
                limit_per_runtime=max(2, min(limit, 12)),
            ),
            [],
        )
    )
    projects = [dict_project(row) for row in db.rundown(limit=limit)]
    harness = load_harness_context()
    operator = load_operator_context(limit=1_000)
    subagents = load_subagent_context(limit=2_000)
    return PrimePacket(
        generated_at=datetime.now(timezone.utc).isoformat(),
        device_id=settings.device_id,
        cwd=str(cwd),
        project_id=project_id_from_cwd(str(cwd)),
        project_name=project_name_from_cwd(str(cwd)),
        harness_files=[str(path) for path in harness.files],
        operator_files=[str(path) for path in operator.files],
        subagent_files=[str(profile.path) for profile in subagents.profiles],
        projects=projects,
        statuses=[status.to_dict() for status in statuses[:limit]],
        errors=[error.__dict__ for error in errors],
        counts=db.counts(),
    )


def render_prime_packet(packet: PrimePacket) -> str:
    lines = [
        "# Bemine Prime",
        "",
        f"Generated: {packet.generated_at}",
        f"Device: `{packet.device_id}`",
        f"CWD: `{packet.cwd}`",
        f"Current project: `{packet.project_name}` (`{packet.project_id}`)",
        "",
        "## Required Context",
        "",
    ]
    if packet.harness_files:
        lines.extend(f"- Harness: `{path}`" for path in packet.harness_files)
    else:
        lines.append("- Harness: no `SOUL.md`/`SKILL.md` found")
    if packet.operator_files:
        lines.extend(f"- Operator context: `{path}`" for path in packet.operator_files)
    else:
        lines.append("- Operator context: none found")
    if packet.subagent_files:
        lines.extend(f"- Subagent: `{path}`" for path in packet.subagent_files)
    else:
        lines.append("- Subagents: none configured")
    lines.extend(
        [
            "",
            "## Operating Rules",
            "",
            "- Read `SOUL.md` and `SKILL.md` before changing harness behavior.",
            "- Use last interaction, not process presence, when reporting status.",
            "- Use `bemine capabilities` before assuming credential access.",
            "- Gated credentials require `bemine permissions ask ...`; never print secrets.",
            "- End substantial work with `bemine handoff`.",
            "",
            "## Recent Sessions",
            "",
        ]
    )
    statuses = [SessionStatus(**row) for row in packet.statuses]
    errors = []
    if packet.errors:
        from ..index.status import StatusError

        errors = [StatusError(**row) for row in packet.errors]
    lines.append(render_status(statuses, errors, limit=len(statuses)).rstrip())
    lines.extend(
        [
            "",
            "## Project Rundown",
            "",
            render_rundown_from_packet(packet).rstrip(),
            "",
            "## Next Commands",
            "",
            "- `uv run bemine status --limit 12`",
            "- `uv run bemine rundown --limit 12`",
            "- `uv run bemine capabilities`",
            "- `uv run bemine handoff \"<summary>\" --next \"<next step>\"`",
            "",
        ]
    )
    return "\n".join(lines)


def render_rundown_from_packet(packet: PrimePacket) -> str:
    from ..index.models import ProjectRundown

    projects = [
        ProjectRundown(
            project_id=str(row["project_id"]),
            name=str(row["name"]),
            cwd=str(row["cwd"]),
            devices=list(row.get("devices") or []),
            latest_at=str(row.get("latest_at") or ""),
            session_count=int(row.get("session_count") or 0),
            runtimes=list(row.get("runtimes") or []),
            summaries=list(row.get("summaries") or []),
        )
        for row in packet.projects
    ]
    rendered = render_rundown(projects)
    lines = rendered.splitlines()
    if lines[:1] == ["# Project Rundown"]:
        return "\n".join(lines[4:]).strip() + "\n"
    return rendered


def record_handoff(
    settings: BemineSettings,
    *,
    summary: str,
    cwd: Path | None = None,
    project_name: str = "",
    session_id: str = "",
    runtime: str = "",
    status: str = "in_progress",
    next_steps: list[str] | None = None,
    blockers: list[str] | None = None,
    tests: list[str] | None = None,
    actor: str = "cli",
) -> HandoffResult:
    cwd = (cwd or Path.cwd()).expanduser().resolve()
    project_name = project_name.strip() or project_name_from_cwd(str(cwd))
    project_id = project_id_from_cwd(str(cwd))
    now = datetime.now(timezone.utc)
    summary = summary.strip() or "No summary provided."
    next_steps = [item.strip() for item in (next_steps or []) if item.strip()]
    blockers = [item.strip() for item in (blockers or []) if item.strip()]
    tests = [item.strip() for item in (tests or []) if item.strip()]

    vault = ObsidianVault(settings.vault_dir)
    markdown = _render_handoff_markdown(
        generated_at=now.isoformat(),
        project_name=project_name,
        project_id=project_id,
        cwd=str(cwd),
        session_id=session_id,
        runtime=runtime,
        status=status,
        summary=summary,
        next_steps=next_steps,
        blockers=blockers,
        tests=tests,
    )
    path = vault.write_handoff(project_name=project_name, text=markdown, when=now)
    vault.append_daily_entry(
        when=now,
        title=f"Handoff: {project_name}",
        text=f"- Summary: {compact_text(summary, limit=500)}\n- File: `{path}`",
    )

    db = BemineDB(settings.db_path)
    db.record_audit_event(
        category="ritual",
        event_type="handoff_recorded",
        actor=actor,
        device_id=settings.device_id,
        project_id=project_id,
        session_id=session_id or None,
        payload_json=json.dumps(
            {
                "path": str(path),
                "project_name": project_name,
                "runtime": runtime,
                "status": status,
                "summary": compact_text(summary, limit=500),
                "next_steps": next_steps,
                "blockers": blockers,
                "tests": tests,
            },
            sort_keys=True,
        ),
    )
    return HandoffResult(
        path=path,
        project_id=project_id,
        project_name=project_name,
        summary=summary,
    )


def summary_from_args(parts: list[str], *, read_stdin: bool = False) -> str:
    text = " ".join(parts).strip()
    if read_stdin:
        stdin_text = sys.stdin.read().strip()
        if stdin_text:
            text = f"{text}\n\n{stdin_text}".strip() if text else stdin_text
    return text


def dict_project(project: object) -> dict:
    return {
        "project_id": getattr(project, "project_id"),
        "name": getattr(project, "name"),
        "cwd": getattr(project, "cwd"),
        "devices": list(getattr(project, "devices")),
        "latest_at": getattr(project, "latest_at"),
        "session_count": getattr(project, "session_count"),
        "runtimes": list(getattr(project, "runtimes")),
        "summaries": list(getattr(project, "summaries")),
    }


def _render_handoff_markdown(
    *,
    generated_at: str,
    project_name: str,
    project_id: str,
    cwd: str,
    session_id: str,
    runtime: str,
    status: str,
    summary: str,
    next_steps: list[str],
    blockers: list[str],
    tests: list[str],
) -> str:
    lines = [
        f"# Handoff - {project_name}",
        "",
        f"- Generated: {generated_at}",
        f"- Project ID: `{project_id}`",
        f"- CWD: `{cwd}`",
        f"- Runtime: {runtime or 'unknown'}",
        f"- Session: `{session_id or 'unknown'}`",
        f"- Status: {status}",
        "",
        "## Summary",
        "",
        summary,
        "",
        "## Next Steps",
        "",
    ]
    if next_steps:
        lines.extend(f"- {item}" for item in next_steps)
    else:
        lines.append("- None recorded.")
    lines.extend(["", "## Blockers", ""])
    if blockers:
        lines.extend(f"- {item}" for item in blockers)
    else:
        lines.append("- None recorded.")
    lines.extend(["", "## Verification", ""])
    if tests:
        lines.extend(f"- `{item}`" for item in tests)
    else:
        lines.append("- Not recorded.")
    lines.append("")
    return "\n".join(lines)
