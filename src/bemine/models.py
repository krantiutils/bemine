from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    runtime: str
    device_id: str
    cwd: str
    source_path: Path
    started_at: str
    updated_at: str
    model: str
    title: str
    summary: str
    message_count: int
    tool_count: int
    status: str = "observed"


@dataclass(frozen=True)
class ProjectRecord:
    project_id: str
    name: str
    cwd: str
    device_id: str
    updated_at: str


@dataclass
class ProjectRundown:
    project_id: str
    name: str
    cwd: str
    devices: list[str]
    latest_at: str
    session_count: int
    runtimes: list[str]
    summaries: list[str]
    vault_page: Path | None = None
