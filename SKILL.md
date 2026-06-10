---
name: bemine-harness
description: Use when working on Bemine, the user's personal multi-device agent harness for Discord, tmux, Codex, Claude, Kimi, Obsidian memory, project status, scheduled jobs, credential gates, and observability.
---

# Bemine Harness

Bemine is a proper system, not a loose script pile. Treat it as the central
state and interaction layer for the user's agent work across machines.

## First Checks

Run these before changing behavior:

```bash
uv run bemine status --limit 12
uv run bemine prime --no-scan
uv run bemine workers list
uv run bemine capabilities
uv run bemine jobs list
systemctl --user is-active bemine-daemon mayor-discord-gateway
```

For Discord routing issues:

```bash
mayor-discord-gateway --check
journalctl --user -u mayor-discord-gateway -n 100 --no-pager
```

## Design Rules

- Keep core behavior modular: discovery, status, routing, dispatch, memory,
  credentials, jobs, notifications, and observability stay separable.
- Prefer real end-to-end tests over mocks when the behavior crosses tmux,
  CLI dispatch, filesystem state, SQLite, or systemd rendering.
- A feature is not functional until it is wired into CLI or Discord and has a
  test proving the intended workflow.
- Start substantial sessions with `bemine prime`; end substantial sessions with
  `bemine handoff`.
- Do not leak secrets. Redact operator context before injecting it into prompts.
- Baseline identity credentials can be advertised as capabilities; costly or
  infrastructure credentials must go through permission requests.
- Do not inject commands into arbitrary raw TTYs. Only use safe transports such
  as managed tmux panes or an explicit app-server/session adapter.
- Use `bemine workers` for live control. It can start owned tmux workers and
  adopt existing tmux sessions; raw TTY sessions remain read-only via status.
- Read `bemine.toml` or run `bemine config show` before assuming paths,
  devices, Discord routing, runtime, or model choices. Runtime/model selection
  is configuration-level, not per-message syntax.
- Status must consider last interaction and whether the latest agent turn asks
  for input.
- Avoid legacy Hermes/Gas Town semantics unless intentionally migrating data.

## Important Surfaces

- CLI: `src/bemine/cli.py`
- Status and history: `src/bemine/index/status.py`, `src/bemine/index/discovery.py`
- Discord gateway: `src/bemine/discord/gateway.py`
- Runtime dispatch: `src/bemine/discord/dispatch.py` uses `bemine.toml` for the
  default runtime/model and keeps per-project runtime selectable.
- Kimi API handler: `src/bemine/kimi_handler.py` is for direct API checks;
  Discord project dispatch uses local Kimi CLI tool mode from
  `src/bemine/discord/dispatch.py`.
- Credential inventory and gates: `src/bemine/security/credentials.py`,
  `src/bemine/security/proxy.py`, `src/bemine/security/totp.py`
- Jobs and daemon: `src/bemine/jobs/core.py`, `src/bemine/jobs/daemon.py`
- Session watcher: `src/bemine/jobs/session_watcher.py` emits started/done/needs-input
  Discord updates, creates/routes project channels on first active sighting, and
  re-arms undelivered events for the next daemon poll.
- Project watcher: `src/bemine/jobs/project_watcher.py` drives a tmux-hosted session
  toward a per-project brief; `src/bemine/jobs/remote_tmux.py` runs tmux locally or over
  SSH. Control via the `watch_control` channel tool or `bemine watch`.
- Goal sessions: `src/bemine/goals.py` is the worker+watcher fused path. Use
  `bemine goal <project> "<brief>"` when the user wants Bemine to start and run
  a goal itself instead of manually opening a terminal first. It resolves local
  projects first, then fleet status, and can start remote tmux workers via SSH.
  Inspect active goal state with `bemine goal --list` or
  `bemine goal --status <project-or-worker> --json`.
- cdjk continuity: `src/bemine/workers/workers.py::resume_missing_local_workers`
  and `BemineDaemon._continuity_tick()` restart recorded `running` local managed
  tmux workers after line cuts/reboots. Configured under `[continuity]`.
- Android/Phantom phone state is exposed through `bemine capabilities` under the
  non-secret tools section (`adb`, attached device states, Android SDK path).
- Subagent profiles: `src/bemine/context/subagents.py` loads configured Markdown
  profiles such as `subagents/PA.md` and `subagents/SMM.md`; inspect them with
  `bemine subagents list`.
- PA/SMM backlog: `src/bemine/backlog.py` writes durable Markdown entries under
  the Obsidian vault `Backlog/`; use `bemine backlog add/list` or explicit
  Discord phrases like `SMM idea: ...`.
- PA/SMM brief: `src/bemine/briefs.py` deterministically turns recent
  sessions/messages/audit/tool state into `Briefs/<date>-pa-smm.md` and
  non-duplicate PA/SMM backlog nudges; run with `bemine brief run` or the
  daemon's seeded `pa_smm_brief` cron job. Routed agents can call the
  `brief_run` local tool directly.
- Dream memory: `src/bemine/dreams.py` condenses recent Discord/audit history
  into one JSONL record under `Dreams/dreams.jsonl`; run manually with
  `bemine dream run` or via the daemon's seeded `dream` cron job.
- Self-deploy: `src/bemine/deploy/` (heartbeat, state, pipeline). Triggered via the
  `deploy_control` channel tool or `bemine deploy`; executed by the daemon, test-gated,
  with heartbeat health checks and auto-rollback (daemon boot-guard self-heals crash-loops).
- Obsidian vault sync: `src/bemine/index/vault.py`
- Managed workers: `src/bemine/workers/workers.py`, `src/bemine/workers/tmux_adapter.py`
- Auto-recovery: `src/bemine/jobs/auto_recovery.py` enumerates live agent tmux panes across
  devices (via `RemoteTmux`, not the recency-ranked status list) and unsticks them
  (rate-limit back-off+resubmit, trivial-confirm auto-yes — never destructive), verifies every
  action by re-reading the pane, escalates on repeated failure. Tunable via `[recovery]`.
- Harness identity: `SOUL.md`

## Self Improvement Loop

When the user hits friction:

1. Reproduce or inspect the live failure.
2. Patch the smallest architectural surface that makes the path real.
3. Add a test that would have caught the failure.
4. Update `README.md`, `SKILL.md`, or `SOUL.md` only when the behavior or
   operating contract changed.
5. Record a handoff when the work changes the harness state meaningfully.
6. Restart the relevant service and verify the live path.

## Discord Expectations

Discord should feel like a natural control surface:

- react quickly when a message is seen;
- show typing while work is running;
- keep general chat light;
- route project work into per-project channels;
- run plain-language operator requests through the local tool router before
  falling through to project dispatch;
- show local tool calls such as `tool: config.update_dispatch` in replies;
- include the last `[discord].context_messages` messages from the same channel
  in tool routing and dispatch prompts;
- inspect stored Discord context with `bemine discord threads` and
  `bemine discord transcript` when live API credentials are unavailable;
- support checklist writes such as shopping/buy list additions through
  `checklist.add`;
- return worker output directly on success;
- prefix failures clearly with the dispatch issue.
- use `BEMINE_CODEX_SANDBOX=danger-full-access` by default unless a stricter
  Codex sandbox is proven to work in the service environment.

## Worker Prompt Expectations

Every dispatched worker should know:

- the project root and Discord channel;
- the user's exact request and saved attachments;
- redacted operator context such as `~/deploy.md`;
- credential capabilities and permission rules;
- that durable memory belongs in the Obsidian vault or Bemine state;
- that final replies must be concise and Discord-suitable.
