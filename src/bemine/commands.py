from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from bemine.config import BemineSettings, config_bool, config_int, config_list, config_string
from bemine.kernel import BemineKernel


@dataclass(frozen=True)
class CommandResult:
    text: str
    data: dict


@dataclass(frozen=True)
class CommandSpec:
    name: str
    summary: str
    handler: Callable[[BemineSettings, Mapping], CommandResult]


def _status(settings: BemineSettings, args: Mapping) -> CommandResult:
    from bemine.index.status import collect_fleet_status, render_status

    statuses, errors = collect_fleet_status(settings)
    limit = int(args.get("limit", 8))
    return CommandResult(
        text=render_status(statuses, errors, limit=limit),
        data={"count": len(statuses), "errors": len(errors)},
    )


def _rundown(settings: BemineSettings, args: Mapping) -> CommandResult:
    kernel = BemineKernel(settings)
    text = kernel.project_rundown(limit=int(args.get("limit", 10)))
    return CommandResult(text=text, data={})


def _jobs_list(settings: BemineSettings, args: Mapping) -> CommandResult:
    from bemine.jobs import render_jobs

    kernel = BemineKernel(settings)
    rows = [dict(r) for r in kernel.db.list_jobs()]
    return CommandResult(text=render_jobs(rows), data={"jobs": rows})


def _health(settings: BemineSettings, args: Mapping) -> CommandResult:
    from bemine.health import collect_health, render_health

    payload = collect_health(settings)
    return CommandResult(text=render_health(payload), data=payload)


def _continuity(settings: BemineSettings, args: Mapping) -> CommandResult:
    from bemine.health import collect_health, render_continuity

    payload = collect_health(settings)
    return CommandResult(text=render_continuity(payload), data=payload["continuity"])


def _dispatch_status(settings: BemineSettings, args: Mapping) -> CommandResult:
    from bemine.discord.dispatch import dispatch_status_payload, render_dispatch_status

    payload = dispatch_status_payload(settings)
    return CommandResult(text=render_dispatch_status(payload), data=payload)


def _goal_start(settings: BemineSettings, args: Mapping) -> CommandResult:
    from bemine.goals import render_goal_start, start_goal

    project = str(args.get("project") or "").strip()
    brief = str(args.get("brief") or "").strip()
    if not project or not brief:
        raise ValueError("goal.start requires project and brief")
    cwd_raw = str(args.get("cwd") or "").strip()
    runtime = str(args.get("runtime") or "").strip()
    worker_name = str(args.get("worker") or "").strip()
    interval = int(args.get("interval") or 0)
    channel_id = str(args.get("channel_id") or "").strip()
    target_device = str(args.get("target_device") or "").strip()
    command = str(args.get("command") or "").strip()
    result = start_goal(
        settings,
        project=project,
        brief=brief,
        cwd=Path(cwd_raw) if cwd_raw else None,
        runtime=runtime,
        worker_name=worker_name,
        interval_secs=interval,
        channel_id=channel_id,
        target_device=target_device,
        command=command,
    )
    return CommandResult(text=render_goal_start(result), data=result.to_dict())


def _pa_plan(settings: BemineSettings, args: Mapping) -> CommandResult:
    from bemine.pa import build_pa_plan, render_pa_plan

    limit = int(args.get("limit") or 6)
    result = build_pa_plan(settings, limit=limit)
    return CommandResult(text=render_pa_plan(result), data=result.to_dict())


def _goals_list(settings: BemineSettings, args: Mapping) -> CommandResult:
    from bemine.goals import list_goal_sessions, render_goal_sessions

    query = str(args.get("query") or "")
    sessions = list_goal_sessions(settings, status=query)
    return CommandResult(
        text=render_goal_sessions(sessions),
        data={"sessions": [session.to_dict() for session in sessions]},
    )


def _capabilities(settings: BemineSettings, args: Mapping) -> CommandResult:
    from bemine.capabilities import build_capability_manifest, render_capability_manifest

    kernel = BemineKernel(settings)
    rows = [dict(r) for r in kernel.db.list_credential_profiles()]
    manifest = build_capability_manifest(rows, device_id=settings.device_id)
    return CommandResult(text=render_capability_manifest(manifest), data=manifest)


def _usage(settings: BemineSettings, args: Mapping) -> CommandResult:
    from bemine.usage import render_usage

    kernel = BemineKernel(settings)
    rollup = kernel.db.usage_rollup(provider=args.get("provider"))
    return CommandResult(text=render_usage(rollup), data={"providers": rollup})


def _help(settings: BemineSettings, args: Mapping) -> CommandResult:
    from bemine.context.subagents import load_subagent_context
    from bemine.discord.dispatch import dispatch_status_payload
    from bemine.jobs import default_jobs

    subagents = load_subagent_context()
    dispatch = dispatch_status_payload(settings)
    jobs = list(default_jobs(settings))
    goal_runtime = config_string("goal", "runtime", "claude") or "claude"
    goal_interval = config_int("goal", "interval_secs", 600)
    continuity_enabled = config_bool("continuity", "enabled", True)
    continuity_devices = config_list("continuity", "devices", ["cdjk"])
    phantom_root = config_string("phantom", "project_root", "")
    summary_lines = [
        "Bemine help:",
        "- ask: help, status, health, continuity, goal, watch, jobs, dispatch, pa, smm, subagents, phantom, backlog, capabilities, audit, config",
        "Configured surfaces:",
        f"- device: `{settings.device_id}`",
        f"- state: `{settings.state_dir}`",
        f"- vault: `{settings.vault_dir}`",
        f"- goal: runtime={goal_runtime} interval={goal_interval}s",
        f"- continuity: enabled={continuity_enabled} devices={', '.join(continuity_devices) or 'none'}",
        f"- dispatch: runtime={dispatch.get('runtime', 'unknown')} backend={dispatch.get('backend', 'unknown')} model={dispatch.get('model', 'unknown')}",
        f"- subagents: {', '.join(profile.name for profile in subagents.profiles) or 'none'}",
        f"- phantom: project_root={phantom_root or 'unset'}",
        "Built-in jobs:",
    ]
    summary_lines.extend(
        f"- {job.job_id}: `{job.schedule}`" for job in jobs
    )
    summary_lines.extend(
        [
            "",
            "Use `uv run bemine <command> --help` for the contract of any command.",
        ]
    )
    return CommandResult(
        text="\n".join(summary_lines) + "\n",
        data={
            "device_id": settings.device_id,
            "state_dir": str(settings.state_dir),
            "vault_dir": str(settings.vault_dir),
            "goal": {"runtime": goal_runtime, "interval_secs": goal_interval},
            "continuity": {
                "enabled": continuity_enabled,
                "devices": continuity_devices,
            },
            "dispatch": dispatch,
            "subagents": [profile.to_dict() for profile in subagents.profiles],
            "phantom": {"project_root": phantom_root},
            "jobs": [
                {"job_id": job.job_id, "schedule": job.schedule, "name": job.name}
                for job in jobs
            ],
        },
    )


_SPECS = (
    CommandSpec("status", "Recent fleet status", _status),
    CommandSpec("rundown", "Project rundown", _rundown),
    CommandSpec("jobs.list", "List scheduled jobs", _jobs_list),
    CommandSpec("health", "Bemine service/job/deploy health", _health),
    CommandSpec("continuity", "cdjk line-cut recovery status", _continuity),
    CommandSpec("help", "Self-documenting command and config summary", _help),
    CommandSpec("dispatch.status", "Current Discord dispatch runtime/model", _dispatch_status),
    CommandSpec("goal.start", "Start a managed goal session", _goal_start),
    CommandSpec("pa.plan", "Priority-ordered PA focus plan", _pa_plan),
    CommandSpec("goals.list", "List managed goal sessions", _goals_list),
    CommandSpec("capabilities", "Credential capabilities", _capabilities),
    CommandSpec("usage", "Codex/Claude/Kimi token usage", _usage),
)


def registry() -> dict[str, CommandSpec]:
    return {spec.name: spec for spec in _SPECS}


def run_command(name: str, settings: BemineSettings, args: Mapping) -> CommandResult:
    return registry()[name].handler(settings, dict(args))
