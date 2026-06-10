from __future__ import annotations

import time
from typing import Any

from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.deploy.heartbeat import read_heartbeat
from bemine.deploy.state import DeployState, state_path
from bemine.jobs import seed_default_jobs
from bemine.jobs.systemd import UnitStatus, user_unit_status


def collect_health(
    settings: BemineSettings,
    *,
    db: BemineDB | None = None,
    unit_statuses: list[UnitStatus] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    db = db or BemineDB(settings.db_path)
    now = time.time() if now is None else now
    seed_default_jobs(db, settings=settings)

    deploy = DeployState.load(state_path(settings))
    units = unit_statuses if unit_statuses is not None else user_unit_status()
    services = []
    issues: list[str] = []
    for unit in units:
        service = _heartbeat_service_name(unit.name)
        heartbeat = read_heartbeat(settings, service) or {}
        item = unit.as_dict()
        item["service"] = service
        item["heartbeat"] = heartbeat
        services.append(item)
        if unit.active_state != "active":
            issues.append(f"{unit.name} is {unit.active_state or 'unknown'}")
        if not heartbeat:
            issues.append(f"{service} heartbeat is missing")
        elif deploy.target_sha and heartbeat.get("sha") != deploy.target_sha:
            issues.append(f"{service} heartbeat is not on deploy target")

    jobs = [dict(row) for row in db.list_jobs()]
    failed_jobs = [
        job for job in jobs
        if int(job.get("failure_count") or 0) > 0
    ]
    for job in failed_jobs:
        issues.append(f"{job['job_id']} has {job['failure_count']} failure(s)")

    watchers = db.list_active_project_watchers()
    if deploy.phase not in {"confirmed", "idle"}:
        issues.append(f"deploy phase is {deploy.phase}")

    return {
        "ok": not issues,
        "status": "ok" if not issues else "attention",
        "checked_at": now,
        "issues": issues,
        "deploy": {
            "phase": deploy.phase,
            "target_sha": deploy.target_sha,
            "rollback_sha": deploy.rollback_sha,
            "updated_at": deploy.updated_at,
        },
        "services": services,
        "jobs": {
            "total": len(jobs),
            "enabled": sum(1 for job in jobs if int(job.get("enabled") or 0)),
            "failed": len(failed_jobs),
            "failed_jobs": [
                {
                    "job_id": job["job_id"],
                    "failure_count": int(job.get("failure_count") or 0),
                    "last_run_at": job.get("last_run_at") or "",
                }
                for job in failed_jobs
            ],
        },
        "watchers": {
            "active": len(watchers),
            "items": watchers,
        },
    }


def render_health(payload: dict[str, Any]) -> str:
    lines = [f"Bemine health: {payload['status']}"]
    issues = payload.get("issues") or []
    if issues:
        lines.append("Issues:")
        lines.extend(f"- {issue}" for issue in issues)

    deploy = payload.get("deploy") or {}
    target = str(deploy.get("target_sha") or "")
    lines.append(
        f"Deploy: phase={deploy.get('phase') or 'unknown'} target={target[:8] or 'none'}"
    )

    lines.append("Services:")
    for service in payload.get("services") or []:
        heartbeat = service.get("heartbeat") or {}
        sha = str(heartbeat.get("sha") or "")
        lines.append(
            f"- {service.get('name')}: active={service.get('active_state') or 'unknown'} "
            f"sub={service.get('sub_state') or 'unknown'} "
            f"heartbeat={sha[:8] or 'none'}"
        )

    jobs = payload.get("jobs") or {}
    lines.append(
        f"Jobs: total={jobs.get('total', 0)} enabled={jobs.get('enabled', 0)} "
        f"failed={jobs.get('failed', 0)}"
    )
    watchers = payload.get("watchers") or {}
    lines.append(f"Watchers: active={watchers.get('active', 0)}")
    lines.append("")
    return "\n".join(lines)


def _heartbeat_service_name(unit_name: str) -> str:
    if unit_name.startswith("bemine-daemon"):
        return "daemon"
    if unit_name.startswith("mayor-discord-gateway"):
        return "gateway"
    return unit_name.removesuffix(".service")
