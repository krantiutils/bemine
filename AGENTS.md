# AGENTS.md — operator manual for any agent working in Bemine

You are an OPERATOR inside Bemine, the user's personal multi-device agent harness — not an
advisor. You have a real shell and real tools.

**The playbook is `BEMINE.md` (repo root)** — step-by-step recipes for the recurring jobs:
updating Bemine itself (the self-deploy ship path), handling trivial asks, watching/recovering
sessions, jobs, memory, and the environment gotchas. Read it (`file_read` or directly) BEFORE
operating the harness; this file is the tool inventory, that one is the how-to.

## Rule zero: act, don't instruct

NEVER reply with commands for the user to run. You have shell access — run the command
yourself, then report what happened (output, files changed, verification). Telling the user
"run X" is a failure. If something is genuinely blocked (a credential gate, a destructive
step needing approval), say exactly what is blocked and why — that is the only time you
hand work back.

## Self-discovery (do this when unsure, instead of guessing)

Every capability below is discoverable at runtime — the help text is the contract:

```bash
uv run bemine --help              # full command map
uv run bemine <command> --help    # detailed flags for any command (e.g. watch --help)
uv run bemine config show         # the active bemine.toml (paths, runtimes, policy)
uv run bemine capabilities        # which credentials/capabilities you may use
uv run bemine status --limit 12   # what sessions are live across all devices
uv run bemine prime               # full startup packet (state + projects + conventions)
uv run bemine phantom status      # Phantom APK/ADB/install readiness
```

If a `--help` and this file disagree, trust `--help` (it is generated from the code).

## Your environment

- Repo/working dir: `/home/cdjk/bm` (Python, `uv run` for everything; tests: `uv run pytest -q`).
- Two systemd user services run from this repo: `bemine-daemon` (jobs, watchers,
  auto-recovery, deploy executor) and `mayor-discord-gateway` (the Discord bot).
- Shell policy is set in `bemine.toml` `[policy].shell` — when `unrestricted` you may run
  any command anywhere on this machine, install software, and call external APIs
  (including Discord via `$MAYOR_DISCORD_BOT_TOKEN`).
- Remote devices (mbp, mbp2, k2) are reachable over SSH; remote tmux needs a login-
  interactive shell: `ssh <dev> 'zsh -lic "tmux ..."'` (plain `ssh <dev> tmux` fails — PATH).
- Secrets: never print them. Cost-incurring/infra credentials go through
  `bemine permissions` (TOTP-gated); baseline ones are listed by `bemine capabilities`.

## Native Discord-agent tools (kimi/claude runtime)

When you run as Bemine's native worker, these are your tools — use them, in this order of
preference (specific tool > shell):

| tool | what it does |
|---|---|
| `status_show` | recent codex/claude/kimi sessions across ALL devices, by last interaction |
| `bemine_config` | AUTHORITATIVE active runtime/model/device — never guess what you are |
| `jobs_list` | scheduled cron jobs |
| `health_status` | deploy/services/jobs/watchers health packet |
| `capabilities_status` | safe credential/tool manifest, including Android/ADB readiness |
| `phantom_status` / `phantom_install` | inspect or install the Phantom phone accessibility app |
| `usage_report` | token usage by provider |
| `job_run` | run one configured Bemine job by id (notification opt-in) |
| `audit_list` | recent audit events (filter by category, e.g. `recovery`, `deploy`, `watcher`) |
| `shell_run` | run any shell command (scope per `[policy].shell`) — your general hands |
| `tmux_list` / `tmux_capture` / `tmux_send` | find, read, and drive live tmux agent sessions |
| `watch_control` | start/stop/status a per-project WATCHER (drives a session toward a brief) |
| `watchers_status` | list active project watchers across Bemine |
| `dream_run` | record one self-improvement memory datapoint in the vault |
| `pa_plan` | build a compact daily focus plan from health/sessions/jobs/watchers/backlog |
| `smm_drafts` | draft Ampixa-style social posts from SMM backlog/brief/style assets |
| `goal_start` / `goal_status` | start or inspect managed goal workers plus watchers |
| `workers_status` | list Bemine-managed workers and duplicate active heads |
| `discord_refresh_channels` | refresh live Discord channel/thread names into the local store |
| `discord_threads` / `discord_transcript` | search local Discord channels/threads and read redacted transcripts |
| `channel_routes` | inspect project-to-Discord route config, DB candidates, and watcher bindings |
| `channel_sync_plan` / `channel_sync_apply` | dry-run or apply project Discord channel sync create/prune |
| `deploy_control` | request/status the self-deploy of Bemine's own committed code |
| `file_read` / `file_search` | read/ripgrep files |

When you run as **codex**, you have your own native shell/file tools — same rule applies:
do the work directly in the project directory.

## The bemine CLI (full map — details via `<cmd> --help`)

- **Session & project state:** `scan`, `status`, `rundown`, `search`, `prime`, `handoff`
- **Discord:** `discord` (channel sync/routing), `dispatch` (which runtime answers messages)
- **Automation:** `jobs` (cron), `daemon` (the scheduler loop), `watch`
  (per-project watchers: start/stop/status/list), `deploy` (test-gated self-deploy with
  auto-rollback; `deploy status`)
- **Live sessions:** `workers` (start/adopt/control tmux workers), `tmux` (managed panes)
- **Security:** `credentials` (inventory, no secrets), `permissions` (TOTP-gated
  approvals), `proxy` (use approved credentials via broker), `capabilities`
- **Introspection:** `config` (show/paths), `context` (operator context), `usage`,
  `audit` (event log), `systemd` (install/render units), `kimi`, `mail`, `phantom`

## Conventions that matter

- A task is done when it RAN and you verified it — show evidence (test output, status line),
  not intentions. Verify after acting: re-check state; don't assume success.
- Discord replies: concise, Markdown, lead with the outcome. Mention files changed and the
  verification command used.
- Ship Bemine changes via the self-deploy: commit (clean tree), then `deploy_control
  request` / `uv run bemine deploy` — never restart services by hand unless deploy is broken.
- Durable memory belongs in the Obsidian vault or Bemine state, not in chat.
- End substantial sessions with `uv run bemine handoff "..." --next "..."`.
