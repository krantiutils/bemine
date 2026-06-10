from __future__ import annotations

import time
from typing import Any

from bemine.config import BemineSettings, config_bool, config_list
from bemine.db import BemineDB
from bemine.deploy.heartbeat import read_heartbeat
from bemine.deploy.state import DeployState, state_path
from bemine.jobs import seed_default_jobs
from bemine.jobs.systemd import UnitStatus, user_unit_status
from bemine.workers.tmux_adapter import TmuxAdapter


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
    continuity = _collect_continuity_health(settings, db)
    if continuity.get("check_error"):
        issues.append(f"continuity check failed: {continuity['check_error']}")
    missing_count = int(continuity.get("restartable_missing", 0) or 0)
    if missing_count:
        issues.append(f"continuity has {missing_count} restartable missing worker(s)")

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
        "continuity": continuity,
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
    continuity = payload.get("continuity") or {}
    devices = ", ".join(continuity.get("devices") or [])
    lines.append(
        "Continuity: "
        f"enabled={bool(continuity.get('enabled'))} "
        f"applies={bool(continuity.get('applies_to_device'))} "
        f"devices={devices or 'none'} "
        f"restartable_missing={continuity.get('restartable_missing', 0)}"
    )
    lines.append("")
    return "\n".join(lines)


def _heartbeat_service_name(unit_name: str) -> str:
    if unit_name.startswith("bemine-daemon"):
        return "daemon"
    if unit_name.startswith("mayor-discord-gateway"):
        return "gateway"
    return unit_name.removesuffix(".service")


def _collect_continuity_health(settings: BemineSettings, db: BemineDB) -> dict[str, Any]:
    enabled = config_bool("continuity", "enabled", True)
    devices = config_list("continuity", "devices", ["cdjk"])
    applies = enabled and settings.device_id in devices
    rows = [dict(row) for row in db.list_managed_sessions()]
    candidates = [
        row
        for row in rows
        if str(row.get("status") or "") in {"running", "missing"}
        and str(row.get("transport") or "") == "tmux"
        and str(row.get("device_id") or "") in {settings.device_id, "local"}
        and _has_restartable_command(row)
        and _cwd_is_directory(str(row.get("cwd") or ""))
    ]
    missing: list[dict[str, Any]] = []
    check_error = ""
    if applies and candidates:
        try:
            tmux = TmuxAdapter()
            for row in candidates:
                name = str(row.get("name") or "")
                if name and not tmux.exists(name):
                    missing.append(_continuity_worker_summary(row))
        except Exception as exc:
            check_error = str(exc)
    recent_events = [
        {
            "ts": row["ts"],
            "event_type": row["event_type"],
            "project_id": row["project_id"] or "",
            "payload_json": row["payload_json"] or "{}",
        }
        for row in db.list_audit_events(category="continuity", limit=5)
    ]
    return {
        "enabled": enabled,
        "device_id": settings.device_id,
        "devices": devices,
        "applies_to_device": applies,
        "tracked_workers": len(candidates),
        "restartable_missing": len(missing),
        "missing_workers": missing,
        "recent_events": recent_events,
        "check_error": check_error,
    }


def _has_restartable_command(row: dict[str, Any]) -> bool:
    command = str(row.get("command") or "").strip()
    return bool(command and command != "adopted-existing-tmux-session")


def _cwd_is_directory(raw: str) -> bool:
    try:
        from pathlib import Path

        path = Path(raw).expanduser()
        return path.exists() and path.is_dir()
    except Exception:
        return False


def _continuity_worker_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": row.get("name") or "",
        "runtime": row.get("runtime") or "",
        "project_id": row.get("project_id") or "",
        "cwd": row.get("cwd") or "",
        "status": row.get("status") or "",
        "updated_at": row.get("updated_at") or 0,
    }
