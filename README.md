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
- track audit events, permission requests, credential policies, and usage samples

## Commands

```bash
uv run bemine scan
uv run bemine rundown
uv run bemine search "blocked schema"
uv run bemine tmux start bemine-demo
uv run bemine tmux send bemine-demo "echo hello"
uv run bemine tmux capture bemine-demo
uv run bemine tmux kill bemine-demo
```

Discord gateway:

```bash
uv run bemine-discord-gateway
uv run bemine-discord-send --text "bemine is online"
```

Compatibility aliases still exist:

```bash
uv run mayor-discord-gateway
uv run mayor-discord-send --text "legacy alias"
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
BEMINE_DEVICE_ID=cdjk
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
        +-- Jobs / Scheduler later
        +-- Credential Gate / Permission Requests
        +-- Observability Ledger / Usage Samples
```

SQLite is the operational index: devices, projects, sessions, commands,
permission requests, usage samples, audit events, locks, and FTS5.

Obsidian is the human memory: project pages, daily logs, decisions, handoffs,
job notes, and summaries.

## Tests

The tests are integration-oriented:

- CLI scan over fake Codex and Claude logs
- SQLite + FTS5 search verification
- Obsidian project page update verification
- security and usage ledger verification
- real tmux start/send/capture/kill flow when tmux is installed

Run:

```bash
uv run pytest -q
```

