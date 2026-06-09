from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .state import DeployState, state_path

GATEWAY = "gateway"
DAEMON = "daemon"
BOOT_ATTEMPT_LIMIT = 2
CONFIRM_WINDOW = 60
HEALTH_TIMEOUT = 90
HEALTH_POLL = 3
HB_FRESH = 45

_IN_FLIGHT = {"deploying", "daemon_refresh"}


@dataclass
class DeployDeps:
    run_tests: Callable          # () -> (bool, str)
    restart: Callable            # (service) -> None
    git_reset_hard: Callable     # (sha) -> None
    read_hb: Callable            # (service) -> dict | None
    now: Callable                # () -> float
    sleep: Callable              # (seconds) -> None
    echo: Callable               # (channel_id, text) -> None
    head_sha: Callable           # () -> str
    is_clean: Callable           # () -> bool


def _tail(text: str, lines: int = 20) -> str:
    return "\n".join((text or "").splitlines()[-lines:])


def _await_healthy(deps: DeployDeps, service: str, sha: str) -> bool:
    deadline = deps.now() + HEALTH_TIMEOUT
    while deps.now() < deadline:
        hb = deps.read_hb(service)
        if hb and hb.get("sha") == sha and (deps.now() - float(hb.get("ts", 0))) < HB_FRESH:
            return True
        deps.sleep(HEALTH_POLL)
    return False


def request_deploy(settings, *, channel_id: str, requested_by: str = "") -> tuple[bool, str]:
    path = state_path(settings)
    st = DeployState.load(path)
    if st.phase in _IN_FLIGHT or st.phase == "requested":
        return False, "a deploy is already in progress"
    now = time.time()
    DeployState(phase="requested", channel_id=channel_id, requested_by=requested_by,
                requested_at=now, updated_at=now).save(path)
    return True, "deploy requested"


def run_deploy(settings, *, channel_id: str = "", deps: DeployDeps | None = None,
               self_restart_daemon: bool = True) -> DeployState:
    deps = deps or default_deps(settings)
    path = state_path(settings)
    st = DeployState.load(path)
    if st.phase in _IN_FLIGHT:
        return st
    channel_id = channel_id or st.channel_id

    if not deps.is_clean():
        st = DeployState(phase="failed", channel_id=channel_id, updated_at=deps.now())
        st.save(path)
        deps.echo(channel_id, "❌ Deploy aborted: working tree is dirty. Commit or stash first.")
        return st

    target = deps.head_sha()
    hb = deps.read_hb(GATEWAY)
    rollback = (hb or {}).get("sha") or target
    st = DeployState(phase="deploying", target_sha=target, rollback_sha=rollback,
                     channel_id=channel_id, updated_at=deps.now())
    st.save(path)

    ok, output = deps.run_tests()
    if not ok:
        ok, output = deps.run_tests()  # retry once (absorbs known flake)
    if not ok:
        st.phase = "failed"
        st.updated_at = deps.now()
        st.save(path)
        deps.echo(channel_id, "❌ Tests failed — not deploying:\n" + _tail(output))
        return st

    deps.restart(GATEWAY)
    if not _await_healthy(deps, GATEWAY, target):
        deps.git_reset_hard(rollback)
        deps.restart(GATEWAY)
        rolled_ok = _await_healthy(deps, GATEWAY, rollback)
        st.phase = "rolled_back" if rolled_ok else "failed"
        st.updated_at = deps.now()
        st.save(path)
        deps.echo(channel_id,
                  f"↩ Rolled back to `{rollback[:8]}` (gateway failed health check)."
                  if rolled_ok else
                  "‼ Rollback failed — manual intervention needed.")
        return st

    deps.echo(channel_id, f"✅ Gateway live on `{target[:8]}`.")
    st.phase = "daemon_refresh"
    st.rollback_sha = rollback
    st.boot_attempts = 0
    st.boot_ts = deps.now()
    st.updated_at = deps.now()
    st.save(path)

    if self_restart_daemon:
        deps.echo(channel_id, "♻ Refreshing daemon…")
        deps.restart(DAEMON)
        return st

    deps.restart(DAEMON)
    if _await_healthy(deps, DAEMON, target):
        st.phase = "confirmed"
        st.updated_at = deps.now()
        st.save(path)
        deps.echo(channel_id, f"✅ Deploy complete on `{target[:8]}`.")
    else:
        deps.git_reset_hard(rollback)
        deps.restart(GATEWAY)
        deps.restart(DAEMON)
        st.phase = "rolled_back"
        st.updated_at = deps.now()
        st.save(path)
        deps.echo(channel_id, f"↩ Rolled back to `{rollback[:8]}` (daemon failed health check).")
    return st


def default_deps(settings) -> DeployDeps:  # pragma: no cover - replaced in a later task
    raise NotImplementedError("default_deps is implemented in a later task")
