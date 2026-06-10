from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .backlog import list_backlog_items
from .config import BemineSettings
from .db import BemineDB
from .health import collect_health
from .index.status import SessionStatus, _redact, collect_fleet_status
from .index.vault import ObsidianVault
from .util import compact_text, safe_filename


DEFAULT_PA_PLAN_LIMIT = 6


@dataclass(frozen=True)
class PAPlanItem:
    priority: int
    text: str
    next_action: str
    evidence: str = ""
    project: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "priority": self.priority,
            "text": self.text,
            "next_action": self.next_action,
            "evidence": self.evidence,
            "project": self.project,
        }


@dataclass(frozen=True)
class PAPlanResult:
    generated_at: str
    items: list[PAPlanItem]
    evidence: dict[str, int]
    path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "items": [item.to_dict() for item in self.items],
            "evidence": self.evidence,
            "path": str(self.path) if self.path else "",
        }


def build_pa_plan(
    settings: BemineSettings,
    *,
    limit: int = DEFAULT_PA_PLAN_LIMIT,
    write: bool = False,
    db: BemineDB | None = None,
    statuses: list[SessionStatus] | None = None,
    health: Mapping[str, Any] | None = None,
    audits: list[Mapping[str, Any]] | None = None,
    generated_at: datetime | None = None,
) -> PAPlanResult:
    db = db or BemineDB(settings.db_path)
    generated_at = generated_at or datetime.now(timezone.utc)
    if statuses is None:
        try:
            statuses, _errors = collect_fleet_status(settings, limit_per_runtime=8, timeout=6)
        except Exception:
            statuses = []
    health = dict(health) if health is not None else collect_health(settings, db=db)
    audits = audits if audits is not None else [dict(row) for row in db.list_audit_events(limit=30)]
    backlog = list_backlog_items(settings, owner="PA", limit=20)
    watchers = list(health.get("watchers", {}).get("items") or [])
    jobs = health.get("jobs") if isinstance(health.get("jobs"), Mapping) else {}

    items: list[PAPlanItem] = []
    _add_health_items(items, health)
    _add_job_items(items, jobs)
    _add_session_asks(items, statuses)
    _add_watcher_items(items, watchers)
    _add_audit_items(items, audits)
    _add_backlog_items(items, backlog)
    items = _dedupe_items(items)
    items.sort(key=lambda item: item.priority)
    items = items[: max(1, limit)]

    path = None
    result = PAPlanResult(
        generated_at=generated_at.isoformat(),
        items=items,
        evidence={
            "statuses": len(statuses),
            "health_issues": len(health.get("issues") or []),
            "active_watchers": len(watchers),
            "audit_events": len(audits),
            "pa_backlog_items": len(backlog),
        },
    )
    if write:
        path = pa_plan_path(settings, generated_at=generated_at)
        path.parent.mkdir(parents=True, exist_ok=True)
        result = PAPlanResult(
            generated_at=result.generated_at,
            items=result.items,
            evidence=result.evidence,
            path=path,
        )
        path.write_text(render_pa_plan_markdown(result), encoding="utf-8")
        db.record_audit_event(
            category="pa",
            event_type="pa_plan_recorded",
            actor="pa",
            device_id=settings.device_id,
            payload_json=json.dumps(
                {
                    "path": str(path),
                    "items": len(items),
                    "evidence": result.evidence,
                },
                sort_keys=True,
            ),
        )
    return result if path is None else PAPlanResult(
        generated_at=result.generated_at,
        items=result.items,
        evidence=result.evidence,
        path=path,
    )


def render_pa_plan(result: PAPlanResult) -> str:
    lines = [
        "PA plan:",
        f"- items: {len(result.items)}",
        f"- evidence: statuses={result.evidence.get('statuses', 0)} "
        f"health_issues={result.evidence.get('health_issues', 0)} "
        f"watchers={result.evidence.get('active_watchers', 0)} "
        f"backlog={result.evidence.get('pa_backlog_items', 0)}",
    ]
    if result.path:
        lines.append(f"- path: `{result.path}`")
    if not result.items:
        lines.append("- No urgent PA items found from current evidence.")
    for index, item in enumerate(result.items, start=1):
        lines.extend(
            [
                "",
                f"{index}. [P{item.priority}] {item.text}",
                f"   next: {item.next_action}",
                f"   evidence: {item.evidence or 'none'}",
            ]
        )
    return "\n".join(lines) + "\n"


def render_pa_plan_markdown(result: PAPlanResult) -> str:
    lines = [
        "# PA Plan",
        "",
        f"Generated: `{result.generated_at}`",
        "",
        "## Focus",
        "",
    ]
    if not result.items:
        lines.append("- No urgent PA items found from current evidence.")
    for item in result.items:
        suffix = f" _(project: {item.project})_" if item.project else ""
        lines.append(f"- [ ] P{item.priority}: {item.text}{suffix}")
        lines.append(f"  - Next: {item.next_action}")
        lines.append(f"  - Evidence: `{item.evidence or 'none'}`")
    lines.extend(
        [
            "",
            "## Evidence",
            "",
            f"- Status rows: {result.evidence.get('statuses', 0)}",
            f"- Health issues: {result.evidence.get('health_issues', 0)}",
            f"- Active watchers: {result.evidence.get('active_watchers', 0)}",
            f"- Audit events: {result.evidence.get('audit_events', 0)}",
            f"- PA backlog items: {result.evidence.get('pa_backlog_items', 0)}",
            "",
        ]
    )
    return "\n".join(lines)


def pa_plan_path(
    settings: BemineSettings,
    *,
    generated_at: datetime | None = None,
) -> Path:
    generated_at = generated_at or datetime.now(timezone.utc)
    ObsidianVault(settings.vault_dir)
    stem = safe_filename(f"{generated_at.date().isoformat()}-pa-plan", "pa-plan")
    return settings.vault_dir / "PA" / "Plans" / f"{stem}.md"


def _add_health_items(items: list[PAPlanItem], health: Mapping[str, Any]) -> None:
    for issue in health.get("issues") or []:
        text = f"Fix Bemine health issue: {compact_text(str(issue), limit=180)}"
        items.append(
            PAPlanItem(
                priority=1,
                text=text,
                next_action="Run health_status, inspect the failing subsystem, and fix or deploy.",
                evidence="health",
            )
        )


def _add_job_items(items: list[PAPlanItem], jobs: Mapping[str, Any]) -> None:
    for job in jobs.get("failed_jobs") or []:
        job_id = str(job.get("job_id") or "job")
        failures = int(job.get("failure_count") or 0)
        items.append(
            PAPlanItem(
                priority=1,
                text=f"Recover failed scheduled job `{job_id}` ({failures} failure(s)).",
                next_action=f"Run job_run for `{job_id}` or inspect audit/jobs output.",
                evidence=f"job:{job_id}",
            )
        )


def _add_session_asks(items: list[PAPlanItem], statuses: Iterable[SessionStatus]) -> None:
    for status in statuses:
        if not status.asks_user:
            continue
        ask = compact_text(
            _clean_status_text(status.ask_summary or status.latest_summary),
            limit=180,
        )
        project = _project_from_cwd(status.cwd)
        items.append(
            PAPlanItem(
                priority=2,
                text=f"Answer waiting {status.device_id} {status.runtime} session for `{project}`: {ask}",
                next_action=_next_action_for_ask(ask),
                evidence=f"session:{status.session_id}",
                project=project,
            )
        )


def _add_watcher_items(items: list[PAPlanItem], watchers: Iterable[Mapping[str, Any]]) -> None:
    for watcher in watchers:
        project = str(watcher.get("project_id") or "")
        if bool(watcher.get("awaiting_reply")):
            priority = 2
            text = f"Watcher for `{project}` is awaiting a reply."
            next_action = "Inspect watchers_status and the project session before nudging."
        else:
            priority = 4
            text = f"Watcher active for `{project}`."
            next_action = "Let it continue unless the project is stale or noisy."
        items.append(
            PAPlanItem(
                priority=priority,
                text=text,
                next_action=next_action,
                evidence=f"watcher:{project}",
                project=project,
            )
        )


def _add_audit_items(items: list[PAPlanItem], audits: Iterable[Mapping[str, Any]]) -> None:
    for row in audits:
        category = str(row.get("category") or "")
        event_type = str(row.get("event_type") or "")
        if category == "recovery" and "escalate" in event_type:
            items.append(
                PAPlanItem(
                    priority=2,
                    text="Review auto-recovery escalation from a recent session.",
                    next_action="Open audit_list for recovery and inspect the affected session.",
                    evidence=f"audit:{row.get('id') or ''}",
                    project=str(row.get("project_id") or ""),
                )
            )


def _add_backlog_items(items: list[PAPlanItem], backlog: Iterable[str]) -> None:
    for line in backlog:
        task = _strip_task_line(line)
        if not task:
            continue
        items.append(
            PAPlanItem(
                priority=5,
                text=f"PA backlog: {compact_text(task, limit=180)}",
                next_action="Either do it now, schedule it, or clarify the missing owner/date.",
                evidence="backlog:PA",
            )
        )


def _dedupe_items(items: list[PAPlanItem]) -> list[PAPlanItem]:
    seen: set[str] = set()
    result: list[PAPlanItem] = []
    for item in items:
        key = f"{item.text.casefold()}|{item.evidence}"
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _strip_task_line(line: str) -> str:
    text = _redact(line).strip()
    text = text.removeprefix("- [ ] ").strip()
    if " — " in text:
        text = text.split(" — ", 1)[1]
    if "<!--" in text:
        text = text.split("<!--", 1)[0].strip()
    return " ".join(text.split())


def _clean_status_text(text: str) -> str:
    cleaned = _redact(text).strip()
    if cleaned.startswith("{"):
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, Mapping):
            cleaned = str(parsed.get("question") or parsed.get("rationale") or cleaned)
    cleaned = cleaned.rstrip('"}').strip()
    return " ".join(cleaned.split())


def _next_action_for_ask(ask: str) -> str:
    haystack = ask.casefold()
    if any(term in haystack for term in ("destructive", "irreversible", "authorize termination")):
        return "Review manually; do not auto-confirm destructive or irreversible action."
    return "Read the session or relay the missing answer through tmux/session tools."


def _project_from_cwd(cwd: str) -> str:
    if not cwd:
        return ""
    return Path(cwd).name or cwd
