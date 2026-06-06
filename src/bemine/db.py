from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .index.models import ProjectRundown, SessionRecord
from .util import project_id_from_cwd, project_name_from_cwd


SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    alias TEXT NOT NULL,
    last_seen_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    cwd TEXT NOT NULL,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS project_devices (
    project_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    cwd TEXT NOT NULL,
    last_seen_at REAL NOT NULL,
    PRIMARY KEY(project_id, device_id, cwd),
    FOREIGN KEY(project_id) REFERENCES projects(project_id),
    FOREIGN KEY(device_id) REFERENCES devices(device_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    runtime TEXT NOT NULL,
    device_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    cwd TEXT NOT NULL,
    source_path TEXT NOT NULL,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    model TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    message_count INTEGER NOT NULL,
    tool_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    ingested_at REAL NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(project_id),
    FOREIGN KEY(device_id) REFERENCES devices(device_id)
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    trigger_type TEXT NOT NULL,
    schedule TEXT NOT NULL,
    config_json TEXT NOT NULL,
    last_run_at TEXT,
    next_run_at TEXT,
    failure_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS job_runs (
    run_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    output TEXT NOT NULL,
    FOREIGN KEY(job_id) REFERENCES jobs(job_id)
);

CREATE TABLE IF NOT EXISTS credential_profiles (
    credential_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    storage_ref TEXT NOT NULL,
    approval_policy TEXT NOT NULL,
    totp_required INTEGER NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS permission_requests (
    request_id TEXT PRIMARY KEY,
    credential_id TEXT NOT NULL,
    project_id TEXT,
    session_id TEXT,
    device_id TEXT,
    runtime TEXT,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    discord_message_id TEXT,
    created_at REAL NOT NULL,
    resolved_at REAL,
    resolved_by TEXT,
    FOREIGN KEY(credential_id) REFERENCES credential_profiles(credential_id)
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    category TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    device_id TEXT,
    project_id TEXT,
    session_id TEXT,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    provider TEXT NOT NULL,
    account TEXT,
    runtime TEXT,
    session_id TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    cost_estimate REAL,
    limit_name TEXT,
    limit_remaining REAL,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS managed_sessions (
    name TEXT PRIMARY KEY,
    runtime TEXT NOT NULL,
    transport TEXT NOT NULL,
    device_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    cwd TEXT NOT NULL,
    command TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_seen_at REAL
);

CREATE TABLE IF NOT EXISTS bemine_sessions (
    session_id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    runtime TEXT NOT NULL,
    model TEXT NOT NULL,
    device_id TEXT NOT NULL,
    cwd TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    estimated_tokens INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS bemine_session_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY(session_id) REFERENCES bemine_sessions(session_id)
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    FOREIGN KEY(session_id) REFERENCES bemine_sessions(session_id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS session_fts USING fts5(
    session_id UNINDEXED,
    project_id UNINDEXED,
    runtime,
    cwd,
    title,
    summary,
    tokenize='unicode61'
);
"""


class BemineDB:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def upsert_device(self, device_id: str, alias: str | None = None) -> None:
        now = time.time()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO devices(device_id, alias, last_seen_at)
                VALUES(?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    alias=excluded.alias,
                    last_seen_at=excluded.last_seen_at
                """,
                (device_id, alias or device_id, now),
            )

    def upsert_session(self, record: SessionRecord) -> str:
        project_id = project_id_from_cwd(record.cwd)
        project_name = project_name_from_cwd(record.cwd)
        now = time.time()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO projects(project_id, name, cwd, first_seen_at, last_seen_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    name=excluded.name,
                    cwd=excluded.cwd,
                    last_seen_at=excluded.last_seen_at
                """,
                (project_id, project_name, record.cwd, now, now),
            )
            conn.execute(
                """
                INSERT INTO project_devices(project_id, device_id, cwd, last_seen_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(project_id, device_id, cwd) DO UPDATE SET
                    last_seen_at=excluded.last_seen_at
                """,
                (project_id, record.device_id, record.cwd, now),
            )
            conn.execute(
                """
                INSERT INTO sessions(
                    session_id, runtime, device_id, project_id, cwd, source_path,
                    started_at, updated_at, model, title, summary, message_count,
                    tool_count, status, ingested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    runtime=excluded.runtime,
                    device_id=excluded.device_id,
                    project_id=excluded.project_id,
                    cwd=excluded.cwd,
                    source_path=excluded.source_path,
                    started_at=excluded.started_at,
                    updated_at=excluded.updated_at,
                    model=excluded.model,
                    title=excluded.title,
                    summary=excluded.summary,
                    message_count=excluded.message_count,
                    tool_count=excluded.tool_count,
                    status=excluded.status,
                    ingested_at=excluded.ingested_at
                """,
                (
                    record.session_id,
                    record.runtime,
                    record.device_id,
                    project_id,
                    record.cwd,
                    str(record.source_path),
                    record.started_at,
                    record.updated_at,
                    record.model,
                    record.title,
                    record.summary,
                    record.message_count,
                    record.tool_count,
                    record.status,
                    now,
                ),
            )
            conn.execute("DELETE FROM session_fts WHERE session_id = ?", (record.session_id,))
            conn.execute(
                """
                INSERT INTO session_fts(session_id, project_id, runtime, cwd, title, summary)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.session_id,
                    project_id,
                    record.runtime,
                    record.cwd,
                    record.title,
                    record.summary,
                ),
            )
        return project_id

    def rundown(self, *, limit: int = 30) -> list[ProjectRundown]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    p.project_id,
                    p.name,
                    p.cwd,
                    MAX(s.updated_at) AS latest_at,
                    COUNT(s.session_id) AS session_count,
                    GROUP_CONCAT(DISTINCT s.runtime) AS runtimes,
                    GROUP_CONCAT(DISTINCT s.device_id) AS devices
                FROM projects p
                JOIN sessions s ON s.project_id = p.project_id
                GROUP BY p.project_id
                ORDER BY latest_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            results: list[ProjectRundown] = []
            for row in rows:
                summary_rows = conn.execute(
                    """
                    SELECT runtime, title, summary
                    FROM sessions
                    WHERE project_id = ?
                    ORDER BY updated_at DESC
                    LIMIT 3
                    """,
                    (row["project_id"],),
                ).fetchall()
                summaries = [
                    f"{r['runtime']}: {r['title'] or r['summary']}".strip()
                    for r in summary_rows
                    if (r["title"] or r["summary"])
                ]
                results.append(
                    ProjectRundown(
                        project_id=row["project_id"],
                        name=row["name"],
                        cwd=row["cwd"],
                        devices=sorted((row["devices"] or "").split(",")) if row["devices"] else [],
                        latest_at=row["latest_at"] or "",
                        session_count=int(row["session_count"] or 0),
                        runtimes=sorted((row["runtimes"] or "").split(",")) if row["runtimes"] else [],
                        summaries=summaries,
                    )
                )
            return results

    def search_sessions(self, query: str, *, limit: int = 20) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT s.*
                FROM session_fts f
                JOIN sessions s ON s.session_id = f.session_id
                WHERE session_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (query, limit),
            ).fetchall()

    def counts(self) -> dict[str, int]:
        with self.connect() as conn:
            return {
                "devices": conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0],
                "projects": conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0],
                "sessions": conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
                "jobs": conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                "credential_profiles": conn.execute("SELECT COUNT(*) FROM credential_profiles").fetchone()[0],
                "permission_requests": conn.execute("SELECT COUNT(*) FROM permission_requests").fetchone()[0],
                "audit_events": conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0],
                "usage_samples": conn.execute("SELECT COUNT(*) FROM usage_samples").fetchone()[0],
                "managed_sessions": conn.execute("SELECT COUNT(*) FROM managed_sessions").fetchone()[0],
            }

    def list_projects(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT project_id, name, cwd, first_seen_at, last_seen_at
                FROM projects
                ORDER BY last_seen_at DESC
                """
            ).fetchall()

    def delete_projects(self, project_ids: list[str]) -> int:
        ids = sorted({project_id for project_id in project_ids if project_id})
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as conn:
            conn.execute(
                f"DELETE FROM session_fts WHERE project_id IN ({placeholders})",
                ids,
            )
            conn.execute(
                f"DELETE FROM sessions WHERE project_id IN ({placeholders})",
                ids,
            )
            conn.execute(
                f"DELETE FROM project_devices WHERE project_id IN ({placeholders})",
                ids,
            )
            conn.execute(
                f"UPDATE permission_requests SET project_id = NULL WHERE project_id IN ({placeholders})",
                ids,
            )
            cursor = conn.execute(
                f"DELETE FROM projects WHERE project_id IN ({placeholders})",
                ids,
            )
        return int(cursor.rowcount if cursor.rowcount >= 0 else len(ids))

    def upsert_job(
        self,
        *,
        job_id: str,
        name: str,
        enabled: bool,
        trigger_type: str,
        schedule: str,
        config_json: str,
        next_run_at: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs(
                    job_id, name, enabled, trigger_type, schedule, config_json,
                    last_run_at, next_run_at, failure_count
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, 0)
                ON CONFLICT(job_id) DO UPDATE SET
                    name=excluded.name,
                    enabled=excluded.enabled,
                    trigger_type=excluded.trigger_type,
                    schedule=excluded.schedule,
                    config_json=excluded.config_json,
                    next_run_at=COALESCE(jobs.next_run_at, excluded.next_run_at)
                """,
                (
                    job_id,
                    name,
                    1 if enabled else 0,
                    trigger_type,
                    schedule,
                    config_json,
                    next_run_at,
                ),
            )

    def get_job(self, job_id: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()

    def list_jobs(self, *, enabled: bool | None = None) -> list[sqlite3.Row]:
        with self.connect() as conn:
            if enabled is None:
                return conn.execute("SELECT * FROM jobs ORDER BY job_id").fetchall()
            return conn.execute(
                "SELECT * FROM jobs WHERE enabled = ? ORDER BY job_id",
                (1 if enabled else 0,),
            ).fetchall()

    def set_job_enabled(self, job_id: str, *, enabled: bool) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE jobs SET enabled = ? WHERE job_id = ?",
                (1 if enabled else 0, job_id),
            )

    def update_job_after_run(
        self,
        *,
        job_id: str,
        last_run_at: str,
        next_run_at: str,
        success: bool,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET last_run_at = ?,
                    next_run_at = ?,
                    failure_count = CASE WHEN ? THEN 0 ELSE failure_count + 1 END
                WHERE job_id = ?
                """,
                (last_run_at, next_run_at, 1 if success else 0, job_id),
            )

    def record_job_run(
        self,
        *,
        run_id: str,
        job_id: str,
        started_at: str,
        finished_at: str,
        status: str,
        output: str,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO job_runs(run_id, job_id, started_at, finished_at, status, output)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, job_id, started_at, finished_at, status, output),
            )

    def upsert_credential_profile(
        self,
        *,
        credential_id: str,
        name: str,
        kind: str,
        storage_ref: str,
        approval_policy: str,
        totp_required: bool,
    ) -> None:
        now = time.time()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO credential_profiles(
                    credential_id, name, kind, storage_ref, approval_policy,
                    totp_required, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(credential_id) DO UPDATE SET
                    name=excluded.name,
                    kind=excluded.kind,
                    storage_ref=excluded.storage_ref,
                    approval_policy=excluded.approval_policy,
                    totp_required=excluded.totp_required,
                    updated_at=excluded.updated_at
                """,
                (
                    credential_id,
                    name,
                    kind,
                    storage_ref,
                    approval_policy,
                    1 if totp_required else 0,
                    now,
                    now,
                ),
            )

    def list_credential_profiles(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT *
                FROM credential_profiles
                ORDER BY kind, credential_id
                """
            ).fetchall()

    def get_credential_profile(self, credential_id: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM credential_profiles WHERE credential_id = ?",
                (credential_id,),
            ).fetchone()

    def create_permission_request(
        self,
        *,
        request_id: str,
        credential_id: str,
        reason: str,
        project_id: str | None = None,
        session_id: str | None = None,
        device_id: str | None = None,
        runtime: str | None = None,
        discord_message_id: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO permission_requests(
                    request_id, credential_id, project_id, session_id, device_id,
                    runtime, reason, status, discord_message_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    request_id,
                    credential_id,
                    project_id,
                    session_id,
                    device_id,
                    runtime,
                    reason,
                    discord_message_id,
                    time.time(),
                ),
            )

    def get_permission_request(self, request_id: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT
                    pr.*,
                    cp.totp_required,
                    cp.approval_policy,
                    cp.name AS credential_name,
                    cp.kind AS credential_kind
                FROM permission_requests pr
                JOIN credential_profiles cp ON cp.credential_id = pr.credential_id
                WHERE pr.request_id = ?
                """,
                (request_id,),
            ).fetchone()

    def list_permission_requests(self, *, status: str | None = None) -> list[sqlite3.Row]:
        with self.connect() as conn:
            if status:
                return conn.execute(
                    """
                    SELECT pr.*, cp.name AS credential_name
                    FROM permission_requests pr
                    JOIN credential_profiles cp ON cp.credential_id = pr.credential_id
                    WHERE pr.status = ?
                    ORDER BY pr.created_at DESC
                    """,
                    (status,),
                ).fetchall()
            return conn.execute(
                """
                SELECT pr.*, cp.name AS credential_name
                FROM permission_requests pr
                JOIN credential_profiles cp ON cp.credential_id = pr.credential_id
                ORDER BY pr.created_at DESC
                """
            ).fetchall()

    def resolve_permission_request(
        self,
        *,
        request_id: str,
        status: str,
        resolved_by: str,
    ) -> None:
        if status not in {"approved", "denied"}:
            raise ValueError("status must be approved or denied")
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE permission_requests
                SET status = ?, resolved_at = ?, resolved_by = ?
                WHERE request_id = ?
                """,
                (status, time.time(), resolved_by, request_id),
            )

    def set_permission_discord_message(
        self,
        *,
        request_id: str,
        discord_message_id: str,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE permission_requests
                SET discord_message_id = ?
                WHERE request_id = ?
                """,
                (discord_message_id, request_id),
            )

    def record_audit_event(
        self,
        *,
        category: str,
        event_type: str,
        actor: str,
        payload_json: str,
        device_id: str | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_events(
                    ts, category, event_type, actor, device_id, project_id,
                    session_id, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time(),
                    category,
                    event_type,
                    actor,
                    device_id,
                    project_id,
                    session_id,
                    payload_json,
                ),
            )

    def list_audit_events(
        self,
        *,
        category: str = "",
        event_type: str = "",
        project_id: str = "",
        limit: int = 50,
    ) -> list[sqlite3.Row]:
        clauses = []
        params: list[object] = []
        if category:
            clauses.append("category = ?")
            params.append(category)
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(limit, 500)))
        with self.connect() as conn:
            return conn.execute(
                f"""
                SELECT *
                FROM audit_events
                {where}
                ORDER BY ts DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()

    def record_usage_sample(
        self,
        *,
        provider: str,
        raw_json: str,
        account: str | None = None,
        runtime: str | None = None,
        session_id: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        cost_estimate: float | None = None,
        limit_name: str | None = None,
        limit_remaining: float | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO usage_samples(
                    ts, provider, account, runtime, session_id, input_tokens,
                    output_tokens, total_tokens, cost_estimate, limit_name,
                    limit_remaining, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time(),
                    provider,
                    account,
                    runtime,
                    session_id,
                    input_tokens,
                    output_tokens,
                    total_tokens,
                    cost_estimate,
                    limit_name,
                    limit_remaining,
                    raw_json,
                ),
            )

    def usage_rollup(self, *, provider: str | None = None) -> list[dict]:
        sql = (
            "SELECT provider, COUNT(*) AS samples, "
            "COALESCE(SUM(input_tokens),0) AS input_tokens, "
            "COALESCE(SUM(output_tokens),0) AS output_tokens, "
            "COALESCE(SUM(total_tokens),0) AS total_tokens, "
            "MAX(ts) AS last_ts FROM usage_samples "
        )
        params: tuple = ()
        if provider:
            sql += "WHERE provider = ? "
            params = (provider,)
        sql += "GROUP BY provider ORDER BY total_tokens DESC"
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def upsert_managed_session(
        self,
        *,
        name: str,
        runtime: str,
        transport: str,
        device_id: str,
        project_id: str,
        cwd: str,
        command: str,
        status: str,
    ) -> None:
        now = time.time()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO managed_sessions(
                    name, runtime, transport, device_id, project_id, cwd,
                    command, status, created_at, updated_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    runtime=excluded.runtime,
                    transport=excluded.transport,
                    device_id=excluded.device_id,
                    project_id=excluded.project_id,
                    cwd=excluded.cwd,
                    command=excluded.command,
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    last_seen_at=excluded.last_seen_at
                """,
                (
                    name,
                    runtime,
                    transport,
                    device_id,
                    project_id,
                    cwd,
                    command,
                    status,
                    now,
                    now,
                    now,
                ),
            )

    def list_managed_sessions(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT *
                FROM managed_sessions
                ORDER BY updated_at DESC, name
                """
            ).fetchall()

    def get_managed_session(self, name: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM managed_sessions WHERE name = ?",
                (name,),
            ).fetchone()

    def set_managed_session_status(self, name: str, *, status: str) -> None:
        now = time.time()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE managed_sessions
                SET status = ?, updated_at = ?, last_seen_at = ?
                WHERE name = ?
                """,
                (status, now, now, name),
            )

    def delete_managed_session(self, name: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM managed_sessions WHERE name = ?", (name,))
        return bool(cursor.rowcount)

    def upsert_bemine_session(
        self,
        *,
        session_id: str,
        channel_id: str,
        project_id: str,
        runtime: str,
        model: str,
        device_id: str,
        cwd: str,
        status: str = "active",
        summary: str = "",
        estimated_tokens: int = 0,
    ) -> None:
        now = time.time()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO bemine_sessions(
                    session_id, channel_id, project_id, runtime, model, device_id,
                    cwd, status, summary, message_count, estimated_tokens,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    channel_id=excluded.channel_id,
                    project_id=excluded.project_id,
                    runtime=excluded.runtime,
                    model=excluded.model,
                    device_id=excluded.device_id,
                    cwd=excluded.cwd,
                    status=excluded.status,
                    summary=excluded.summary,
                    estimated_tokens=excluded.estimated_tokens,
                    updated_at=excluded.updated_at
                """,
                (
                    session_id,
                    channel_id,
                    project_id,
                    runtime,
                    model,
                    device_id,
                    cwd,
                    status,
                    summary,
                    estimated_tokens,
                    now,
                    now,
                ),
            )

    def append_bemine_session_message(
        self,
        *,
        session_id: str,
        role: str,
        content_json: str,
    ) -> None:
        now = time.time()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO bemine_session_messages(session_id, role, content_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, role, content_json, now),
            )
            conn.execute(
                """
                UPDATE bemine_sessions
                SET message_count = message_count + 1, updated_at = ?
                WHERE session_id = ?
                """,
                (now, session_id),
            )

    def get_bemine_session(self, session_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM bemine_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_bemine_session_by_channel(self, channel_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM bemine_sessions WHERE channel_id = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (channel_id,),
            ).fetchone()
            return dict(row) if row else None

    def list_bemine_session_messages(
        self,
        *,
        session_id: str,
        limit: int = 40,
    ) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT *
                FROM bemine_session_messages
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, max(1, min(limit, 200))),
            ).fetchall()[::-1]

    def record_tool_call_started(
        self,
        *,
        session_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments_json: str,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO tool_calls(
                    session_id, tool_call_id, tool_name, arguments_json,
                    status, result_json, started_at
                ) VALUES (?, ?, ?, ?, 'running', '{}', ?)
                """,
                (session_id, tool_call_id, tool_name, arguments_json, time.time()),
            )

    def record_tool_call_finished(
        self,
        *,
        session_id: str,
        tool_call_id: str,
        status: str,
        result_json: str,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE tool_calls
                SET status = ?, result_json = ?, finished_at = ?
                WHERE session_id = ? AND tool_call_id = ?
                """,
                (status, result_json, time.time(), session_id, tool_call_id),
            )
