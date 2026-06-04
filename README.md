# bemine

Personal multi-device agent mayor.

`bemine` is the kernel for managing project work across machines, agent
runtimes, Discord, terminal/tmux, and an Obsidian memory vault.

The current implementation is a working vertical slice, not the full mayor yet:

- scan local Codex, Claude, and Kimi histories
- index sessions and projects in SQLite with FTS5
- write project status blocks into an Obsidian vault
- render project rundowns from the shared kernel
- expose the same rundown through Discord text commands
- drive tmux sessions from a typed adapter
- run a systemd-backed daemon with cron-style jobs
- run scheduled reminders through the same daemon and Discord sender
- check local Proton Mail Bridge readiness
- track audit events, permission requests, credential policies, and usage samples

Functional now:

- `bemine status` gives recent Codex, Claude, and Kimi sessions across known SSH devices.
- `bemine rundown` gives indexed project summaries from SQLite/Obsidian.
- Discord `status`, `rundown`, credential approval commands, and per-project Codex dispatch work.
- Per-project Discord channels route messages into `codex exec --cd <project>`.
- Image attachments are passed to Codex; non-image attachments are saved and listed in the prompt.
- `bemine-daemon` runs `daily_digest` and reminder jobs from systemd.
- `bemine mail bridge-status` verifies Proton Bridge process and local IMAP/SMTP ports.
- `bemine context show` renders redacted operator runbooks such as `~/deploy.md`.

Not functional yet:

- Claude dispatch and Kimi dispatch from Discord project channels.
- Attaching to existing live Codex/Claude/Kimi TUI sessions and injecting text into them.
- Proton Mail IMAP ingestion and schedule extraction from email.
- Calendar sync beyond Bemine cron/reminder jobs.
- Credential proxy operations that actually perform AWS/GitHub/HF actions; current proxy is an approval ledger and broker contract.
- Cross-device project transfer.

## Commands

```bash
uv run bemine scan
uv run bemine status
uv run bemine rundown
uv run bemine search "blocked schema"
uv run bemine capabilities
uv run bemine context show
uv run bemine kimi status
uv run bemine mail bridge-status
uv run bemine jobs list
uv run bemine jobs add-reminder morning_plan "30 8 * * *" --text "Review calendar, inbox, and blockers"
uv run bemine jobs run daily_digest --no-notify
uv run bemine jobs run morning_plan --no-notify
uv run bemine daemon --once
uv run bemine systemd install --now
uv run bemine credentials scan
uv run bemine credentials list
uv run bemine permissions request fmw:legacy --reason "need legacy env for migration"
uv run bemine permissions ask aws:any --reason "need AWS SSO for deploy smoke test"
uv run bemine permissions approve perm_xxxxx --totp 123456
uv run bemine proxy check perm_xxxxx
uv run bemine tmux start bemine-demo
uv run bemine tmux send bemine-demo "echo hello"
uv run bemine tmux capture bemine-demo
uv run bemine tmux kill bemine-demo
```

Discord gateway:

```bash
uv run bemine-discord-gateway
uv run bemine-discord-send --text "bemine is online"
uv run bemine discord sync-project-channels --guild-id "$MAYOR_DISCORD_GUILD_ID"
```

Compatibility aliases still exist:

```bash
uv run mayor-discord-gateway
uv run mayor-discord-send --text "legacy alias"
```

## Daemon And Jobs

`bemine-daemon` is the one background process. It seeds and runs jobs from the
SQLite `jobs` table, recording each execution in `job_runs`.

The first built-in job is `daily_digest`, scheduled at `0 18 * * *` local time.
It scans local histories, checks recent fleet status on `local,mbp,mbp2,k2`,
builds a project rundown, records the run, and sends the digest to Discord when
`notify_discord` is enabled.

Reminder jobs are also real daemon jobs. They use five-field cron schedules and
can be used for routine management:

```bash
uv run bemine jobs seed
uv run bemine jobs list
uv run bemine jobs add-reminder morning_plan "30 8 * * *" --text "Review calendar, inbox, and blockers"
uv run bemine jobs add-reminder evening_review "0 18 * * *" --text "Review active sessions and write handoffs"
uv run bemine jobs run daily_digest --no-notify
uv run bemine jobs run morning_plan --no-notify
uv run bemine-daemon --once --json
```

Persistent startup uses user systemd units:

```bash
uv run bemine systemd render
uv run bemine systemd install --now
systemctl --user status bemine-daemon mayor-discord-gateway
```

## Configuration

Runtime state defaults to `./.bemine`.

The Obsidian vault defaults to `~/Documents/Mayor`.

```bash
BEMINE_HOME=/path/to/state
BEMINE_VAULT=/path/to/obsidian-vault
BEMINE_CODEX_HOME=~/.codex
BEMINE_CLAUDE_HOME=~/.claude
BEMINE_KIMI_HOME=~/devlog/raw/kimi
BEMINE_HERMES_HOME=~/.hermes
BEMINE_DEVICE_ID=cdjk
BEMINE_IGNORED_PROJECT_ROOTS=~/gt
BEMINE_OPERATOR_CONTEXT_FILES=~/deploy.md
BEMINE_TOTP_SECRET=
```

`BEMINE_IGNORED_PROJECT_ROOTS` is a `:`-separated list on Linux/macOS. When it
is not set, Bemine ignores `~/gt` as a legacy workspace root so migrated
projects do not reappear from old session history.

`BEMINE_OPERATOR_CONTEXT_FILES` is a `:`-separated list of runbook files. When
it is not set, Bemine reads `~/deploy.md`. Context is redacted before it is
shown or injected into worker prompts.

Discord settings can use `MAYOR_DISCORD_*` or fall back to Hermes-compatible
`DISCORD_*` environment variables:

```bash
MAYOR_DISCORD_BOT_TOKEN=
MAYOR_DISCORD_HOME_CHANNEL=
MAYOR_DISCORD_ALLOWED_USERS=
MAYOR_DISCORD_ALLOW_ALL_USERS=false
```

## Mail And Routine

Routine management currently means daemon-backed cron jobs plus Discord
notifications. Use `jobs add-reminder` for recurring personal reminders and
`jobs list` to see next runs.

Proton Mail Bridge is treated as a local integration point. Bemine can verify
that Bridge is installed, the process is running, and IMAP/SMTP ports are open:

```bash
uv run bemine mail bridge-status
uv run bemine mail bridge-status --json
```

Default Bridge endpoints:

```bash
IMAP 127.0.0.1:1143
SMTP 127.0.0.1:1025
```

Mail ingestion is not wired yet. The intended next slice is read-only IMAP
ingestion from Proton Bridge into SQLite, redacted message summaries, and
routine/schedule extraction into reminder jobs.

## Architecture

```text
Discord / CLI / Tmux UI
        |
        v
Bemine Kernel
        |
        +-- Project Registry
        +-- Runtime Adapters: Codex, Claude, Kimi
        +-- Device Adapters: local shell, tmux, SSH status
        +-- Memory Adapters: SQLite, Obsidian
        +-- Jobs / Scheduler
        +-- Mail Adapter: Proton Bridge status now, IMAP ingestion next
        +-- Credential Gate / Permission Requests
        +-- Observability Ledger / Usage Samples
```

SQLite is the operational index: devices, projects, sessions, commands,
permission requests, usage samples, audit events, locks, and FTS5.

Obsidian is the human memory: project pages, daily logs, decisions, handoffs,
job notes, and summaries.

## Operator Context

Every Discord-routed Codex worker receives a redacted operator context block.
By default this comes from `~/deploy.md`, so workers know how to approach
Hetzner, Porkbun DNS, Traefik, Docker deploys, and similar shared operational
procedures.

Inspect exactly what workers see:

```bash
uv run bemine context show
uv run bemine context show --json
```

The context renderer redacts private keys, common API tokens, and assignment
values for keys such as `PORKBUN_API_KEY`, `PORKBUN_SECRET_API_KEY`,
`HF_TOKEN`, `AWS_SECRET_ACCESS_KEY`, and `PASSWORD`.

Credential policy still applies. If a worker needs Hugging Face, it should run
`bemine capabilities` and use baseline shared HF identity when present. If it
needs gated infrastructure secrets such as AWS, Google, or Porkbun credentials,
it must create a permission request and wait for approval.

## Status

`bemine status` reports recent Codex, Claude, and Kimi interactions ordered by
session log write time across the local machine and known SSH devices
(`mbp`, `mbp2`, `k2`, `pi`, `windows`). It treats "active" as last interaction,
not just whether a process exists.

Each item includes an `ask:` line. `ask: yes` means the latest agent message
appears to be waiting for user input, approval, permission, or clarification.
Summaries are intentionally short and redact common one-time codes and tokens.

```bash
uv run bemine status
uv run bemine status --local-only
uv run bemine status --devices local,mbp2 --json
```

Discord `status`, `bemine status`, and `mayor status` use the same status
logic.

## Discord Project Channels

`bemine discord sync-project-channels` creates one text channel per indexed
project under `Bemine Projects`. If the category reaches Discord's 50-channel
limit, Bemine creates numbered overflow categories such as `Bemine Projects 2`.

Project channels are a routing signal, not the only place to talk to Bemine.
Messages in project channels dispatch directly to that project. Messages in
general channels can route by mentioned channel, explicit text such as `tts:`
or `#bemine`, or fall back to the mayor project when no project is clear.

Dispatched messages use the selected project root as `--cd`. Image attachments
are passed to Codex with `--image`; other attachments are saved and listed in
the prompt. The gateway reacts when it receives work, shows Discord typing while
Codex runs, and replies with the final Codex message.

## Credential Gate

Credential access is classified into baseline shared identity and gated
cost-incurring or legacy-secret access.

Baseline shared identity examples:

- GitHub personal account
- GitHub `ampixa` org access
- Codeberg account
- Hugging Face `ampixa` org token
- Git signing layer

Gated examples:

- `fmw:legacy`
- AWS credentials
- Google API credentials
- paid provider credentials

Gated credentials require permission requests and can require TOTP:

```bash
uv run bemine credentials scan
uv run bemine capabilities
uv run bemine permissions request aws:any --reason "deploy smoke test"
uv run bemine permissions ask aws:any --reason "deploy smoke test"
uv run bemine permissions approve perm_xxxxx --totp 123456
```

`bemine capabilities` is the worker-facing manifest. It lists available identity
capabilities and tells workers to use `bemine permissions ask ...` and
`bemine proxy ...` for gated credentials instead of reading secret files
directly.

`bemine permissions ask ...` creates a permission request and sends a Discord
notification to the configured home channel. `permissions request` remains the
quiet primitive for tests and local workflows.

The legacy `~/.fmw` bundle on `cdjk-mbp` is declared as `fmw:cdjk-mbp` and is
proxy-only. Workers should never receive the raw keys; they receive an approved
proxy path after TOTP-gated approval.

Discord supports:

```text
bemine credentials
bemine permissions
bemine approve perm_xxxxx 123456
bemine deny perm_xxxxx
```

Approval codes are redacted before Discord messages are stored locally.

## Tests

The agent working on Bemine is responsible for running relevant tests before
claiming a change is done. For dispatcher, daemon, Discord, credential, and
mail changes, run the full suite unless there is a clear reason not to.

The tests are integration-oriented:

- CLI scan over fake Codex and Claude logs
- SQLite + FTS5 search verification
- Obsidian project page update verification
- security and usage ledger verification
- TOTP-gated credential request and approval flow
- Discord approval-code redaction
- Discord per-project channel routing and Codex dispatch adapter
- Kimi handler wire-shape and usage recording
- cron scheduling, daily digest/reminder execution, and systemd unit rendering
- Proton Bridge readiness checks
- operator context redaction and prompt injection
- real tmux start/send/capture/kill flow when tmux is installed

Run:

```bash
uv run pytest -q
```

Useful live smoke checks on this machine:

```bash
uv run bemine status
uv run bemine jobs list
uv run bemine jobs run daily_digest --no-notify
uv run bemine mail bridge-status
uv run bemine context show
systemctl --user is-active bemine-daemon mayor-discord-gateway
```
