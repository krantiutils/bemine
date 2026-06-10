from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from bemine.config import BemineSettings
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


def _dispatch_status(settings: BemineSettings, args: Mapping) -> CommandResult:
    from bemine.discord.dispatch import dispatch_status_payload, render_dispatch_status

    payload = dispatch_status_payload(settings)
    return CommandResult(text=render_dispatch_status(payload), data=payload)


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


_SPECS = (
    CommandSpec("status", "Recent fleet status", _status),
    CommandSpec("rundown", "Project rundown", _rundown),
    CommandSpec("jobs.list", "List scheduled jobs", _jobs_list),
    CommandSpec("health", "Bemine service/job/deploy health", _health),
    CommandSpec("dispatch.status", "Current Discord dispatch runtime/model", _dispatch_status),
    CommandSpec("goals.list", "List managed goal sessions", _goals_list),
    CommandSpec("capabilities", "Credential capabilities", _capabilities),
    CommandSpec("usage", "Codex/Claude/Kimi token usage", _usage),
)


def registry() -> dict[str, CommandSpec]:
    return {spec.name: spec for spec in _SPECS}


def run_command(name: str, settings: BemineSettings, args: Mapping) -> CommandResult:
    return registry()[name].handler(settings, dict(args))
