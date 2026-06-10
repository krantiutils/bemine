from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import BemineSettings
from .db import BemineDB
from .index.status import _redact
from .util import compact_text


@dataclass(frozen=True)
class DreamResult:
    path: Path
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "data": self.data}


def run_dream(
    settings: BemineSettings,
    *,
    limit: int = 80,
    db: BemineDB | None = None,
) -> DreamResult:
    db = db or BemineDB(settings.db_path)
    inbound = _recent_rows(db, "inbound_messages", "received_at", limit=limit)
    outbound = _recent_rows(db, "outbound_messages", "sent_at", limit=limit)
    audits = _recent_audits(db, limit=min(limit, 80))
    data = build_dream_datapoint(
        inbound=inbound,
        outbound=outbound,
        audits=audits,
        generated_at=datetime.now(timezone.utc),
    )
    path = dream_log_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(data, sort_keys=True) + "\n")
    db.record_audit_event(
        category="dream",
        event_type="dream_recorded",
        actor="dream",
        device_id=settings.device_id,
        payload_json=json.dumps(
            {
                "path": str(path),
                "inbound": len(inbound),
                "outbound": len(outbound),
                "signals": len(data["signals"]),
                "friction": len(data["friction"]),
            },
            sort_keys=True,
        ),
    )
    return DreamResult(path=path, data=data)


def build_dream_datapoint(
    *,
    inbound: list[dict[str, Any]],
    outbound: list[dict[str, Any]],
    audits: list[dict[str, Any]],
    generated_at: datetime,
) -> dict[str, Any]:
    user_messages = [
        compact_text(_redact(str(row.get("content") or "")), limit=220)
        for row in inbound
        if str(row.get("content") or "").strip()
    ]
    bot_failures = [
        compact_text(_redact(str(row.get("content") or "")), limit=220)
        for row in outbound
        if "failed" in str(row.get("content") or "").casefold()
        or "blocked" in str(row.get("content") or "").casefold()
    ]
    audit_counts: dict[str, int] = {}
    for row in audits:
        key = f"{row.get('category')}/{row.get('event_type')}"
        audit_counts[key] = audit_counts.get(key, 0) + 1
    signals = _signals_from_messages(user_messages)
    friction = _friction_from_messages(user_messages, bot_failures)
    return {
        "schema": "bemine.dream.v1",
        "generated_at": generated_at.isoformat(),
        "window": {
            "inbound_messages": len(inbound),
            "outbound_messages": len(outbound),
            "audit_events": len(audits),
        },
        "signals": signals[:12],
        "friction": friction[:12],
        "recent_user_requests": user_messages[:20],
        "recent_failures": bot_failures[:12],
        "audit_counts": dict(sorted(audit_counts.items())),
    }


def render_dream(result: DreamResult) -> str:
    data = result.data
    lines = [
        "Dream recorded:",
        f"- path: `{result.path}`",
        f"- window: inbound={data['window']['inbound_messages']} "
        f"outbound={data['window']['outbound_messages']} "
        f"audit={data['window']['audit_events']}",
    ]
    if data["signals"]:
        lines.append("- signals:")
        lines.extend(f"  - {item}" for item in data["signals"])
    if data["friction"]:
        lines.append("- friction:")
        lines.extend(f"  - {item}" for item in data["friction"])
    return "\n".join(lines) + "\n"


def dream_log_path(settings: BemineSettings) -> Path:
    return settings.vault_dir / "Dreams" / "dreams.jsonl"


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


def _recent_audits(db: BemineDB, *, limit: int) -> list[dict[str, Any]]:
    return [dict(row) for row in db.list_audit_events(limit=limit)]


def _signals_from_messages(messages: list[str]) -> list[str]:
    signals: list[str] = []
    haystack = "\n".join(messages).casefold()
    if any(word in haystack for word in ("goal ", "watch", "training", "train ")):
        signals.append("Users expect long-running project work to start, continue, and report back autonomously.")
    if any(word in haystack for word in ("model", "runtime", "using right now")):
        signals.append("Runtime/model identity must be self-documenting and authoritative.")
    if any(word in haystack for word in ("idea", "post", "social", "smm")):
        signals.append("Social/post ideas need durable backlog capture before they become tasks.")
    if any(word in haystack for word in ("status", "what are you doing", "latest")):
        signals.append("Status answers should lead with live evidence from sessions, jobs, and audit state.")
    if any(word in haystack for word in ("line cut", "power", "restart", "cut off")):
        signals.append("Managed work on cdjk needs continuity across line cuts and reboots.")
    return signals


def _friction_from_messages(messages: list[str], failures: list[str]) -> list[str]:
    friction: list[str] = []
    haystack = "\n".join([*messages, *failures]).casefold()
    if "maximum number of turns" in haystack:
        friction.append("Meridian/proxy turn caps can fail Discord dispatch; prefer short tool paths for ops.")
    if "not visible" in haystack or "isn't visible" in haystack:
        friction.append("Invisible long-running work needs progress reporting and inspectable worker state.")
    if "no such file or directory" in haystack and "codex" in haystack:
        friction.append("Systemd PATH must use absolute runtime binaries.")
    if "bwrap" in haystack or "loopback" in haystack:
        friction.append("Codex workspace sandbox is unreliable under this systemd environment.")
    if "didnt press enter" in haystack or "didn't press enter" in haystack:
        friction.append("Tmux input must verify that submitted text actually reached the pane.")
    return friction
