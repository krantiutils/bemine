# Self-Deploy Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship Bemine code changes from Discord (or `bemine deploy`) with a hard test-gate, heartbeat-based health checks, and automatic rollback for both the gateway and the daemon — no SSH, self-healing.

**Architecture:** A new `src/bemine/deploy/` package. The gateway and daemon each publish a heartbeat (`{service, sha, pid, ts}`). A deploy is requested from Discord (writes a `requested` state) and executed by the daemon on its next poll (or run directly by the CLI). `run_deploy` runs tests, restarts the gateway and verifies its heartbeat (rolling back on failure), then refreshes the daemon. A daemon `boot_guard` counts restarts after a deploy and auto-reverts on a crash-loop. Spec: `docs/superpowers/specs/2026-06-09-self-deploy-design.md`.

**Tech Stack:** Python 3, stdlib `subprocess`/`json`/`dataclasses`, `git`, `systemctl --user`, `uv run pytest`, the existing `bemine.jobs.project_watcher.echo_to_channel`, `pytest`.

---

## Conventions (read once)

- Run tests with `uv run pytest <path> -v`; full suite `uv run pytest -q`. Lint with `uv run ruff check <paths>`.
- `BemineSettings` has `.state_dir` (a `Path`, auto-created). Tests build it with `BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")`.
- Heartbeats and deploy state live under `settings.state_dir / "deploy"`.
- `bemine.jobs.project_watcher.echo_to_channel(channel_id, text)` posts to Discord (best-effort, no-op on empty channel). Reuse it for the real `echo` dep.
- Known flake: `tests/test_workers.py::test_workers_transfer_injects_handoff_into_existing_tmux_target` is a real-tmux ordering flake; rerun once if it fails in the full-suite step.

### Locked interfaces (used across tasks — keep names exact)

- `heartbeat.py`: `repo_dir() -> Path`; `current_head_sha(repo=None, *, runner=subprocess.run) -> str`; `heartbeat_path(settings, service) -> Path`; `write_heartbeat(settings, service, sha) -> None`; `read_heartbeat(settings, service) -> dict | None`.
- `state.py`: `DeployState` dataclass; `state_path(settings) -> Path`; `DeployState.load(path) -> DeployState`; `DeployState.save(path) -> None`.
- `pipeline.py`: constants `GATEWAY="gateway"`, `DAEMON="daemon"`, `BOOT_ATTEMPT_LIMIT=2`, `CONFIRM_WINDOW=60`, `HEALTH_TIMEOUT=90`, `HEALTH_POLL=3`, `HB_FRESH=45`; `DeployDeps` dataclass; `default_deps(settings) -> DeployDeps`; `request_deploy(settings, *, channel_id, requested_by="") -> tuple[bool, str]`; `run_deploy(settings, *, channel_id="", deps=None, self_restart_daemon=True) -> DeployState`; `boot_guard(settings, *, deps=None) -> None`; `maybe_confirm_deploy(settings, *, deps=None) -> None`.
- `DeployDeps` fields: `run_tests` `()->(bool,str)`, `restart` `(service)->None`, `git_reset_hard` `(sha)->None`, `read_hb` `(service)->dict|None`, `now` `()->float`, `sleep` `(s)->None`, `echo` `(channel_id,text)->None`, `head_sha` `()->str`, `is_clean` `()->bool`.

---

## Task 1: heartbeat module

**Files:**
- Create: `src/bemine/deploy/__init__.py` (empty), `src/bemine/deploy/heartbeat.py`
- Test: `tests/test_deploy_heartbeat.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_deploy_heartbeat.py
import subprocess
from bemine.config import BemineSettings
from bemine.deploy.heartbeat import (
    write_heartbeat, read_heartbeat, heartbeat_path, current_head_sha,
)


def _settings(tmp_path):
    return BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")


def test_write_then_read_roundtrip(tmp_path):
    s = _settings(tmp_path)
    write_heartbeat(s, "gateway", "abc123")
    hb = read_heartbeat(s, "gateway")
    assert hb["service"] == "gateway"
    assert hb["sha"] == "abc123"
    assert isinstance(hb["pid"], int)
    assert isinstance(hb["ts"], (int, float))


def test_read_missing_returns_none(tmp_path):
    assert read_heartbeat(_settings(tmp_path), "daemon") is None


def test_heartbeat_path_under_state_dir(tmp_path):
    s = _settings(tmp_path)
    p = heartbeat_path(s, "gateway")
    assert p.name == "heartbeat-gateway.json"
    assert p.parent == s.state_dir / "deploy"


def test_current_head_sha_uses_runner():
    def fake_runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="deadbeef\n", stderr="")
    assert current_head_sha(runner=fake_runner) == "deadbeef"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_deploy_heartbeat.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bemine.deploy'`.

- [ ] **Step 3: Create the package + module**

Create empty `src/bemine/deploy/__init__.py`. Create `src/bemine/deploy/heartbeat.py`:

```python
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path


def repo_dir() -> Path:
    """The git repo root containing this package (services run from it)."""
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        if (parent / ".git").exists():
            return parent
    return Path.cwd()


def current_head_sha(repo: Path | None = None, *, runner=subprocess.run) -> str:
    proc = runner(
        ["git", "-C", str(repo or repo_dir()), "rev-parse", "HEAD"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    return (proc.stdout or "").strip()


def heartbeat_path(settings, service: str) -> Path:
    return settings.state_dir / "deploy" / f"heartbeat-{service}.json"


def write_heartbeat(settings, service: str, sha: str) -> None:
    path = heartbeat_path(settings, service)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"service": service, "sha": sha, "pid": os.getpid(), "ts": time.time()},
        sort_keys=True,
    ), encoding="utf-8")


def read_heartbeat(settings, service: str) -> dict | None:
    path = heartbeat_path(settings, service)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_deploy_heartbeat.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/bemine/deploy/__init__.py src/bemine/deploy/heartbeat.py tests/test_deploy_heartbeat.py
git commit -m "feat(deploy): per-service heartbeat write/read"
```

---

## Task 2: deploy state record

**Files:**
- Create: `src/bemine/deploy/state.py`
- Test: `tests/test_deploy_state.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_deploy_state.py
from bemine.config import BemineSettings
from bemine.deploy.state import DeployState, state_path


def _settings(tmp_path):
    return BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")


def test_default_state_is_idle():
    st = DeployState()
    assert st.phase == "idle"
    assert st.boot_attempts == 0
    assert st.target_sha == ""


def test_save_then_load_roundtrip(tmp_path):
    s = _settings(tmp_path)
    p = state_path(s)
    st = DeployState(phase="daemon_refresh", target_sha="t1", rollback_sha="r1",
                     channel_id="c1", boot_attempts=2, boot_ts=99.0)
    st.save(p)
    loaded = DeployState.load(p)
    assert loaded.phase == "daemon_refresh"
    assert loaded.target_sha == "t1"
    assert loaded.rollback_sha == "r1"
    assert loaded.channel_id == "c1"
    assert loaded.boot_attempts == 2
    assert loaded.boot_ts == 99.0


def test_load_missing_returns_idle(tmp_path):
    assert DeployState.load(state_path(_settings(tmp_path))).phase == "idle"


def test_state_path_under_state_dir(tmp_path):
    s = _settings(tmp_path)
    assert state_path(s) == s.state_dir / "deploy" / "state.json"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_deploy_state.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bemine.deploy.state'`.

- [ ] **Step 3: Write the implementation**

```python
# src/bemine/deploy/state.py
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class DeployState:
    phase: str = "idle"  # idle|requested|deploying|daemon_refresh|confirmed|rolled_back|failed
    target_sha: str = ""
    rollback_sha: str = ""
    channel_id: str = ""
    requested_by: str = ""
    boot_attempts: int = 0
    boot_ts: float = 0.0
    requested_at: float = 0.0
    updated_at: float = 0.0

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), sort_keys=True, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "DeployState":
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return cls()
        fields = cls.__dataclass_fields__
        return cls(**{k: v for k, v in data.items() if k in fields})


def state_path(settings) -> Path:
    return settings.state_dir / "deploy" / "state.json"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_deploy_state.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/bemine/deploy/state.py tests/test_deploy_state.py
git commit -m "feat(deploy): DeployState record (load/save)"
```

---

## Task 3: pipeline core — request_deploy + run_deploy (all paths)

**Files:**
- Create: `src/bemine/deploy/pipeline.py`
- Test: `tests/test_deploy_pipeline.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_deploy_pipeline.py
from bemine.config import BemineSettings
from bemine.deploy.state import DeployState, state_path
from bemine.deploy import pipeline as P


def _settings(tmp_path):
    return BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")


class _Clock:
    def __init__(self):
        self.t = 1000.0
    def now(self):
        return self.t
    def sleep(self, s):
        self.t += s


def _deps(*, clock, tests_ok=True, head="newsha", clean=True, hb_script=None):
    """hb_script: dict service -> list of heartbeat dicts returned successively."""
    calls = {"restart": [], "reset": [], "echo": [], "tests": 0}
    hb_script = hb_script or {}
    hb_idx = {k: 0 for k in hb_script}

    def run_tests():
        calls["tests"] += 1
        ok = tests_ok if isinstance(tests_ok, bool) else tests_ok[min(calls["tests"] - 1, len(tests_ok) - 1)]
        return ok, "test output tail"

    def read_hb(service):
        seq = hb_script.get(service)
        if not seq:
            return None
        i = min(hb_idx[service], len(seq) - 1)
        hb_idx[service] += 1
        return seq[i]

    deps = P.DeployDeps(
        run_tests=run_tests,
        restart=lambda svc: calls["restart"].append(svc),
        git_reset_hard=lambda sha: calls["reset"].append(sha),
        read_hb=read_hb,
        now=clock.now,
        sleep=clock.sleep,
        echo=lambda c, t: calls["echo"].append(t),
        head_sha=lambda: head,
        is_clean=lambda: clean,
    )
    return deps, calls


def _fresh(sha, ts):
    return {"service": "x", "sha": sha, "pid": 1, "ts": ts}


def test_request_deploy_sets_requested(tmp_path):
    s = _settings(tmp_path)
    ok, _ = P.request_deploy(s, channel_id="c1", requested_by="u")
    assert ok is True
    assert DeployState.load(state_path(s)).phase == "requested"
    # second request while pending is rejected
    ok2, msg = P.request_deploy(s, channel_id="c1")
    assert ok2 is False


def test_run_deploy_aborts_on_dirty_tree(tmp_path):
    s = _settings(tmp_path)
    clock = _Clock()
    deps, calls = _deps(clock=clock, clean=False)
    st = P.run_deploy(s, channel_id="c1", deps=deps)
    assert st.phase == "failed"
    assert calls["restart"] == []
    assert any("dirty" in t.lower() for t in calls["echo"])


def test_run_deploy_aborts_on_red_tests_after_retry(tmp_path):
    s = _settings(tmp_path)
    clock = _Clock()
    deps, calls = _deps(clock=clock, tests_ok=False)
    st = P.run_deploy(s, channel_id="c1", deps=deps)
    assert st.phase == "failed"
    assert calls["tests"] == 2          # retried once
    assert calls["restart"] == []       # never restarted


def test_run_deploy_cli_happy_path_confirms(tmp_path):
    s = _settings(tmp_path)
    clock = _Clock()
    # gateway and daemon both report the new sha, fresh
    deps, calls = _deps(clock=clock, head="newsha", hb_script={
        "gateway": [_fresh("oldsha", clock.t), _fresh("newsha", clock.t)],
        "daemon": [_fresh("newsha", clock.t)],
    })
    st = P.run_deploy(s, channel_id="c1", deps=deps, self_restart_daemon=False)
    assert st.phase == "confirmed"
    assert calls["restart"] == ["gateway", "daemon"]
    assert calls["reset"] == []


def test_run_deploy_rolls_back_when_gateway_unhealthy(tmp_path):
    s = _settings(tmp_path)
    clock = _Clock()
    # gateway never reaches newsha; rollback target is the pre-deploy gateway sha "oldsha"
    deps, calls = _deps(clock=clock, head="newsha", hb_script={
        "gateway": [_fresh("oldsha", clock.t)],  # stays old forever -> read repeatedly
    })
    st = P.run_deploy(s, channel_id="c1", deps=deps, self_restart_daemon=False)
    assert st.phase == "rolled_back"
    assert calls["reset"] == ["oldsha"]
    # restarted gateway twice (deploy attempt + rollback), daemon never
    assert calls["restart"].count("gateway") == 2
    assert "daemon" not in calls["restart"]


def test_run_deploy_daemon_executor_ends_at_daemon_refresh(tmp_path):
    s = _settings(tmp_path)
    clock = _Clock()
    deps, calls = _deps(clock=clock, head="newsha", hb_script={
        "gateway": [_fresh("oldsha", clock.t), _fresh("newsha", clock.t)],
    })
    st = P.run_deploy(s, channel_id="c1", deps=deps, self_restart_daemon=True)
    assert st.phase == "daemon_refresh"
    assert calls["restart"] == ["gateway", "daemon"]
    # state persisted as daemon_refresh for the boot-guard to pick up
    assert DeployState.load(state_path(s)).phase == "daemon_refresh"
```

NOTE on the rollback test: `read_hb("gateway")` returns "oldsha" both for the initial rollback-target read AND every health poll, so `_await_healthy(... "newsha")` times out and `_await_healthy(... "oldsha")` succeeds immediately — yielding `rolled_back` with `reset == ["oldsha"]`. The fake clock advances via `sleep` so the timeout loop terminates.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_deploy_pipeline.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bemine.deploy.pipeline'`.

- [ ] **Step 3: Write the implementation**

```python
# src/bemine/deploy/pipeline.py
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
        return st  # boot_guard confirms or rolls back

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


def default_deps(settings) -> DeployDeps:  # pragma: no cover - real side effects
    raise NotImplementedError("default_deps is implemented in Task 5")
```

(`default_deps` is a stub here so `run_deploy(..., deps=None)` has a target; Task 5 replaces it. Tests always pass `deps=`, so the stub is never hit.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_deploy_pipeline.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/bemine/deploy/pipeline.py tests/test_deploy_pipeline.py
git commit -m "feat(deploy): request_deploy + run_deploy state machine (test-gate, gateway rollback)"
```

---

## Task 4: boot_guard + maybe_confirm_deploy

**Files:**
- Modify: `src/bemine/deploy/pipeline.py`
- Test: `tests/test_deploy_bootguard.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_deploy_bootguard.py
from bemine.config import BemineSettings
from bemine.deploy.state import DeployState, state_path
from bemine.deploy import pipeline as P
from tests.test_deploy_pipeline import _Clock, _deps  # reuse fakes


def _settings(tmp_path):
    return BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")


def test_boot_guard_noop_when_not_refreshing(tmp_path):
    s = _settings(tmp_path)
    DeployState(phase="idle").save(state_path(s))
    deps, calls = _deps(clock=_Clock())
    P.boot_guard(s, deps=deps)
    assert calls["reset"] == []
    assert DeployState.load(state_path(s)).phase == "idle"


def test_boot_guard_increments_and_proceeds_under_limit(tmp_path):
    s = _settings(tmp_path)
    DeployState(phase="daemon_refresh", rollback_sha="old", boot_attempts=0).save(state_path(s))
    deps, calls = _deps(clock=_Clock())
    P.boot_guard(s, deps=deps)
    st = DeployState.load(state_path(s))
    assert st.boot_attempts == 1
    assert st.phase == "daemon_refresh"  # still pending confirm
    assert calls["reset"] == []


def test_boot_guard_rolls_back_over_limit(tmp_path):
    s = _settings(tmp_path)
    DeployState(phase="daemon_refresh", rollback_sha="old", boot_attempts=2,
                channel_id="c1").save(state_path(s))
    deps, calls = _deps(clock=_Clock())
    P.boot_guard(s, deps=deps)
    st = DeployState.load(state_path(s))
    assert st.boot_attempts == 3
    assert st.phase == "rolled_back"
    assert calls["reset"] == ["old"]
    assert "gateway" in calls["restart"] and "daemon" in calls["restart"]


def test_maybe_confirm_after_window(tmp_path):
    s = _settings(tmp_path)
    clock = _Clock()
    DeployState(phase="daemon_refresh", target_sha="newsha", boot_ts=clock.t - 61,
                channel_id="c1").save(state_path(s))
    deps, calls = _deps(clock=clock)
    P.maybe_confirm_deploy(s, deps=deps)
    st = DeployState.load(state_path(s))
    assert st.phase == "confirmed"
    assert st.boot_attempts == 0
    assert any("complete" in t.lower() for t in calls["echo"])


def test_maybe_confirm_waits_within_window(tmp_path):
    s = _settings(tmp_path)
    clock = _Clock()
    DeployState(phase="daemon_refresh", target_sha="newsha", boot_ts=clock.t - 5).save(state_path(s))
    deps, calls = _deps(clock=clock)
    P.maybe_confirm_deploy(s, deps=deps)
    assert DeployState.load(state_path(s)).phase == "daemon_refresh"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_deploy_bootguard.py -v`
Expected: FAIL — `AttributeError: module 'bemine.deploy.pipeline' has no attribute 'boot_guard'`.

- [ ] **Step 3: Append the implementation to `src/bemine/deploy/pipeline.py`**

```python
def boot_guard(settings, *, deps: DeployDeps | None = None) -> None:
    deps = deps or default_deps(settings)
    path = state_path(settings)
    st = DeployState.load(path)
    if st.phase != "daemon_refresh":
        return
    st.boot_attempts += 1
    st.updated_at = deps.now()
    st.save(path)  # persist immediately so a crash still counts this boot
    if st.boot_attempts > BOOT_ATTEMPT_LIMIT:
        deps.git_reset_hard(st.rollback_sha)
        deps.restart(GATEWAY)
        st.phase = "rolled_back"
        st.updated_at = deps.now()
        st.save(path)
        deps.echo(st.channel_id,
                  f"↩ Daemon unhealthy — rolled back to `{st.rollback_sha[:8]}`.")
        deps.restart(DAEMON)


def maybe_confirm_deploy(settings, *, deps: DeployDeps | None = None) -> None:
    deps = deps or default_deps(settings)
    path = state_path(settings)
    st = DeployState.load(path)
    if st.phase != "daemon_refresh":
        return
    if deps.now() - float(st.boot_ts) >= CONFIRM_WINDOW:
        st.phase = "confirmed"
        st.boot_attempts = 0
        st.updated_at = deps.now()
        st.save(path)
        deps.echo(st.channel_id, f"✅ Deploy complete on `{st.target_sha[:8]}`.")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_deploy_bootguard.py tests/test_deploy_pipeline.py -v`
Expected: PASS (5 + 6).

- [ ] **Step 5: Commit**

```bash
git add src/bemine/deploy/pipeline.py tests/test_deploy_bootguard.py
git commit -m "feat(deploy): daemon boot_guard + deploy confirm window"
```

---

## Task 5: real default_deps

**Files:**
- Modify: `src/bemine/deploy/pipeline.py` (replace the `default_deps` stub)
- Test: `tests/test_deploy_default_deps.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_deploy_default_deps.py
from bemine.config import BemineSettings
from bemine.deploy import pipeline as P


def test_default_deps_builds_callable_bundle(tmp_path):
    s = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    deps = P.default_deps(s)
    assert callable(deps.run_tests)
    assert callable(deps.restart)
    assert callable(deps.git_reset_hard)
    assert callable(deps.read_hb)
    assert callable(deps.echo)
    assert callable(deps.head_sha)
    assert callable(deps.is_clean)
    # now/sleep are real
    assert isinstance(deps.now(), float)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_deploy_default_deps.py -v`
Expected: FAIL — `NotImplementedError` from the stub.

- [ ] **Step 3: Replace the `default_deps` stub in `src/bemine/deploy/pipeline.py`**

Add these imports at the top of the file (with the existing ones):

```python
import subprocess
from pathlib import Path
```

Replace the stub `default_deps` with:

```python
_UNIT = {GATEWAY: "mayor-discord-gateway.service", DAEMON: "bemine-daemon.service"}


def default_deps(settings) -> DeployDeps:
    from .heartbeat import read_heartbeat, current_head_sha, repo_dir
    from bemine.jobs.project_watcher import echo_to_channel

    root = repo_dir()

    def run_tests() -> tuple[bool, str]:
        proc = subprocess.run(
            ["uv", "run", "pytest", "-q"], cwd=str(root), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False, timeout=900,
        )
        return proc.returncode == 0, proc.stdout or ""

    def restart(service: str) -> None:
        subprocess.run(["systemctl", "--user", "restart", _UNIT[service]], check=False)

    def git_reset_hard(sha: str) -> None:
        subprocess.run(["git", "-C", str(root), "reset", "--hard", sha], check=False)

    def is_clean() -> bool:
        proc = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        return not (proc.stdout or "").strip()

    return DeployDeps(
        run_tests=run_tests,
        restart=restart,
        git_reset_hard=git_reset_hard,
        read_hb=lambda service: read_heartbeat(settings, service),
        now=time.time,
        sleep=time.sleep,
        echo=echo_to_channel,
        head_sha=lambda: current_head_sha(root),
        is_clean=is_clean,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_deploy_default_deps.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bemine/deploy/pipeline.py tests/test_deploy_default_deps.py
git commit -m "feat(deploy): real default_deps (pytest/systemctl/git/heartbeat)"
```

---

## Task 6: gateway publishes heartbeats

**Files:**
- Modify: `src/bemine/discord/gateway.py`
- Test: `tests/test_gateway_heartbeat.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_gateway_heartbeat.py
from bemine.config import BemineSettings
from bemine.deploy.heartbeat import read_heartbeat
from bemine.discord.gateway import publish_gateway_heartbeat


def test_publish_gateway_heartbeat_writes_file(tmp_path, monkeypatch):
    s = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    import bemine.discord.gateway as gw
    monkeypatch.setattr(gw, "current_head_sha", lambda *a, **k: "gwsha")
    publish_gateway_heartbeat(s)
    hb = read_heartbeat(s, "gateway")
    assert hb["sha"] == "gwsha"
    assert hb["service"] == "gateway"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_gateway_heartbeat.py -v`
Expected: FAIL — `cannot import name 'publish_gateway_heartbeat'`.

- [ ] **Step 3: Implement**

In `src/bemine/discord/gateway.py`, add near the top imports:

```python
from bemine.deploy.heartbeat import write_heartbeat, current_head_sha
```

Add a module-level function (near `run_bound_agent`):

```python
def publish_gateway_heartbeat(settings) -> None:
    """Write the gateway heartbeat (best-effort)."""
    try:
        write_heartbeat(settings, "gateway", current_head_sha())
    except Exception:
        pass
```

In the `on_ready` handler (inside `_register_events`, the `async def on_ready`), after the existing `LOG.info("connected as ...")` lines, add:

```python
            publish_gateway_heartbeat(self.settings)
            asyncio.create_task(self._heartbeat_loop())
```

Add this method to the gateway class (the same class that has `_handle_allowed_message`):

```python
    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            await asyncio.to_thread(publish_gateway_heartbeat, self.settings)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_gateway_heartbeat.py -v`
Then confirm the module still imports: `uv run python -c "import bemine.discord.gateway"`.
Expected: PASS; clean import.

- [ ] **Step 5: Commit**

```bash
git add src/bemine/discord/gateway.py tests/test_gateway_heartbeat.py
git commit -m "feat(deploy): gateway publishes heartbeat on ready + every 30s"
```

---

## Task 7: daemon boot-guard + heartbeat + deploy pickup

**Files:**
- Modify: `src/bemine/jobs/daemon.py` (call `boot_guard` before `run_forever`)
- Modify: `src/bemine/jobs/core.py` (`run_forever`: heartbeat, deploy pickup, confirm)
- Test: `tests/test_jobs_daemon.py` (add cases)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_jobs_daemon.py`:

```python
def test_daemon_main_calls_boot_guard_before_run(tmp_path, monkeypatch):
    import bemine.jobs.daemon as d
    import bemine.deploy.pipeline as P
    from bemine.config import BemineSettings

    monkeypatch.setattr(BemineSettings, "load",
                        classmethod(lambda cls: BemineSettings.load(
                            state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")))
    order = []
    monkeypatch.setattr(P, "boot_guard", lambda s: order.append("boot_guard"))

    class _FakeDaemon:
        def __init__(self, *a, **k): pass
        def run_forever(self): order.append("run_forever")
    monkeypatch.setattr(d, "BemineDaemon", _FakeDaemon)
    d.main([])
    assert order == ["boot_guard", "run_forever"]


def test_run_once_publishes_daemon_heartbeat_and_confirms(tmp_path, monkeypatch):
    from bemine.config import BemineSettings
    from bemine.jobs.core import BemineDaemon
    import bemine.jobs.core as core
    import bemine.deploy.pipeline as P
    from bemine.deploy.heartbeat import read_heartbeat

    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    monkeypatch.setattr(core, "current_head_sha", lambda *a, **k: "dsha")
    called = {"confirm": 0}
    monkeypatch.setattr(P, "maybe_confirm_deploy", lambda s: called.__setitem__("confirm", 1))
    BemineDaemon(settings).run_once()
    assert read_heartbeat(settings, "daemon")["sha"] == "dsha"
    assert called["confirm"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_jobs_daemon.py -k "boot_guard or heartbeat" -v`
Expected: FAIL (boot_guard not called; daemon heartbeat not written).

- [ ] **Step 3: Modify the daemon entry**

In `src/bemine/jobs/daemon.py` `main()`, in the non-`--once` branch, call boot_guard before `run_forever`:

```python
    from bemine.deploy.pipeline import boot_guard
    boot_guard(settings)
    daemon.run_forever()
```

(Place it so it only runs in the long-running path, not under `--once`.)

- [ ] **Step 4: Modify `run_forever` and `run_once` in `src/bemine/jobs/core.py`**

Add an import near the top of `core.py`:

```python
from bemine.deploy.heartbeat import write_heartbeat, current_head_sha
```

Add a helper and a deploy step to `BemineDaemon`, and call them. Update `run_once` and `run_forever`:

```python
    def run_once(self) -> list[JobRunResult]:
        self._deploy_tick()
        results = run_due_jobs(self.settings)
        self._tick_watchers()
        return results

    def run_forever(self) -> None:
        db = BemineDB(self.settings.db_path)
        seed_default_jobs(db)
        while True:
            self._deploy_tick()
            run_due_jobs(self.settings)
            self._tick_watchers()
            time.sleep(self.poll_seconds)

    def _deploy_tick(self) -> None:
        from bemine.deploy.pipeline import run_deploy, maybe_confirm_deploy
        from bemine.deploy.state import DeployState, state_path
        try:
            write_heartbeat(self.settings, "daemon", current_head_sha())
        except Exception:
            pass
        try:
            st = DeployState.load(state_path(self.settings))
            if st.phase == "requested":
                run_deploy(self.settings, channel_id=st.channel_id, self_restart_daemon=True)
            else:
                maybe_confirm_deploy(self.settings)
        except Exception:
            pass
```

(`_tick_watchers` already exists from the watcher feature.)

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_jobs_daemon.py -v`
Expected: PASS (existing + 2 new).

- [ ] **Step 6: Commit**

```bash
git add src/bemine/jobs/daemon.py src/bemine/jobs/core.py tests/test_jobs_daemon.py
git commit -m "feat(deploy): daemon boot-guard, heartbeat, and deploy-request pickup"
```

---

## Task 8: `deploy_control` channel tool

**Files:**
- Modify: `src/bemine/context/tool_registry.py`
- Test: `tests/test_deploy_control_tool.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_deploy_control_tool.py
from pathlib import Path
from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.context.tool_registry import BemineToolRegistry, ToolContext
from bemine.deploy.state import DeployState, state_path


def _ctx(tmp_path):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    db = BemineDB(settings.db_path)
    db.upsert_bemine_session(
        session_id="s1", channel_id="c1", project_id="bemine", runtime="kimi",
        model="kimi-k2.6", device_id="cdjk", cwd="/tmp", status="active", summary="",
    )
    return settings, db, ToolContext(settings=settings, db=db, session_id="s1",
                                     project_id="bemine", cwd=Path("/tmp"))


def test_deploy_control_request(tmp_path):
    settings, db, ctx = _ctx(tmp_path)
    reg = BemineToolRegistry()
    out = reg.execute("deploy_control", {"action": "request"}, ctx)
    assert out["ok"] is True
    assert DeployState.load(state_path(settings)).phase == "requested"
    assert DeployState.load(state_path(settings)).channel_id == "c1"


def test_deploy_control_status(tmp_path):
    settings, db, ctx = _ctx(tmp_path)
    reg = BemineToolRegistry()
    out = reg.execute("deploy_control", {"action": "status"}, ctx)
    assert out["phase"] == "idle"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_deploy_control_tool.py -v`
Expected: FAIL — `unknown tool: deploy_control`.

- [ ] **Step 3: Register the tool + handler**

In `src/bemine/context/tool_registry.py`, add this `BemineTool` to the list in `default_tools()` (after the `watch_control` entry):

```python
        BemineTool(
            name="deploy_control",
            description=(
                "Deploy Bemine's own current committed code (HEAD). action='request' runs "
                "the test-gated, auto-rollback deploy (restarts the gateway + daemon, reverts "
                "if unhealthy). Use this when the user says to deploy/ship Bemine. "
                "action='status' reports the current deploy phase. The working tree must be "
                "clean (changes committed) first."
            ),
            input_schema={
                "type": "object",
                "properties": {"action": {"type": "string", "enum": ["request", "status"]}},
                "required": ["action"],
            },
            handler=_deploy_control,
            policy="project-write",
        ),
```

Add the handler near the other handlers:

```python
def _deploy_control(args: Mapping[str, Any], context: ToolContext) -> Mapping[str, Any]:
    from bemine.deploy.pipeline import request_deploy
    from bemine.deploy.state import DeployState, state_path

    action = str(args.get("action") or "status")
    session = context.db.get_bemine_session(context.session_id) or {}
    channel_id = str(session.get("channel_id") or "")

    if action == "request":
        ok, msg = request_deploy(context.settings, channel_id=channel_id,
                                 requested_by=context.session_id)
        return {"ok": ok, "message": msg}

    st = DeployState.load(state_path(context.settings))
    return {"ok": True, "phase": st.phase, "target_sha": st.target_sha[:8],
            "rollback_sha": st.rollback_sha[:8]}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_deploy_control_tool.py -v`
Then `uv run python -c "from bemine.context.tool_registry import BemineToolRegistry; BemineToolRegistry()"`.
Expected: PASS; registry constructs.

- [ ] **Step 5: Commit**

```bash
git add src/bemine/context/tool_registry.py tests/test_deploy_control_tool.py
git commit -m "feat(deploy): deploy_control channel tool (request/status)"
```

---

## Task 9: `bemine deploy` CLI

**Files:**
- Modify: `src/bemine/cli.py`
- Test: `tests/test_deploy_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_deploy_cli.py
from bemine.config import BemineSettings
from bemine.cli import main
from bemine.deploy.state import DeployState, state_path


def test_cli_deploy_status(tmp_path, capsys):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    DeployState(phase="confirmed", target_sha="abcdef123").save(state_path(settings))
    main(["--state-dir", str(settings.state_dir), "deploy", "status"])
    assert "confirmed" in capsys.readouterr().out


def test_cli_deploy_runs_pipeline(tmp_path, monkeypatch, capsys):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    import bemine.cli as cli
    called = {"run": 0}

    def fake_run_deploy(s, *, channel_id="", deps=None, self_restart_daemon=True):
        called["run"] += 1
        from bemine.deploy.state import DeployState
        return DeployState(phase="confirmed", target_sha="newsha")
    monkeypatch.setattr(cli, "run_deploy", fake_run_deploy, raising=False)

    main(["--state-dir", str(settings.state_dir), "deploy"])
    assert called["run"] == 1
    assert "confirmed" in capsys.readouterr().out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_deploy_cli.py -v`
Expected: FAIL — argparse: invalid choice 'deploy'.

- [ ] **Step 3: Add the subparser**

In `build_parser()` in `src/bemine/cli.py`, after the `watch` subcommand block, add:

```python
    deploy = sub.add_parser("deploy", help="Deploy Bemine's committed code (test-gated, auto-rollback).")
    deploy_sub = deploy.add_subparsers(dest="deploy_command", required=False)
    deploy_sub.add_parser("status", help="Show the current deploy state.")
```

- [ ] **Step 4: Add the dispatch block + import**

Near the top of `src/bemine/cli.py`, add an import:

```python
from bemine.deploy.pipeline import run_deploy
```

In `main()`, add this `elif` to the same if/elif chain as `watch`:

```python
    elif args.command == "deploy":
        from bemine.deploy.state import DeployState, state_path
        settings = _settings_from_args(args)
        if getattr(args, "deploy_command", None) == "status":
            st = DeployState.load(state_path(settings))
            print(f"phase={st.phase} target={st.target_sha[:8]} rollback={st.rollback_sha[:8]}")
        else:
            st = run_deploy(settings, self_restart_daemon=False)
            print(f"deploy {st.phase} (target={st.target_sha[:8]})")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_deploy_cli.py tests/test_cli_help.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/bemine/cli.py tests/test_deploy_cli.py
git commit -m "feat(deploy): bemine deploy + deploy status CLI"
```

---

## Task 10: docs + full-suite gate

**Files:**
- Modify: `SKILL.md`, `README.md`

- [ ] **Step 1: Update `SKILL.md`**

Under "## Important Surfaces", add:

```markdown
- Self-deploy: `src/bemine/deploy/` (heartbeat, state, pipeline). Triggered via the
  `deploy_control` channel tool or `bemine deploy`; executed by the daemon, test-gated,
  with heartbeat health checks and auto-rollback (daemon boot-guard self-heals crash-loops).
```

- [ ] **Step 2: Add a `README.md` section**

Near the jobs/daemon docs, add (match neighboring heading level):

```markdown
### Self-deploy

Ship Bemine from Discord: say "deploy bemine" in a channel (or run `bemine deploy`). The
daemon runs the full test suite, and only if it's green restarts the gateway and verifies a
fresh heartbeat on the new commit — auto-rolling back if it doesn't come up. The daemon then
refreshes itself, guarded against crash-loops. `bemine deploy status` shows the current
phase. The working tree must be clean (commit first).
```

- [ ] **Step 3: Full suite + lint**

Run:
```bash
uv run pytest -q
uv run ruff check src/bemine/deploy/ src/bemine/discord/gateway.py src/bemine/jobs/daemon.py \
  src/bemine/jobs/core.py src/bemine/context/tool_registry.py src/bemine/cli.py
```
Expected: all pass (rerun once if the known `test_workers` flake trips); ruff clean.

- [ ] **Step 4: Commit**

```bash
git add SKILL.md README.md
git commit -m "docs(deploy): document self-deploy pipeline"
```

---

## Manual verification (after implementation, before relying on it)

1. `git status` clean; note HEAD. `bemine deploy status` → `phase=idle`.
2. `bemine deploy` (CLI path) → tests run, gateway+daemon restart, ends `confirmed`; both
   heartbeats show the new SHA (`cat .bemine/deploy/heartbeat-*.json`).
3. From a Discord channel: "deploy bemine" → "🚀 Deploy requested…", then "✅ gateway live",
   "✅ deploy complete".
4. Rollback drill: commit a deliberately broken gateway change on a scratch branch, deploy,
   confirm it auto-reverts and the bot stays up. (Do this once to trust it.)

---

## Self-review notes (author)

- **Spec coverage:** heartbeat+SHA health (Tasks 1,6,7 + `_await_healthy`), test-gate w/ retry (Task 3), gateway restart+rollback (Task 3), daemon refresh + boot-guard crash-loop revert (Tasks 4,7), Discord trigger no-TOTP (Task 8) + CLI (Task 9), request/execute split via daemon pickup (Tasks 3,7), audit/echo visibility (echo on every transition; **note:** `audit_events` rows are NOT written in this plan — the spec mentions them; echo covers user-facing visibility. If audit rows are required, add `db.record_audit_event` calls in `run_deploy`/`boot_guard` as a follow-up, mirroring the watcher's audit pattern).
- **Deferred vs spec:** explicit `audit_events` writes (echo is implemented; DB audit is the gap — call out to user). Everything else matches the locked spec.
- **Type/name consistency:** `DeployDeps` field names and `DeployState` field names are identical across Tasks 3–9; service constants `GATEWAY`/`DAEMON` and `_UNIT` mapping consistent; `current_head_sha` is monkeypatched in gateway/daemon tests so it must be imported into those modules by name (Tasks 6,7 do `from bemine.deploy.heartbeat import ... current_head_sha`).
```
