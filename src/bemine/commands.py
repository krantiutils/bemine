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
    from bemine.status import collect_fleet_status, render_status

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
    CommandSpec("capabilities", "Credential capabilities", _capabilities),
    CommandSpec("usage", "Codex/Claude/Kimi token usage", _usage),
)


def registry() -> dict[str, CommandSpec]:
    return {spec.name: spec for spec in _SPECS}


def run_command(name: str, settings: BemineSettings, args: Mapping) -> CommandResult:
    return registry()[name].handler(settings, dict(args))
