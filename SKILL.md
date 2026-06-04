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
- Status must consider last interaction and whether the latest agent turn asks
  for input.
- Avoid legacy Hermes/Gas Town semantics unless intentionally migrating data.

## Important Surfaces

- CLI: `src/bemine/cli.py`
- Status and history: `src/bemine/status.py`, `src/bemine/discovery.py`
- Discord gateway: `src/mayor_discord/gateway.py`
- Codex dispatch: `src/mayor_discord/dispatch.py`
- Kimi handler: `src/bemine/kimi_handler.py`
- Credential inventory and gates: `src/bemine/credentials.py`,
  `src/bemine/proxy.py`, `src/bemine/totp.py`
- Jobs and daemon: `src/bemine/jobs.py`, `src/bemine/daemon.py`
- Obsidian vault sync: `src/bemine/vault.py`
- Managed workers: `src/bemine/workers.py`, `src/bemine/tmux_adapter.py`
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
