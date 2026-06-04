# bemine

Personal multi-device agent mayor.

`bemine` is the kernel for managing project work across machines, agent
runtimes, Discord, terminal/tmux, and an Obsidian memory vault.

The current implementation is the first working vertical slice:

- scan local Codex, Claude, and Kimi histories
- index sessions and projects in SQLite with FTS5
- write project status blocks into an Obsidian vault
- render project rundowns from the shared kernel
- expose the same rundown through Discord text commands
- drive tmux sessions from a typed adapter
- run a systemd-backed daemon with cron-style jobs
- track audit events, permission requests, credential policies, and usage samples

## Commands

```bash
uv run bemine scan
uv run bemine status
uv run bemine rundown
uv run bemine search "blocked schema"
uv run bemine capabilities
uv run bemine kimi status
uv run bemine jobs list
uv run bemine jobs run daily_digest --no-notify
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

```bash
uv run bemine jobs seed
uv run bemine jobs list
uv run bemine jobs run daily_digest --no-notify
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
BEMINE_TOTP_SECRET=
```

Discord settings can use `MAYOR_DISCORD_*` or fall back to Hermes-compatible
`DISCORD_*` environment variables:

```bash
MAYOR_DISCORD_BOT_TOKEN=
MAYOR_DISCORD_HOME_CHANNEL=
MAYOR_DISCORD_ALLOWED_USERS=
MAYOR_DISCORD_ALLOW_ALL_USERS=false
```

## Architecture

```text
Discord / CLI / Tmux UI
        |
        v
Bemine Kernel
        |
        +-- Project Registry
        +-- Runtime Adapters: Codex, Claude, Kimi
        +-- Device Adapters: local shell, tmux, SSH later
        +-- Memory Adapters: SQLite, Obsidian
        +-- Jobs / Scheduler
        +-- Credential Gate / Permission Requests
        +-- Observability Ledger / Usage Samples
```

SQLite is the operational index: devices, projects, sessions, commands,
permission requests, usage samples, audit events, locks, and FTS5.

Obsidian is the human memory: project pages, daily logs, decisions, handoffs,
job notes, and summaries.

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

The tests are integration-oriented:

- CLI scan over fake Codex and Claude logs
- SQLite + FTS5 search verification
- Obsidian project page update verification
- security and usage ledger verification
- TOTP-gated credential request and approval flow
- Discord approval-code redaction
- Discord per-project channel routing and Codex dispatch adapter
- Kimi handler wire-shape and usage recording
- cron scheduling, daily digest execution, and systemd unit rendering
- real tmux start/send/capture/kill flow when tmux is installed

Run:

```bash
uv run pytest -q
```
