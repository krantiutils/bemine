from __future__ import annotations

import json
from dataclasses import dataclass

from .config import BemineSettings
from .db import BemineDB
from .discovery import discover_claude_sessions, discover_codex_sessions, discover_kimi_sessions
from .models import ProjectRundown
from .project_filters import ignored_project_reason
from .rundown import render_rundown
from .vault import ObsidianVault


@dataclass(frozen=True)
class ScanResult:
    sessions: int
    projects: int
    devices: int


class BemineKernel:
    def __init__(self, settings: BemineSettings) -> None:
        self.settings = settings
        self.settings.ensure_dirs()
        self.db = BemineDB(settings.db_path)
        self.vault = ObsidianVault(settings.vault_dir)

    def scan_local(self) -> ScanResult:
        self.db.upsert_device(self.settings.device_id)
        self.db.record_audit_event(
            category="scan",
            event_type="local_scan_started",
            actor="kernel",
            device_id=self.settings.device_id,
            payload_json=json.dumps(
                {
                    "codex_home": str(self.settings.codex_home),
                    "claude_home": str(self.settings.claude_home),
                    "kimi_home": str(self.settings.kimi_home),
                },
                sort_keys=True,
            ),
        )
        records = []
        records.extend(discover_codex_sessions(self.settings.codex_home, device_id=self.settings.device_id))
        records.extend(discover_claude_sessions(self.settings.claude_home, device_id=self.settings.device_id))
        records.extend(discover_kimi_sessions(self.settings.kimi_home, device_id=self.settings.device_id))
        ignored_sessions = [
            record for record in records if ignored_project_reason(record.cwd)
        ]
        records = [
            record for record in records if not ignored_project_reason(record.cwd)
        ]
        for record in records:
            self.db.upsert_session(record)
        ignored_project_ids = [
            str(project["project_id"])
            for project in self.db.list_projects()
            if ignored_project_reason(str(project["cwd"]), name=str(project["name"]))
        ]
        purged_projects = self.db.delete_projects(ignored_project_ids)
        projects = self.db.rundown(limit=200)
        for project in projects:
            page = self.vault.update_project_page(project)
            project.vault_page = page
        counts = self.db.counts()
        result = ScanResult(
            sessions=counts["sessions"],
            projects=counts["projects"],
            devices=counts["devices"],
        )
        self.db.record_audit_event(
            category="scan",
            event_type="local_scan_finished",
            actor="kernel",
            device_id=self.settings.device_id,
            payload_json=json.dumps(
                {
                    **result.__dict__,
                    "ignored_sessions": len(ignored_sessions),
                    "purged_projects": purged_projects,
                },
                sort_keys=True,
            ),
        )
        return result

    def project_rundown(self, *, limit: int = 30) -> str:
        projects = self.db.rundown(limit=limit)
        for project in projects:
            path = self.vault.project_page_path(project.name)
            if path.exists():
                project.vault_page = path
        return render_rundown(projects, vault_dir=self.settings.vault_dir)

    def search(self, query: str, *, limit: int = 20) -> list[dict]:
        rows = self.db.search_sessions(query, limit=limit)
        return [dict(row) for row in rows]
