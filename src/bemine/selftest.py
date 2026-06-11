"""Nightly in-daemon selftest: exercise the loops unit tests cannot reach.

The 2026-06-11 e2e round found 11 bugs behind a fully green suite — daemon
PATH, TUI boot races, CLI exit codes all live outside FakeTmux. This job runs
INSIDE the daemon, so its environment (systemd PATH, real tmux, real disk
state) is the production one. Failures raise, which marks the job failed and
surfaces in health as a job issue.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.jobs.auto_recovery import _parse_reset_epoch, classify, _cfg_signatures
from bemine.preflight import runtime_preflight
from bemine.workers.tmux_adapter import TmuxAdapter
from bemine.workers.workers import (
    WorkerController,
    _resolve_runtime_bin,
    resume_missing_local_workers,
)

SELFTEST_WORKER_PREFIX = "bemine-selftest"


def run_selftest(
    settings: BemineSettings, *, db: BemineDB | None = None
) -> dict[str, Any]:
    db = db or BemineDB(settings.db_path)
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    # 1. Runtime preflight (the goal-default runtime must be usable).
    for row in runtime_preflight(settings):
        name = f"runtime:{row['runtime']}"
        if row["goal_default"] or row["installed"]:
            check(name, row["ok"], row["detail"])

    # 2. Daemon PATH: every installed runtime must resolve to an absolute path —
    #    after a reboot THIS process spawns the tmux server, bare names die.
    for runtime in ("claude", "codex", "kimi"):
        resolved = _resolve_runtime_bin(runtime)
        if "/" in resolved:
            check(f"abs-bin:{runtime}", Path(resolved).is_file(), resolved)

    # 3. Recovery classification + reset parsing on known CLI phrasings.
    sigs = _cfg_signatures()
    limit_text = "You've hit your usage limit. Try again in 2 hours 5 minutes."
    check(
        "recovery:classify-usage-limit",
        classify(limit_text, signatures=sigs) == "usage_limit",
        limit_text,
    )
    now = time.time()
    parsed = _parse_reset_epoch(limit_text, now)
    check(
        "recovery:parse-relative-reset",
        parsed is not None and abs(parsed - (now + 2 * 3600 + 5 * 60)) < 120,
        f"parsed={parsed}",
    )
    check(
        "recovery:approaching-not-limit",
        classify("Approaching 5-hour limit · resets 3pm", signatures=sigs) == "healthy",
        "approaching warning must stay healthy",
    )

    # 4. Continuity loop with a real tmux session in THIS environment:
    #    start -> kill -> resume_missing_local_workers -> alive again.
    name = f"{SELFTEST_WORKER_PREFIX}-{uuid.uuid4().hex[:8]}"
    tmux = TmuxAdapter()
    controller = WorkerController(settings, db=db, tmux=tmux)
    cwd = settings.state_dir
    cwd.mkdir(parents=True, exist_ok=True)
    try:
        controller.start(name=name, runtime="shell", cwd=cwd, command="bash")
        tmux.kill(name)
        resumed = resume_missing_local_workers(
            settings, db=db, tmux=tmux, nudge_sleep=lambda _: None
        )
        resumed_names = {record.name for record in resumed}
        check(
            "continuity:kill-resume",
            name in resumed_names and tmux.exists(name),
            f"worker {name} resumed={name in resumed_names}",
        )
    finally:
        try:
            tmux.kill(name)
        except Exception:
            pass
        db.delete_managed_session(name)

    failed = [c for c in checks if not c["ok"]]
    return {
        "ok": not failed,
        "checks": checks,
        "failed": [c["name"] for c in failed],
        "ran_at": time.time(),
    }


def render_selftest(result: dict[str, Any]) -> str:
    status = "ok" if result.get("ok") else "FAILED"
    lines = [f"Bemine selftest: {status}"]
    for check in result.get("checks") or []:
        marker = "ok" if check["ok"] else "FAIL"
        lines.append(f"- {check['name']}: {marker} — {check['detail']}")
    return "\n".join(lines) + "\n"
