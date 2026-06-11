from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import BemineSettings, config_list, config_string
from .db import BemineDB
from .index.vault import ObsidianVault
from .util import compact_text, safe_filename


DEFAULT_BACKLOG_OWNERS = ["PA", "SMM"]


@dataclass(frozen=True)
class BacklogEntry:
    owner: str
    text: str
    path: Path
    created_at: str
    source: str = ""
    project: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "owner": self.owner,
            "text": self.text,
            "path": str(self.path),
            "created_at": self.created_at,
            "source": self.source,
            "project": self.project,
        }


def backlog_owners() -> list[str]:
    owners = config_list("backlog", "owners", DEFAULT_BACKLOG_OWNERS)
    return [owner.upper() for owner in owners] or list(DEFAULT_BACKLOG_OWNERS)


def default_backlog_owner() -> str:
    return config_string("backlog", "default_owner", "SMM").strip().upper() or "SMM"


def add_backlog_item(
    settings: BemineSettings,
    *,
    owner: str,
    text: str,
    source: str = "cli",
    project: str = "",
    db: BemineDB | None = None,
    when: datetime | None = None,
) -> BacklogEntry:
    owner = _normalize_owner(owner)
    text = " ".join(text.split())
    if not text:
        raise ValueError("backlog text is required")
    when = when or datetime.now(timezone.utc)
    vault = ObsidianVault(settings.vault_dir)
    path = backlog_path(settings, owner)
    existing = (
        path.read_text(encoding="utf-8") if path.exists() else _initial_backlog(owner)
    )
    created_at = when.isoformat()
    meta = " ".join(
        part
        for part in [
            f"source={_inline_value(source)}" if source else "",
            f"project={_inline_value(project)}" if project else "",
        ]
        if part
    )
    suffix = f" <!-- {meta} -->" if meta else ""
    line = f"- [ ] {created_at} — {text}{suffix}"
    path.write_text(existing.rstrip() + "\n" + line + "\n", encoding="utf-8")
    entry = BacklogEntry(
        owner=owner,
        text=text,
        path=path,
        created_at=created_at,
        source=source,
        project=project,
    )
    (db or BemineDB(settings.db_path)).record_audit_event(
        category="backlog",
        event_type="backlog_item_added",
        actor=source or "cli",
        device_id=settings.device_id,
        project_id=project or None,
        payload_json=json.dumps(
            {
                "owner": owner,
                "path": str(path),
                "text": compact_text(text, limit=500),
                "source": source,
                "project": project,
            },
            sort_keys=True,
        ),
    )
    return entry


def list_backlog_items(
    settings: BemineSettings,
    *,
    owner: str,
    limit: int = 20,
) -> list[str]:
    owner = _normalize_owner(owner)
    path = backlog_path(settings, owner)
    if not path.exists():
        return []
    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip().startswith("- [ ] ")
    ]
    return lines[-max(1, limit) :]


def backlog_status(settings: BemineSettings, *, owner: str = "") -> dict:
    owners = [_normalize_owner(owner)] if owner else backlog_owners()
    rows = [_backlog_owner_status(settings, item) for item in owners]
    return {
        "schema": "bemine.backlog_status.v1",
        "default_owner": default_backlog_owner(),
        "owners": rows,
        "total_open": sum(int(row["open_count"]) for row in rows),
    }


def render_backlog_items(owner: str, items: list[str]) -> str:
    owner = owner.upper()
    if not items:
        return f"No open {owner} backlog items.\n"
    return f"{owner} backlog:\n" + "\n".join(items) + "\n"


def render_backlog_status(status: dict) -> str:
    lines = [
        "Backlog status:",
        f"- default_owner: {status.get('default_owner') or 'none'}",
        f"- total_open: {status.get('total_open', 0)}",
    ]
    for row in status.get("owners", []):
        state = "present" if row.get("exists") else "missing"
        latest = row.get("latest_created_at") or "none"
        preview = row.get("latest_preview") or "none"
        lines.append(
            f"- {row.get('owner')}: {row.get('open_count', 0)} open "
            f"({state}) latest={latest} path={row.get('path')}"
        )
        if preview != "none":
            lines.append(f"  latest_item: {preview}")
    return "\n".join(lines) + "\n"


def backlog_path(settings: BemineSettings, owner: str) -> Path:
    owner = _normalize_owner(owner)
    return settings.vault_dir / "Backlog" / f"{safe_filename(owner, 'backlog')}.md"


def _backlog_owner_status(settings: BemineSettings, owner: str) -> dict:
    owner = _normalize_owner(owner)
    path = backlog_path(settings, owner)
    lines = _open_backlog_lines(path)
    latest_line = lines[-1] if lines else ""
    latest_created_at, latest_text = _parse_backlog_line(latest_line)
    mtime = ""
    if path.exists():
        mtime = datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        ).isoformat()
    return {
        "owner": owner,
        "path": str(path),
        "exists": path.exists(),
        "open_count": len(lines),
        "latest_created_at": latest_created_at,
        "latest_preview": compact_text(latest_text, limit=160) if latest_text else "",
        "mtime": mtime,
    }


def _open_backlog_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip().startswith("- [ ] ")
    ]


def _parse_backlog_line(line: str) -> tuple[str, str]:
    if not line.startswith("- [ ] "):
        return "", ""
    body = line.removeprefix("- [ ] ").strip()
    if " — " in body:
        created_at, text = body.split(" — ", 1)
    else:
        created_at, text = "", body
    text = text.split("<!--", 1)[0].strip()
    return created_at.strip(), text


def _normalize_owner(owner: str) -> str:
    owner = (owner or default_backlog_owner()).strip().upper()
    owners = backlog_owners()
    if owner not in owners:
        raise ValueError(
            f"unknown backlog owner {owner!r}; configured owners: {', '.join(owners)}"
        )
    return owner


def _initial_backlog(owner: str) -> str:
    return f"# {owner} Backlog\n\nOpen ideas and tasks captured by Bemine.\n"


def _inline_value(value: str) -> str:
    return " ".join(str(value).split()).replace("--", "-")
