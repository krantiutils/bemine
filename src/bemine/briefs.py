from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .backlog import BacklogEntry, add_backlog_item, list_backlog_items
from .capabilities import detect_tool_capabilities
from .config import BemineSettings, config_int, config_list
from .db import BemineDB
from .index.status import SessionStatus, _redact, collect_fleet_status
from .index.vault import ObsidianVault
from .util import compact_text


DEFAULT_BRIEF_LIMIT = 80
MAX_ITEMS_PER_OWNER = 8
STATUS_BOILERPLATE_RE = re.compile(
    r"\s*latest (?:agent turn|interaction).*$",
    re.IGNORECASE,
)
PARTIAL_QUESTION_RE = re.compile(r'"question"\s*:\s*"([^"]+)', re.IGNORECASE)
PROJECT_NAME_RE = re.compile(r"\b([A-Za-z0-9][A-Za-z0-9_.-]+-(?:toolkit|bench|better|core|tests))\b")


@dataclass(frozen=True)
class BriefItem:
    owner: str
    text: str
    project: str = ""
    evidence: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "owner": self.owner,
            "text": self.text,
            "project": self.project,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class BriefResult:
    path: Path
    generated_at: str
    items: list[BriefItem]
    backlog_entries: list[BacklogEntry]
    evidence: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "generated_at": self.generated_at,
            "items": [item.to_dict() for item in self.items],
            "backlog_entries": [entry.to_dict() for entry in self.backlog_entries],
            "evidence": self.evidence,
        }


def run_brief(
    settings: BemineSettings,
    *,
    limit: int = DEFAULT_BRIEF_LIMIT,
    record_backlog: bool = True,
    db: BemineDB | None = None,
    statuses: list[SessionStatus] | None = None,
    inbound: list[dict[str, Any]] | None = None,
    outbound: list[dict[str, Any]] | None = None,
    audits: list[dict[str, Any]] | None = None,
    tools: Mapping[str, Any] | None = None,
    generated_at: datetime | None = None,
) -> BriefResult:
    db = db or BemineDB(settings.db_path)
    generated_at = generated_at or datetime.now(timezone.utc)
    statuses = statuses if statuses is not None else _collect_statuses(settings)
    inbound = inbound if inbound is not None else _recent_rows(db, "inbound_messages", "received_at", limit=limit)
    outbound = outbound if outbound is not None else _recent_rows(db, "outbound_messages", "sent_at", limit=limit)
    audits = audits if audits is not None else [dict(row) for row in db.list_audit_events(limit=limit)]
    tools = dict(tools) if tools is not None else detect_tool_capabilities()
    items = build_brief_items(
        statuses=statuses,
        inbound=inbound,
        outbound=outbound,
        audits=audits,
        tools=tools,
        limit=limit,
    )
    path = brief_path(settings, generated_at=generated_at)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_brief_markdown(
            generated_at=generated_at.isoformat(),
            items=items,
            evidence={
                "statuses": len(statuses),
                "inbound_messages": len(inbound),
                "outbound_messages": len(outbound),
                "audit_events": len(audits),
            },
        ),
        encoding="utf-8",
    )
    backlog_entries: list[BacklogEntry] = []
    if record_backlog:
        backlog_entries = _record_new_backlog_items(settings, db=db, items=items)
    db.record_audit_event(
        category="brief",
        event_type="pa_smm_brief_recorded",
        actor="brief",
        device_id=settings.device_id,
        payload_json=json.dumps(
            {
                "path": str(path),
                "items": len(items),
                "backlog_entries": len(backlog_entries),
                "pa_items": sum(1 for item in items if item.owner == "PA"),
                "smm_items": sum(1 for item in items if item.owner == "SMM"),
            },
            sort_keys=True,
        ),
    )
    return BriefResult(
        path=path,
        generated_at=generated_at.isoformat(),
        items=items,
        backlog_entries=backlog_entries,
        evidence={
            "statuses": len(statuses),
            "inbound_messages": len(inbound),
            "outbound_messages": len(outbound),
            "audit_events": len(audits),
        },
    )


def build_brief_items(
    *,
    statuses: Iterable[SessionStatus],
    inbound: Iterable[Mapping[str, Any]],
    outbound: Iterable[Mapping[str, Any]],
    audits: Iterable[Mapping[str, Any]],
    tools: Mapping[str, Any],
    limit: int = DEFAULT_BRIEF_LIMIT,
) -> list[BriefItem]:
    items: list[BriefItem] = []
    seen: set[tuple[str, str]] = set()
    _add_android_items(items, seen, tools)
    for status in statuses:
        if status.asks_user:
            ask = _clean_evidence_text(status.ask_summary)
            _add_item(
                items,
                seen,
                BriefItem(
                    owner="PA",
                    text=(
                        f"Follow up on {status.device_id} {status.runtime} "
                        f"`{status.cwd}`: {compact_text(ask, limit=180)}"
                    ),
                    project=_project_from_cwd(status.cwd),
                    evidence=f"session:{status.session_id}",
                ),
            )
        summary = _clean_evidence_text(
            " ".join([status.latest_summary or "", status.ask_summary or ""])
        )
        if _is_smm_status_candidate(status, summary):
            _add_item(
                items,
                seen,
                BriefItem(
                    owner="SMM",
                    text=(
                        f"Draft a project update from `{_project_from_cwd(status.cwd)}`: "
                        f"{compact_text(summary, limit=220)}"
                    ),
                    project=_project_from_cwd(status.cwd),
                    evidence=f"session:{status.session_id}",
                ),
            )
    for row in outbound:
        content = compact_text(_clean_evidence_text(str(row.get("content") or "")), limit=260)
        if not content:
            continue
        if _is_failure(content):
            _add_item(
                items,
                seen,
                BriefItem(
                    owner="PA",
                    text=f"Review recent bot failure/blocker: {content}",
                    evidence=f"outbound:{row.get('discord_message_id') or row.get('id') or ''}",
                ),
            )
        project = _project_from_text(content)
        if project and _is_smm_worthy(content):
            _add_item(
                items,
                seen,
                BriefItem(
                    owner="SMM",
                    text=f"Turn this progress into a social post idea: {content}",
                    project=project,
                    evidence=f"outbound:{row.get('discord_message_id') or row.get('id') or ''}",
                ),
            )

    for row in inbound:
        content = compact_text(_clean_evidence_text(str(row.get("content") or "")), limit=220)
        if "focus" in content.casefold() or "today" in content.casefold():
            _add_item(
                items,
                seen,
                BriefItem(
                    owner="PA",
                    text=f"Consider for today's focus: {content}",
                    evidence=f"inbound:{row.get('message_id') or ''}",
                ),
            )

    for row in audits:
        category = str(row.get("category") or "")
        event_type = str(row.get("event_type") or "")
        if category == "recovery" and "escalate" in event_type:
            _add_item(
                items,
                seen,
                BriefItem(
                    owner="PA",
                    text="Review auto-recovery escalation and decide whether the session needs a human answer.",
                    project=str(row.get("project_id") or ""),
                    evidence=f"audit:{row.get('id') or ''}",
                ),
            )

    return _cap_owner_items(items, per_owner=MAX_ITEMS_PER_OWNER)[: max(1, limit)]


def render_brief(result: BriefResult) -> str:
    pa_count = sum(1 for item in result.items if item.owner == "PA")
    smm_count = sum(1 for item in result.items if item.owner == "SMM")
    return "\n".join(
        [
            "PA/SMM brief recorded:",
            f"- path: `{result.path}`",
            f"- items: PA={pa_count} SMM={smm_count}",
            f"- backlog entries added: {len(result.backlog_entries)}",
            "",
        ]
    )


def render_brief_markdown(
    *,
    generated_at: str,
    items: list[BriefItem],
    evidence: Mapping[str, int],
) -> str:
    lines = [
        "# PA/SMM Brief",
        "",
        f"Generated: `{generated_at}`",
        "",
        "## PA",
    ]
    _extend_owner_lines(lines, items, owner="PA")
    lines.extend(["", "## SMM"])
    _extend_owner_lines(lines, items, owner="SMM")
    lines.extend(
        [
            "",
            "## Evidence",
            "",
            f"- Status rows: {evidence.get('statuses', 0)}",
            f"- Inbound messages: {evidence.get('inbound_messages', 0)}",
            f"- Outbound messages: {evidence.get('outbound_messages', 0)}",
            f"- Audit events: {evidence.get('audit_events', 0)}",
            "",
        ]
    )
    return "\n".join(lines)


def brief_path(settings: BemineSettings, *, generated_at: datetime | None = None) -> Path:
    generated_at = generated_at or datetime.now(timezone.utc)
    ObsidianVault(settings.vault_dir)
    return settings.vault_dir / "Briefs" / f"{generated_at.date().isoformat()}-pa-smm.md"


def _record_new_backlog_items(
    settings: BemineSettings,
    *,
    db: BemineDB,
    items: list[BriefItem],
) -> list[BacklogEntry]:
    entries: list[BacklogEntry] = []
    existing = {
        owner: _normalize_backlog_text(
            "\n".join(list_backlog_items(settings, owner=owner, limit=200))
        )
        for owner in ("PA", "SMM")
    }
    for item in items:
        normalized = _normalize_backlog_text(item.text)
        if _looks_duplicate(normalized, existing[item.owner]):
            continue
        entry = add_backlog_item(
            settings,
            owner=item.owner,
            text=item.text,
            source="brief",
            project=item.project,
            db=db,
        )
        entries.append(entry)
        existing[item.owner] += "\n" + normalized
    return entries


def _collect_statuses(settings: BemineSettings) -> list[SessionStatus]:
    devices = config_list("brief", "devices", settings.status_devices)
    limit = config_int("brief", "status_limit", 12)
    timeout = config_int("brief", "timeout_secs", 6)
    try:
        statuses, _errors = collect_fleet_status(
            settings,
            devices=devices,
            limit_per_runtime=limit,
            timeout=timeout,
        )
    except Exception:
        return []
    return statuses


def _recent_rows(
    db: BemineDB,
    table: str,
    timestamp_column: str,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    try:
        with db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM {table}
                ORDER BY {timestamp_column} DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(row) for row in rows]


def _add_android_items(
    items: list[BriefItem],
    seen: set[tuple[str, str]],
    tools: Mapping[str, Any],
) -> None:
    adb = tools.get("adb") if isinstance(tools.get("adb"), Mapping) else {}
    for device in adb.get("devices") or []:
        if str(device.get("state") or "") == "unauthorized":
            serial = str(device.get("serial") or "attached device")
            _add_item(
                items,
                seen,
                BriefItem(
                    owner="PA",
                    text=(
                        f"Authorize Android USB debugging for `{serial}` so Phantom "
                        "phone automation can install/run the accessibility service."
                    ),
                    project="phantom",
                    evidence="capabilities:adb",
                ),
            )


def _add_item(items: list[BriefItem], seen: set[tuple[str, str]], item: BriefItem) -> None:
    key = (item.owner, item.text.casefold())
    if key in seen:
        return
    seen.add(key)
    items.append(item)


def _extend_owner_lines(lines: list[str], items: list[BriefItem], *, owner: str) -> None:
    owner_items = [item for item in items if item.owner == owner]
    if not owner_items:
        lines.append("- No generated items.")
        return
    for item in owner_items:
        suffix = f" _(project: {item.project})_" if item.project else ""
        lines.append(f"- [ ] {item.text}{suffix}")


def _is_failure(text: str) -> bool:
    haystack = text.casefold()
    return any(word in haystack for word in ("failed", "blocked", "crashed", "unauthorized"))


def _is_smm_worthy(text: str) -> bool:
    haystack = text.casefold()
    if not _is_public_story_candidate(haystack):
        return False
    return any(
        word in haystack
        for word in (
            "shipped",
            "deployed",
            "checkpoint",
            "benchmark",
            "eval",
            "training status",
            "final report",
            "passed",
            "released",
            "done",
            "chr",
            "bleu",
        )
    )


def _is_smm_status_candidate(status: SessionStatus, text: str) -> bool:
    if status.asks_user or not _is_project_cwd(status.cwd):
        return False
    return _is_smm_worthy(text)


def _is_public_story_candidate(haystack: str) -> bool:
    reject = (
        '{"kind"',
        "dispatch failed",
        "blocked:",
        "crashed",
        "not done",
        "stopped.",
        "want me",
        "should i",
        "waiting for explicit approval",
        "aws sso",
        "private notion",
        "sshpass",
        "password",
        "watcher is now active",
        "agent is idle",
        "dispatch.runtime",
        "codex.model",
        "restart needed",
        "branch `agent/",
        "tests pass",
    )
    return not any(term in haystack for term in reject)


def _is_project_cwd(cwd: str) -> bool:
    if not cwd:
        return False
    path = Path(cwd).expanduser()
    name = path.name.casefold()
    if name in {"", "cdjk", "tmp", "home"}:
        return False
    return str(path) != str(Path.home())


def _cap_owner_items(items: list[BriefItem], *, per_owner: int) -> list[BriefItem]:
    counts: dict[str, int] = {}
    capped: list[BriefItem] = []
    for item in items:
        count = counts.get(item.owner, 0)
        if count >= per_owner:
            continue
        counts[item.owner] = count + 1
        capped.append(item)
    return capped


def _clean_evidence_text(text: str) -> str:
    cleaned = _redact(text).strip()
    if cleaned.startswith("{"):
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            cleaned = str(parsed.get("question") or parsed.get("rationale") or cleaned)
        else:
            match = PARTIAL_QUESTION_RE.search(cleaned)
            if match:
                cleaned = match.group(1)
    cleaned = STATUS_BOILERPLATE_RE.sub("", cleaned).strip()
    cleaned = cleaned.rstrip('"}')
    return cleaned


def _project_from_text(text: str) -> str:
    match = PROJECT_NAME_RE.search(text)
    return match.group(1) if match else ""


def _normalize_backlog_text(text: str) -> str:
    words = re.sub(r"[^a-z0-9]+", " ", text.casefold()).split()
    return " ".join(word for word in words if word not in {"device"})


def _looks_duplicate(normalized: str, existing_normalized: str) -> bool:
    if not normalized:
        return True
    if normalized in existing_normalized:
        return True
    words = normalized.split()
    if len(words) >= 6 and " ".join(words[:6]) in existing_normalized:
        return True
    return False


def _project_from_cwd(cwd: str) -> str:
    if not cwd:
        return ""
    return Path(cwd).name or cwd
