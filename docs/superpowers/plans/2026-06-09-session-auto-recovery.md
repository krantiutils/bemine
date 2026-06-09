# Session Auto-Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An always-on daemon loop that recovers recent active sessions — mechanical back-off+resubmit for transient rate-limits, mechanical auto-yes for trivial confirms, an LLM decision for ambiguous prompts — verifying every action and escalating to Discord only on repeated failure; plus a reliable tmux-Enter actuation fix and verify-after-act for the existing watcher.

**Architecture:** New `src/bemine/jobs/auto_recovery.py` with a deps-injected core (`tick_recovery`), reusing `collect_fleet_status`, `RemoteTmux`, `project_watcher.resolve_pane`/`decide`, and `session_watcher.deliver_events` for escalation. Per-session recovery state (attempts/backoff/next_retry) persists in a JSON file. Wired into `BemineDaemon` poll. Config under `[recovery]`. Spec: `docs/superpowers/specs/2026-06-09-session-auto-recovery-design.md`.

**Tech Stack:** Python 3, stdlib `json`/`re`/`time`/`dataclasses`, pytest. Reuses existing bemine modules.

---

## Conventions

- Tests: `uv run pytest <path> -v`; full: `uv run pytest -q` (known flake `tests/test_workers.py::test_workers_transfer_...` — rerun once). Lint: `uv run ruff check <paths>`.
- Settings built in tests via `BemineSettings.load(state_dir=tmp_path/".bemine", vault_dir=tmp_path/"v")`.
- `SessionStatus` fields used: `device_id, runtime, session_id, cwd, source_path, age_seconds, asks_user, ask_summary`.
- `project_watcher.resolve_pane(tmux, device, cwd) -> str|None`; `RemoteTmux.capture(device, pane) -> str`, `.send_keys(device, pane, list[str])`, `.send_text(device, pane, str)`.
- Config helpers: `from bemine.config import config_bool, config_int, config_string` (already used across the codebase).

### Locked interfaces (keep names exact across tasks)

- `auto_recovery.py`:
  - `RATE_LIMIT_DEFAULTS: list[str]`
  - `classify(pane_text: str, asks_user: bool, *, signatures: list[str]) -> str` → `"rate_limit" | "trivial_confirm" | "ambiguous" | "healthy"`
  - `RecoveryDeps` dataclass: `get_statuses(settings)->list`, `tmux`, `decide(status, pane_text)->object|None`, `escalate(event: dict)->None`, `now()->float`, `sleep(s)->None`, `audit(event_type: str, detail: str, status)->None`
  - `load_recovery_state(path)->dict`, `save_recovery_state(path, dict)->None`, `recovery_state_path(settings)->Path`, `recovery_key(status)->str`
  - `tick_recovery(settings, deps, *, now)->None`
  - `default_recovery_deps(settings)->RecoveryDeps`
  - config getters: `_cfg_max_age()`, `_cfg_backoff()`, `_cfg_max_attempts()`, `_cfg_verify_delay()`, `_cfg_signatures()`, `recovery_enabled()`

---

## Task 1: classify + signatures + state store

**Files:**
- Create: `src/bemine/jobs/auto_recovery.py`
- Test: `tests/test_recovery_classify.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_recovery_classify.py
from bemine.config import BemineSettings
from bemine.jobs.auto_recovery import (
    classify, RATE_LIMIT_DEFAULTS, load_recovery_state, save_recovery_state,
    recovery_state_path, recovery_key,
)


def test_classify_rate_limit():
    pane = "API Error: Server is temporarily limiting requests (not your usage limit) · Rate limited"
    assert classify(pane, asks_user=False, signatures=RATE_LIMIT_DEFAULTS) == "rate_limit"


def test_classify_rate_limit_wins_even_if_asks_user():
    pane = "overloaded_error: please try again\n> "
    assert classify(pane, asks_user=True, signatures=RATE_LIMIT_DEFAULTS) == "rate_limit"


def test_classify_trivial_confirm():
    assert classify("Do you want to proceed? (y/n)", asks_user=True,
                    signatures=RATE_LIMIT_DEFAULTS) == "trivial_confirm"


def test_classify_ambiguous_when_asks_user_but_not_trivial():
    assert classify("Which AWS account should I use?", asks_user=True,
                    signatures=RATE_LIMIT_DEFAULTS) == "ambiguous"


def test_classify_healthy():
    assert classify("...thinking...\nWriting file foo.py", asks_user=False,
                    signatures=RATE_LIMIT_DEFAULTS) == "healthy"


def test_state_roundtrip_and_key(tmp_path):
    s = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    p = recovery_state_path(s)
    save_recovery_state(p, {"k": {"attempts": 2}})
    assert load_recovery_state(p) == {"k": {"attempts": 2}}
    assert load_recovery_state(tmp_path / "missing.json") == {}

    class _St:
        device_id = "mbp"; runtime = "claude"; session_id = "s1"
    assert recovery_key(_St()) == "mbp:claude:s1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_recovery_classify.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bemine.jobs.auto_recovery'`.

- [ ] **Step 3: Write the implementation**

```python
# src/bemine/jobs/auto_recovery.py
from __future__ import annotations

import json
import re
from pathlib import Path

RATE_LIMIT_DEFAULTS = [
    "server is temporarily limiting requests",
    "rate limited",
    "rate_limit",
    "api error",
    "overloaded",
    "529",
    "too many requests",
    "please try again",
]

# Obvious "just say yes / press enter" prompts that are safe to auto-confirm.
_TRIVIAL_CONFIRM = re.compile(
    r"\(y/n\)|\[y/n\]|yes/no|y/n\)|proceed\?|continue\?|press enter|"
    r"do you want to (proceed|continue)|confirm\?|\bok\?\b",
    re.IGNORECASE,
)


def classify(pane_text: str, asks_user: bool, *, signatures: list[str]) -> str:
    low = (pane_text or "").lower()
    if any(sig in low for sig in signatures):
        return "rate_limit"
    if asks_user:
        if _TRIVIAL_CONFIRM.search(pane_text or ""):
            return "trivial_confirm"
        return "ambiguous"
    return "healthy"


def recovery_state_path(settings) -> Path:
    return settings.state_dir / "recovery_state.json"


def load_recovery_state(path: Path) -> dict:
    if not Path(path).exists():
        return {}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def save_recovery_state(path: Path, state: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def recovery_key(status) -> str:
    return f"{status.device_id}:{status.runtime}:{status.session_id}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_recovery_classify.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/bemine/jobs/auto_recovery.py tests/test_recovery_classify.py
git commit -m "feat(recovery): classify (rate_limit/trivial/ambiguous/healthy) + state store"
```

---

## Task 2: reliable tmux actuation (inter-key pause)

Fix the "types text but doesn't submit" race: insert a short pause between the literal text and `Enter` in `RemoteTmux.send_text`, injectable so tests stay instant.

**Files:**
- Modify: `src/bemine/jobs/remote_tmux.py`
- Test: `tests/test_remote_tmux.py` (add a case)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_remote_tmux.py`:

```python
def test_send_text_pauses_between_text_and_enter():
    pauses = []
    runner = _FakeRunner()
    rt = RemoteTmux(gateway_device="cdjk", runner=runner, pause=lambda s: pauses.append(s))
    rt.send_text("cdjk", "%1", "hello")
    # literal text, then a pause, then Enter
    assert runner.calls[-2] == ["tmux", "send-keys", "-t", "%1", "-l", "hello"]
    assert runner.calls[-1] == ["tmux", "send-keys", "-t", "%1", "Enter"]
    assert pauses and pauses[0] > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_remote_tmux.py::test_send_text_pauses_between_text_and_enter -v`
Expected: FAIL — `RemoteTmux.__init__() got an unexpected keyword argument 'pause'`.

- [ ] **Step 3: Implement**

In `src/bemine/jobs/remote_tmux.py`, add `import time` at the top if not present. Change `__init__` to accept an injectable `pause`, and use it in `send_text`:

```python
    def __init__(self, *, gateway_device: str, runner: Runner | None = None,
                 timeout: int = 15, pause=None) -> None:
        self.gateway_device = gateway_device
        self._runner = runner or self._default_runner
        self.timeout = timeout
        self._pause = pause or time.sleep
```

```python
    def send_text(self, device: str, pane_id: str, text: str) -> None:
        # -l sends text literally; pause so the TUI absorbs it before Enter submits.
        self._run(device, ["send-keys", "-t", pane_id, "-l", text])
        self._pause(0.2)
        self._run(device, ["send-keys", "-t", pane_id, "Enter"])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_remote_tmux.py -v`
Expected: PASS (existing 5 + new).

- [ ] **Step 5: Commit**

```bash
git add src/bemine/jobs/remote_tmux.py tests/test_remote_tmux.py
git commit -m "fix(recovery): pause between literal text and Enter so tmux input actually submits"
```

---

## Task 3: recovery tick driver

The deps-injected core: iterate fleet, skip stale/backoff/no-pane, classify, act per tier, verify, update backoff/escalate.

**Files:**
- Modify: `src/bemine/jobs/auto_recovery.py`
- Test: `tests/test_recovery_tick.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_recovery_tick.py
from bemine.config import BemineSettings
from bemine.index.status import SessionStatus
from bemine.jobs.auto_recovery import (
    tick_recovery, RecoveryDeps, load_recovery_state, recovery_state_path,
)


def _settings(tmp_path):
    return BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")


def _status(*, pane_age=10, asks_user=False, cwd="/Users/cdjk/github/llm/rai-speech-toolkit"):
    return SessionStatus(
        device_id="mbp", runtime="claude", session_id="s1", cwd=cwd, source_path="",
        last_interaction_at=1.0, age_seconds=pane_age, last_role="assistant",
        asks_user=asks_user, ask_summary="?", latest_summary="",
    )


class _Tmux:
    def __init__(self, before, after):
        self._seq = [before, after] * 50
        self.i = 0
        self.sent_keys = []
        self.sent_text = []
    def list_panes(self, device):
        return [{"pane_id": "%2", "path": "/Users/cdjk/github/llm/rai-speech-toolkit",
                 "command": "claude"}]
    def capture(self, device, pane):
        v = self._seq[self.i]; self.i += 1; return v
    def send_keys(self, device, pane, keys):
        self.sent_keys.append(keys)
    def send_text(self, device, pane, text):
        self.sent_text.append(text)


def _deps(statuses, tmux, *, clock, escalations, audits, decide=None):
    return RecoveryDeps(
        get_statuses=lambda s: statuses,
        tmux=tmux,
        decide=decide or (lambda status, pane_text: None),
        escalate=lambda event: escalations.append(event),
        now=clock["now"],
        sleep=lambda s: None,
        audit=lambda et, detail, status: audits.append((et, detail)),
    )


def _clock(t=1000.0):
    box = {"t": t}
    return {"now": lambda: box["t"], "box": box}


def test_rate_limit_resubmits_and_recovers(tmp_path):
    s = _settings(tmp_path)
    # before has the error; after (post-Enter) is clean -> advanced
    tmux = _Tmux("API Error: Rate limited", "working again...")
    clock, esc, aud = _clock(), [], []
    deps = _deps([_status()], tmux, clock=clock, escalations=esc, audits=aud)
    tick_recovery(s, deps, now=1000.0)
    assert tmux.sent_keys == [["Enter"]]      # resubmitted in place
    assert esc == []                          # recovered, no escalation
    assert load_recovery_state(recovery_state_path(s)) == {}  # state reset on success


def test_rate_limit_unrecovered_backs_off_then_escalates(tmp_path):
    s = _settings(tmp_path)
    # error stays both before and after -> never advances
    tmux = _Tmux("Rate limited", "Rate limited")
    clock, esc, aud = _clock(), [], []
    deps = _deps([_status()], tmux, clock=clock, escalations=esc, audits=aud)
    for i in range(5):
        clock["box"]["t"] = 1000.0 + i * 100000  # always past backoff
        tick_recovery(s, deps, now=clock["box"]["t"])
    assert len(esc) == 1                       # escalated once after max_attempts
    assert esc[0]["type"] == "needs_input"


def test_trivial_confirm_sends_yes(tmp_path):
    s = _settings(tmp_path)
    tmux = _Tmux("Do you want to proceed? (y/n)", "done")
    clock, esc, aud = _clock(), [], []
    deps = _deps([_status(asks_user=True)], tmux, clock=clock, escalations=esc, audits=aud)
    tick_recovery(s, deps, now=1000.0)
    assert tmux.sent_text == ["yes"]


def test_skips_stale_session(tmp_path):
    s = _settings(tmp_path)
    tmux = _Tmux("Rate limited", "Rate limited")
    clock, esc, aud = _clock(), [], []
    deps = _deps([_status(pane_age=99999)], tmux, clock=clock, escalations=esc, audits=aud)
    tick_recovery(s, deps, now=1000.0)
    assert tmux.sent_keys == [] and tmux.sent_text == []


def test_healthy_session_no_action(tmp_path):
    s = _settings(tmp_path)
    tmux = _Tmux("writing code", "writing code")
    clock, esc, aud = _clock(), [], []
    deps = _deps([_status()], tmux, clock=clock, escalations=esc, audits=aud)
    tick_recovery(s, deps, now=1000.0)
    assert tmux.sent_keys == [] and tmux.sent_text == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_recovery_tick.py -v`
Expected: FAIL — `ImportError: cannot import name 'tick_recovery'`.

- [ ] **Step 3: Implement**

Append to `src/bemine/jobs/auto_recovery.py`:

```python
from dataclasses import dataclass
from typing import Callable

from bemine.config import config_bool, config_int, config_string
from bemine.jobs.project_watcher import resolve_pane

_BACKOFF_CAP = 1800
_ESCALATED_HOLD = 86400  # after escalation, don't retry for a day (until state changes)


def recovery_enabled() -> bool:
    return config_bool("recovery", "enabled", True)


def _cfg_max_age() -> int:
    return config_int("recovery", "max_session_age_secs", 1800)


def _cfg_backoff() -> int:
    return config_int("recovery", "backoff_secs", 30)


def _cfg_max_attempts() -> int:
    return config_int("recovery", "max_attempts", 5)


def _cfg_verify_delay() -> int:
    return config_int("recovery", "verify_delay_secs", 4)


def _cfg_signatures() -> list[str]:
    raw = config_string("recovery", "rate_limit_signatures", "")
    if raw.strip():
        return [s.strip().lower() for s in raw.split(",") if s.strip()]
    return RATE_LIMIT_DEFAULTS


@dataclass
class RecoveryDeps:
    get_statuses: Callable
    tmux: object
    decide: Callable
    escalate: Callable
    now: Callable
    sleep: Callable
    audit: Callable


def _escalation_event(status, kind: str, pane_after: str) -> dict:
    return {
        "type": "needs_input",
        "session_id": status.session_id,
        "device_id": status.device_id,
        "cwd": status.cwd,
        "ask_summary": f"auto-recovery could not clear a `{kind}` state — needs you",
        "output_tail": (pane_after or "")[-1500:],
    }


def tick_recovery(settings, deps: RecoveryDeps, *, now: float) -> None:
    sigs = _cfg_signatures()
    max_age = _cfg_max_age()
    max_attempts = _cfg_max_attempts()
    verify_delay = _cfg_verify_delay()
    base_backoff = _cfg_backoff()
    path = recovery_state_path(settings)
    state = load_recovery_state(path)
    live_keys = set()

    for status in deps.get_statuses(settings):
        if status.age_seconds > max_age:
            continue
        key = recovery_key(status)
        live_keys.add(key)
        rec = state.get(key)
        if rec and now < float(rec.get("next_retry_at", 0)):
            continue
        pane = resolve_pane(deps.tmux, status.device_id, status.cwd)
        if not pane:
            continue
        before = deps.tmux.capture(status.device_id, pane)
        kind = classify(before, status.asks_user, signatures=sigs)
        if kind == "healthy":
            state.pop(key, None)
            continue

        acted = _act(kind, deps, status, pane, before)
        if not acted:  # ambiguous tier chose wait/escalate/done — handled inside _act
            continue

        deps.sleep(verify_delay)
        after = deps.tmux.capture(status.device_id, pane)
        if kind == "rate_limit":
            advanced = not any(sig in after.lower() for sig in sigs)
        else:
            advanced = after != before

        rec = state.get(key) or {"attempts": 0, "backoff": base_backoff}
        if advanced:
            deps.audit("recovered", kind, status)
            state.pop(key, None)
        else:
            rec["attempts"] = int(rec.get("attempts", 0)) + 1
            rec["backoff"] = min(int(rec.get("backoff", base_backoff)) * 2, _BACKOFF_CAP)
            rec["next_retry_at"] = now + rec["backoff"]
            if rec["attempts"] >= max_attempts:
                deps.escalate(_escalation_event(status, kind, after))
                deps.audit("escalated", kind, status)
                rec["next_retry_at"] = now + _ESCALATED_HOLD
            state[key] = rec

    # prune records for sessions no longer live
    for key in list(state.keys()):
        if key not in live_keys:
            state.pop(key, None)
    save_recovery_state(path, state)


def _act(kind: str, deps: RecoveryDeps, status, pane: str, before: str) -> bool:
    """Perform the recovery action. Returns True if an action was issued that should be
    verified, False if nothing actionable was done (ambiguous tier deferred/escalated)."""
    if kind == "rate_limit":
        deps.tmux.send_keys(status.device_id, pane, ["Enter"])
        deps.audit("resubmit", kind, status)
        return True
    if kind == "trivial_confirm":
        deps.tmux.send_text(status.device_id, pane, "yes")
        deps.audit("auto_yes", kind, status)
        return True
    # ambiguous -> LLM decision
    decision = deps.decide(status, before)
    if decision is None:
        return False
    dkind = getattr(decision, "kind", "wait")
    if dkind == "send_text":
        deps.tmux.send_text(status.device_id, pane, getattr(decision, "text", ""))
        deps.audit("llm_send_text", kind, status)
        return True
    if dkind == "send_keys":
        deps.tmux.send_keys(status.device_id, pane, list(getattr(decision, "keys", [])))
        deps.audit("llm_send_keys", kind, status)
        return True
    if dkind == "escalate":
        deps.escalate(_escalation_event(status, kind, before))
        deps.audit("llm_escalate", kind, status)
        return False
    return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_recovery_tick.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/bemine/jobs/auto_recovery.py tests/test_recovery_tick.py
git commit -m "feat(recovery): tick driver (tiered act + verify + backoff + escalate)"
```

---

## Task 4: default deps + daemon wiring

**Files:**
- Modify: `src/bemine/jobs/auto_recovery.py` (`default_recovery_deps`)
- Modify: `src/bemine/jobs/core.py` (`BemineDaemon._recovery_tick`, call from run loops)
- Test: `tests/test_recovery_default_deps.py`, `tests/test_jobs_daemon.py` (add case)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_recovery_default_deps.py
from bemine.config import BemineSettings
from bemine.jobs.auto_recovery import default_recovery_deps


def test_default_recovery_deps_builds(tmp_path):
    s = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    deps = default_recovery_deps(s)
    assert callable(deps.get_statuses)
    assert callable(deps.decide)
    assert callable(deps.escalate)
    assert callable(deps.audit)
    assert deps.tmux is not None
```

Add to `tests/test_jobs_daemon.py`:

```python
def test_run_once_ticks_recovery(tmp_path, monkeypatch):
    from bemine.config import BemineSettings
    from bemine.jobs.core import BemineDaemon
    import bemine.jobs.auto_recovery as ar

    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    called = {"n": 0}
    monkeypatch.setattr(ar, "recovery_enabled", lambda: True)
    monkeypatch.setattr(ar, "tick_recovery",
                        lambda s, deps, *, now: called.__setitem__("n", called["n"] + 1))
    BemineDaemon(settings).run_once()
    assert called["n"] == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_recovery_default_deps.py tests/test_jobs_daemon.py::test_run_once_ticks_recovery -v`
Expected: FAIL (no `default_recovery_deps`; recovery not ticked).

- [ ] **Step 3: Implement `default_recovery_deps`**

Append to `src/bemine/jobs/auto_recovery.py`:

```python
def default_recovery_deps(settings) -> RecoveryDeps:
    import time
    from bemine.index.status import collect_fleet_status, session_transcript_tail
    from bemine.jobs.remote_tmux import RemoteTmux
    from bemine.jobs.project_watcher import decide as _watcher_decide
    from bemine.jobs.session_watcher import deliver_events
    from bemine.discord.dispatch import _DEFAULT_MODELS
    from bemine.agent.clients import build_client_for_runtime
    from bemine.db import BemineDB

    _GENERIC_BRIEF = (
        "Keep this terminal agent session unstuck. Answer only what is needed to let it "
        "continue safely. NEVER take destructive/irreversible actions or use gated "
        "credentials — escalate instead."
    )

    def _statuses(s):
        statuses, _errors = collect_fleet_status(s)
        return statuses

    def _decide(status, pane_text):
        try:
            client = build_client_for_runtime(settings, status.runtime or "kimi")
            rt = (status.runtime or "kimi").lower()
            model = config_string(rt, "model", "") or _DEFAULT_MODELS.get(rt, "kimi-k2.6")
            tail = ""
            if status.source_path:
                try:
                    tail = session_transcript_tail(Path(status.source_path), status.runtime)
                except Exception:
                    tail = ""
            return _watcher_decide(
                client, model=model, brief=_GENERIC_BRIEF, pane_capture=pane_text,
                transcript_tail=tail, ask_summary=status.ask_summary, channel_context="",
            )
        except Exception:
            return None

    def _escalate(event):
        try:
            deliver_events(settings, [event])
        except Exception:
            pass

    def _audit(event_type, detail, status):
        try:
            BemineDB(settings.db_path).record_audit_event(
                category="recovery", event_type=event_type, actor="auto_recovery",
                device_id=status.device_id,
                payload_json=json.dumps({"detail": detail, "session": status.session_id,
                                         "cwd": status.cwd}),
            )
        except Exception:
            pass

    return RecoveryDeps(
        get_statuses=_statuses,
        tmux=RemoteTmux(gateway_device=settings.device_id),
        decide=_decide,
        escalate=_escalate,
        now=time.time,
        sleep=time.sleep,
        audit=_audit,
    )
```

- [ ] **Step 4: Wire into the daemon**

In `src/bemine/jobs/core.py`, add a `_recovery_tick` method to `BemineDaemon` and call it from `run_once` and `run_forever` (alongside the existing `_deploy_tick`/`_tick_watchers`):

```python
    def _recovery_tick(self) -> None:
        import time as _time
        from bemine.jobs import auto_recovery as ar
        try:
            if not ar.recovery_enabled():
                return
            ar.tick_recovery(self.settings, ar.default_recovery_deps(self.settings),
                             now=_time.time())
        except Exception:
            pass
```

Update `run_once` and `run_forever` to call `self._recovery_tick()` after `self._tick_watchers()`:

```python
    def run_once(self) -> list[JobRunResult]:
        self._deploy_tick()
        results = run_due_jobs(self.settings)
        self._tick_watchers()
        self._recovery_tick()
        return results

    def run_forever(self) -> None:
        db = BemineDB(self.settings.db_path)
        seed_default_jobs(db)
        while True:
            self._deploy_tick()
            run_due_jobs(self.settings)
            self._tick_watchers()
            self._recovery_tick()
            time.sleep(self.poll_seconds)
```

Note: the daemon test monkeypatches `ar.tick_recovery` and `ar.recovery_enabled`, so `_recovery_tick` must reference them via the `auto_recovery` module (as written).

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/test_recovery_default_deps.py tests/test_jobs_daemon.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/bemine/jobs/auto_recovery.py src/bemine/jobs/core.py \
  tests/test_recovery_default_deps.py tests/test_jobs_daemon.py
git commit -m "feat(recovery): real deps (status/tmux/LLM/escalate/audit) + daemon wiring"
```

---

## Task 5: verify-after-act in the watcher

Close the deferred loop-protection gap: after the watcher sends, re-capture; if the pane is unchanged across `K` consecutive sends, escalate instead of re-sending blindly.

**Files:**
- Modify: `src/bemine/jobs/project_watcher.py` (`tick_watcher` send branches; add an `unchanged_sends` counter to the watcher row via existing `update_project_watcher`)
- Test: `tests/test_watcher_verify.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_watcher_verify.py
from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.index.status import SessionStatus
from bemine.jobs.project_watcher import WatcherDeps, tick_watcher, Decision


def _setup(tmp_path):
    s = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    db = BemineDB(s.db_path)
    db.upsert_project_watcher(project_id="rai-speech-toolkit", channel_id="c1",
                              brief="b", interval_secs=300)
    return s, db


def _status():
    return SessionStatus(
        device_id="mbp", runtime="claude", session_id="s", cwd="/x/rai-speech-toolkit",
        source_path="", last_interaction_at=1.0, age_seconds=10, last_role="assistant",
        asks_user=True, ask_summary="?", latest_summary="",
    )


class _StuckTmux:
    """capture always returns the same text -> pane never advances after a send."""
    def __init__(self): self.sent = 0
    def list_panes(self, d): return [{"pane_id": "%2", "path": "/x/rai-speech-toolkit", "command": "claude"}]
    def capture(self, d, p): return "SAME PANE (unchanged)"
    def send_text(self, d, p, t): self.sent += 1
    def send_keys(self, d, p, k): self.sent += 1


def _deps(tmux, echoes):
    return WatcherDeps(get_statuses=lambda s: [_status()], tmux=tmux,
                       decide=lambda **kw: Decision(kind="send_text", text="go"),
                       echo=lambda c, t: echoes.append(t), transcript_tail=lambda s: "")


def test_watcher_escalates_when_pane_never_advances(tmp_path):
    s, db = _setup(tmp_path)
    tmux, echoes = _StuckTmux(), []
    deps = _deps(tmux, echoes)
    # Three ticks: pane never changes after sends -> should stop blindly sending and escalate.
    for i in range(3):
        tick_watcher(s, db, db.get_project_watcher("rai-speech-toolkit"), deps, now=100.0 + i)
    assert any("not responding" in t.lower() or "stuck" in t.lower() for t in echoes)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_watcher_verify.py -v`
Expected: FAIL (no escalation; watcher keeps sending).

- [ ] **Step 3: Implement**

In `src/bemine/jobs/project_watcher.py` `tick_watcher`, after computing `pane` and the decision, when the decision is `send_text`/`send_keys`, capture the pane before and after the send and track an `unchanged_sends` counter persisted on the watcher row. Add near the top of the file: `_MAX_UNCHANGED_SENDS = 2`. Replace the `send_text`/`send_keys` branches with verify-aware versions:

```python
    if decision.kind in ("send_text", "send_keys"):
        before = deps.tmux.capture(session.device_id, pane)
        if decision.kind == "send_text":
            deps.tmux.send_text(session.device_id, pane, decision.text)
            deps.echo(channel_id, f"▶ `{project_name}` sent: {decision.text}")
            _audit("send", decision.text)
        else:
            deps.tmux.send_keys(session.device_id, pane, decision.keys)
            deps.echo(channel_id, f"▶ `{project_name}` sent keys: {' '.join(decision.keys)}")
            _audit("send", " ".join(decision.keys))
        after = deps.tmux.capture(session.device_id, pane)
        unchanged = int(watcher.get("unchanged_sends", 0))
        if after == before:
            unchanged += 1
        else:
            unchanged = 0
        db.update_project_watcher(project_id, unchanged_sends=unchanged)
        if unchanged >= _MAX_UNCHANGED_SENDS:
            db.update_project_watcher(project_id, awaiting_reply=1, unchanged_sends=0)
            deps.echo(channel_id,
                      f"⚠ `{project_name}` is not responding to input ({unchanged} sends "
                      "with no change) — paused, your reply will resume it.")
```

This replaces the old separate `send_text` / `send_keys` branches (keep the `escalate`/`done`/`wait` branches as they are). Add `unchanged_sends` to the `project_watchers` table and the `update_project_watcher` whitelist:

- In `src/bemine/db.py` `SCHEMA`, add to the `project_watchers` table definition a column:
  `unchanged_sends INTEGER NOT NULL DEFAULT 0,` (place before `started_at`).
- In `update_project_watcher`'s `allowed` set, add `"unchanged_sends"`.

Note: the `decision.text.strip()` empty-guard from the earlier watcher fix should be preserved — keep skipping empty `send_text` before the verify block (wrap the send-text path in `if decision.text.strip():` as before; if empty, treat as no-op).

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_watcher_verify.py tests/test_watcher_tick.py -v`
Expected: PASS (new + existing watcher tests).

- [ ] **Step 5: Commit**

```bash
git add src/bemine/jobs/project_watcher.py src/bemine/db.py tests/test_watcher_verify.py
git commit -m "feat(recovery): watcher verifies sends advance the pane, escalates if stuck"
```

---

## Task 6: config docs + full gate

**Files:**
- Modify: `bemine.toml` (document `[recovery]`), `SKILL.md`, `README.md`

- [ ] **Step 1: Document `[recovery]` in `bemine.toml`**

Add a commented section (match the file's existing comment style):

```toml
[recovery]
# Auto-recovery watches recent active sessions and unsticks them.
enabled = true
max_session_age_secs = 1800   # ignore sessions idle longer than this (no reviving old ones)
backoff_secs = 30             # initial retry backoff; doubles per failed attempt
max_attempts = 5              # failed verifications before escalating to Discord
verify_delay_secs = 4         # wait before re-capturing to confirm an action worked
# rate_limit_signatures = "rate limited,server is temporarily limiting requests,overloaded"
```

- [ ] **Step 2: `SKILL.md` Important Surfaces bullet**

```markdown
- Auto-recovery: `src/bemine/jobs/auto_recovery.py` unsticks recent active sessions
  (rate-limit back-off+resubmit, trivial-confirm auto-yes, LLM for ambiguous), verifies every
  action, escalates on repeated failure. Tunable via `[recovery]`.
```

- [ ] **Step 3: `README.md` section**

```markdown
### Auto-recovery

Bemine watches your recent active sessions and unsticks them automatically: transient
provider rate-limits are backed-off and resubmitted in place, obvious yes/continue prompts
are auto-confirmed, and anything ambiguous is judged by a sub-agent — every action is
verified by re-reading the pane, and it only pings you if recovery keeps failing. Tune it
under `[recovery]` (e.g. `max_session_age_secs` so it never touches long-idle sessions).
```

- [ ] **Step 4: Full gate**

Run:
```bash
uv run pytest -q
uv run ruff check src/bemine/jobs/auto_recovery.py src/bemine/jobs/remote_tmux.py \
  src/bemine/jobs/project_watcher.py src/bemine/jobs/core.py src/bemine/db.py
```
Expected: all pass (rerun once for the known `test_workers` flake); ruff clean (leave the pre-existing `render_events` unused-import in core.py if present).

- [ ] **Step 5: Commit**

```bash
git add bemine.toml SKILL.md README.md
git commit -m "docs(recovery): document [recovery] config + auto-recovery"
```

---

## Manual verification (after build)

1. `cat .bemine/recovery_state.json` (created lazily). Restart daemon.
2. In a tmux session you control, paste a fake line `API Error: ... Rate limited` and leave it; within ~30s the daemon should send Enter and the line should change. Check `bemine audit list` for a `recovery`/`resubmit` event.
3. Leave a `Proceed? (y/n)` prompt; confirm it auto-answers `yes`.
4. Confirm a long-idle (> `max_session_age_secs`) session is untouched.

---

## Self-review notes (author)

- **Spec coverage:** classify+signatures (T1), resubmit-in-place rate-limit (T3 `_act`), trivial auto-yes (T3), LLM ambiguous tier (T3+T4 `_decide`), verify-after-act + backoff + escalate (T3), configurable recency + `[recovery]` (T3 getters, T6), reliable Enter actuation (T2), watcher verify-after-act (T5), escalation via `deliver_events` (T4). Refinement vs spec: actions are audited (not chat-echoed) every tick; Discord message only on escalation — to avoid spam. Noted intentionally.
- **Type consistency:** `RecoveryDeps` fields identical across T3/T4; `Decision` attribute access (`kind/text/keys`) matches `project_watcher.Decision`; `recovery_key`/state helpers consistent; daemon references `ar.tick_recovery`/`ar.recovery_enabled` to match the monkeypatch in the test.
- **Out of scope (next):** non-interactive Claude launch (#7) — separate small change.
```
