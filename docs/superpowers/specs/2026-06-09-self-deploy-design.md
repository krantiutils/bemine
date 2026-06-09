# Self-Deploy Pipeline — Design

Date: 2026-06-09
Status: Approved (design); pending implementation plan

## Problem

The user wants to ship changes to Bemine **from Discord**, without SSHing into the
machine and asking a human Claude session to redeploy. Today, applying a code change means
manually restarting the systemd services — and a bad change can take down the very Discord
control surface used to manage the system. We need a deploy that is **triggerable from
Discord, test-gated, and self-healing** (auto-rollback) for both services.

## Constraints / facts (verified)

- Two systemd **user** services run from `/home/cdjk/bm` via `uv run`, both with
  `Restart=always`, `RestartSec=10`:
  - `bemine-daemon.service` → `uv run bemine-daemon` (runs `BemineDaemon.run_forever`, a
    poll loop; separate long-running process).
  - `mayor-discord-gateway.service` → `uv run mayor-discord-gateway` (the Discord bot).
- The gateway cannot cleanly restart itself mid-request; the daemon is a separate process
  and is the natural deploy executor.
- Both processes run from the repo working tree; Python pins module code at process start,
  so new code goes live only on restart.
- `git` is available; shell policy is `unrestricted`; `db.record_audit_event(...)` exists.

## Locked decisions (from brainstorming)

- **Health check:** heartbeat file + git SHA. A deploy is healthy only when the restarted
  service publishes a fresh heartbeat whose SHA equals the deployed SHA.
- **Trigger + gate:** triggered from Discord (or `bemine deploy` CLI), **no TOTP**. Safety
  is the hard **test-gate** (never restart on red tests) plus **automatic rollback**.
- **Scope:** restart **both** services, with a **daemon boot-guard** so a bad daemon-side
  change self-heals (crash-loop → auto-revert). Fully self-healing; no SSH needed.

## Architecture

New package `src/bemine/deploy/`:

- `heartbeat.py` — write/read per-service heartbeats.
- `state.py` — the persisted deploy state machine record (load/save).
- `pipeline.py` — `run_deploy(...)` (the core, side-effects injected) and `boot_guard(...)`.

Wiring:
- Gateway entry writes heartbeats (on ready + every ~30s).
- Daemon entry calls `boot_guard()` before `run_forever`; `run_forever` also checks for a
  `requested` deploy each poll and executes it; daemon writes heartbeats each poll.
- A `deploy_control` channel tool and a `bemine deploy` CLI are the triggers.

### Components and responsibilities

**Heartbeat (`heartbeat.py`)**
- `write_heartbeat(settings, service)` → writes `state_dir/deploy/heartbeat-<service>.json`
  = `{"service", "sha", "pid", "ts"}`. `sha` is captured once at process start via
  `git rev-parse HEAD` (cached); `ts` is epoch seconds at each write.
- `read_heartbeat(settings, service) -> dict | None`.
- `current_head_sha(repo_dir) -> str` helper (`git rev-parse HEAD`).
- Services covered: `"gateway"`, `"daemon"`.

**Deploy state (`state.py`)**
- `DeployState` dataclass: `phase` (one of `idle`, `requested`, `deploying`,
  `daemon_refresh`, `confirmed`, `rolled_back`, `failed`), `target_sha`, `rollback_sha`,
  `channel_id`, `requested_by`, `boot_attempts` (int), `requested_at`, `updated_at`.
- `load(path)` / `save(path)`; default path `state_dir/deploy/state.json`.

**Pipeline (`pipeline.py`)**
- `DeployDeps` — injectable bundle: `run_tests() -> tuple[bool, str]`,
  `restart(service: str) -> None`, `git_reset_hard(sha: str) -> None`,
  `read_hb(service: str) -> dict | None`, `now() -> float`, `sleep(seconds: float) -> None`,
  `echo(channel_id: str, text: str) -> None`, `head_sha() -> str`,
  `is_clean() -> bool`.
- `default_deps(settings) -> DeployDeps` builds the real ones (subprocess for
  pytest/systemctl/git, heartbeat reads, `echo_to_channel` reused from the watcher,
  `time`).
- `run_deploy(settings, *, channel_id, deps, self_restart_daemon) -> DeployState` — the core
  state machine (see Data flow).
- `boot_guard(settings, *, deps=None, now=None) -> None` — daemon-startup guard.
- `request_deploy(settings, *, channel_id, requested_by) -> None` — write a `requested`
  state for the daemon to pick up (used by the Discord tool).

## Data flow — `run_deploy`

Preconditions: `phase` is `idle`/`confirmed`/`failed`/`rolled_back` (else reject — a deploy
is already running).

1. If not `deps.is_clean()` → `phase=failed`, echo "❌ working tree dirty; commit first",
   save, return.
2. `target = deps.head_sha()`. `rollback = read_hb("gateway")["sha"]` if a fresh gateway
   heartbeat exists (the code currently running healthy), else `target` (no prior known-good
   — rollback becomes a no-op). Persist `phase=deploying`, `target_sha`, `rollback_sha`.
3. **Test gate:** `ok, output = deps.run_tests()`. If not ok, run once more (absorbs the
   known `test_workers` real-tmux flake). If still not ok → `phase=failed`, echo
   "❌ tests failed — not deploying:\n<tail>", save, return. **No restart happened.**
4. `deps.restart("gateway")`. Poll `deps.read_hb("gateway")` every 3s up to ~90s until
   `sha == target` and `now - ts < 45`.
5. **Gateway unhealthy** (timeout) → `deps.git_reset_hard(rollback)`,
   `deps.restart("gateway")`, poll until heartbeat `sha == rollback`. Echo
   "↩ rolled back to `<rollback>` (gateway failed health check)". `phase=rolled_back`,
   save, return. If even the rollback fails its heartbeat → echo
   "‼ rollback failed — manual intervention needed", `phase=failed`, save, return. The
   daemon is never restarted on this path (executor stays stable).
6. **Gateway healthy** → echo "✅ gateway live on `<target>`". Persist
   `phase=daemon_refresh`, `rollback_sha=rollback`, `boot_attempts=0`.
7. Daemon refresh:
   - If `self_restart_daemon` is True (Discord/daemon-executor path): echo "♻ refreshing
     daemon…", `deps.restart("daemon")` and return — this kills the executor; the
     **boot-guard** confirms or rolls back.
   - If False (CLI path): `deps.restart("daemon")`, poll `read_hb("daemon")` until
     `sha == target` and fresh (~90s). Healthy → `phase=confirmed`, echo "✅ deploy
     complete on `<target>`". Unhealthy → `git_reset_hard(rollback)`, restart both, echo
     "↩ rolled back to `<rollback>` (daemon failed health check)", `phase=rolled_back`.

## Data flow — `boot_guard` (daemon startup, before `run_forever`)

1. Load state. If `phase != daemon_refresh` → return (normal boot).
2. `boot_attempts += 1`; save **immediately** (so a crash right after still counts).
3. If `boot_attempts > 2` → the daemon is crash-looping on the new code:
   `deps.git_reset_hard(rollback_sha)`, `deps.restart("gateway")`, echo "↩ daemon unhealthy
   — rolled back to `<rollback_sha>`", `phase=rolled_back`, save, then `deps.restart("daemon")`
   (comes up on reverted code, where `phase != daemon_refresh` → normal boot). Return.
4. Otherwise proceed into `run_forever`. After the daemon survives a **confirm window**
   (first successful poll iteration AND `now - boot_ts >= 60`), set `phase=confirmed`,
   `boot_attempts=0`, echo "✅ deploy complete on `<target_sha>`", save.

Because systemd `Restart=always` relaunches a crashing daemon every 10s and the boot-guard
increments a *persisted* counter each launch, a daemon that crashes on the new code reaches
the threshold and auto-reverts without human help.

## Control surfaces

- **Channel tool `deploy_control`** (added to the tool registry): `action` in
  `{request, status}`. `request` resolves the channel_id from the session row and calls
  `request_deploy(...)` → replies "🚀 Deploy requested; running tests then shipping."
  `status` returns the current `DeployState` summary. Plain language ("deploy bemine",
  "ship it", "deploy status") routes here via the channel agent.
- **Daemon pickup:** `run_forever` checks `DeployState.phase == "requested"` each poll and
  calls `run_deploy(..., self_restart_daemon=True)`.
- **CLI:** `bemine deploy` → `run_deploy(..., self_restart_daemon=False)` (full verify, since
  the CLI process is not one of the restarted services). `bemine deploy status` prints state.

Every phase transition is echoed to the requesting channel (when known) and written to
`audit_events` (category `deploy`).

## Error handling / edge cases

- Dirty working tree → refuse (step 1).
- Tests red → refuse, services untouched (step 3).
- Concurrent request while a deploy is mid-flight → `request_deploy`/`run_deploy` reject
  with "a deploy is already in progress".
- Rollback that itself fails health check → explicit "manual intervention needed" message;
  this is the single accepted human-needed failure mode.
- Test run blocks the daemon poll (~1 min) during a deploy — acceptable; deploys are rare.

## Testing

`run_deploy` and `boot_guard` take a `DeployDeps` bundle, so all tests use fakes (no real
systemd, git, pytest, or sleeps):

- **Happy path (CLI):** clean tree, tests pass, gateway heartbeat advances to target, daemon
  heartbeat advances → `phase=confirmed`; assert restart order gateway-then-daemon.
- **Dirty tree:** `phase=failed`, no restart.
- **Red tests (with retry):** `run_tests` returns False twice → `phase=failed`, no restart;
  returns False then True → proceeds.
- **Gateway unhealthy:** heartbeat never reaches target → `git_reset_hard(rollback)` called,
  gateway restarted, `phase=rolled_back`; daemon never restarted.
- **Daemon-executor path:** `self_restart_daemon=True` → ends at `restart("daemon")` with
  `phase=daemon_refresh` persisted.
- **boot_guard confirm:** `phase=daemon_refresh`, healthy → after confirm window
  `phase=confirmed`, `boot_attempts` reset.
- **boot_guard crash-loop:** `phase=daemon_refresh`, `boot_attempts` already 2 → increments
  to 3, calls `git_reset_hard(rollback)` + restarts, `phase=rolled_back`.
- **Heartbeat:** write then read round-trips `sha`/`pid`/`ts`; `read_heartbeat` returns None
  when absent.

## Out of scope (fast-follow)

- The coding-worker that *writes* a feature from a Discord request (capability #1). This
  pipeline ships whatever is committed at HEAD.
- Deploying changes to *other* projects (this is Bemine self-deploy only).
- Detecting provider rate-limit/response-limit states in sessions and auto-recovering — a
  separate feature ("strong recovery loops") to be brainstormed next.
