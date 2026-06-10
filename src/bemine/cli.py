from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from textwrap import dedent

from .capabilities import build_capability_manifest, render_capability_manifest
from .backlog import add_backlog_item, default_backlog_owner, list_backlog_items, render_backlog_items
from .config import BemineSettings, render_toml_config
from .config import config_path as bemine_config_path
from .config import config_string, load_bemine_config
from .security.credentials import CredentialInventory, PermissionGate
from .jobs import (
    BemineDaemon,
    render_jobs,
    run_job_by_id,
    seed_default_jobs,
    upsert_reminder_job,
)
from .goals import render_goal_start, start_goal
from .mail_bridge import check_proton_bridge, render_proton_bridge_status
from .kimi_handler import KimiHandler, render_kimi_status, usage_payload
from .kernel import BemineKernel
from .security.notifications import notify_permission_request_via_discord
from .context.operator_context import load_operator_context, render_operator_context
from .context.subagents import load_subagent_context, render_subagent_context
from .security.proxy import CredentialProxy
from .context.rituals import (
    build_prime_packet,
    record_handoff,
    render_prime_packet,
    summary_from_args,
)
from .index.status import collect_fleet_status, render_status
from .jobs.systemd import install_user_units, render_user_units
from .workers.tmux_adapter import TmuxAdapter
from .workers.workers import (
    WorkerController,
    WorkerError,
    render_duplicate_heads,
    render_transfer,
    render_workers,
)
from .deploy.pipeline import run_deploy


HELP_FORMATTER = argparse.RawDescriptionHelpFormatter

MAIN_HELP = """\
Bemine is the user's personal multi-device agent harness.

It indexes Codex, Claude, and Kimi histories; renders project state; routes
Discord work; records vault memory; manages cron jobs; and gates credentials.

New agent quickstart:
  uv run bemine prime
  uv run bemine status --limit 12
  uv run bemine rundown --limit 12
  uv run bemine capabilities
  uv run bemine handoff "what changed" --next "next action" --test "uv run pytest -q"

Core concepts:
  prime         startup packet for a worker/session
  handoff       shutdown packet persisted to vault + audit log
  status        recent sessions by last interaction and ask-state
  rundown       project summaries from SQLite/Obsidian
  capabilities  safe credential/capability manifest for workers
  subagents     configured PA/SMM/profile Markdown injected into workers
  backlog       durable PA/SMM idea and task backlog in the vault
  permissions   explicit approval path for gated credentials
  workers       safe managed live sessions over tmux
  goal          start a managed worker and watcher from one goal brief
  watch         per-project watchers that drive a session toward a goal
  deploy        test-gated self-deploy of Bemine with auto-rollback
  audit         query the operational event log
  discord       project channel routing and Discord sync

Self-discovery: `bemine <command> --help` is the contract for every command.
The full operator manual for agents lives in AGENTS.md at the repo root.
"""


PRIME_HELP = """\
Render the startup packet a new worker should read first.

By default this scans local Codex/Claude/Kimi histories, then prints current
project identity, harness files, operator context files, recent sessions, and a
project rundown. Use --fleet when the worker needs cross-device status.

Examples:
  uv run bemine prime
  uv run bemine prime --fleet --limit 12
  uv run bemine prime --cwd /home/cdjk/bm --json
"""


HANDOFF_HELP = """\
Record the shutdown packet for substantial work.

The handoff writes a timestamped Markdown file under the Obsidian vault
Handoffs/ directory, appends the daily note, and records an audit event. Use it
before ending a meaningful session so another agent can continue cleanly.

Examples:
  uv run bemine handoff "fixed Discord routing" --test "uv run pytest -q"
  uv run bemine handoff --stdin --next "wire Discord command"
  uv run bemine handoff "blocked on AWS SSO" --blocker "needs TOTP approval"
"""


STATUS_HELP = """\
Show recent Codex, Claude, and Kimi sessions ordered by last interaction.

Status is based on session history, not just process presence. The ask-state
tries to identify whether the latest agent message needs user input.

Examples:
  uv run bemine status --limit 12
  uv run bemine status --local-only --json
  uv run bemine status --devices local,mbp2,k2
"""


CAPABILITIES_HELP = """\
Print the worker-facing credential and infrastructure manifest.

Workers should use this before assuming access. Baseline identities may be
available, while AWS/Google/Porkbun/legacy secret access must go through
permissions and proxy paths.

Examples:
  uv run bemine capabilities
  uv run bemine capabilities --json
"""


SUBAGENTS_HELP = """\
Inspect configured Markdown subagent profiles.

Subagents are named operating contracts such as PA.md and SMM.md. They are
configured in [subagents].files and injected as bounded, redacted context for
Discord-routed workers.

Examples:
  uv run bemine subagents list
  uv run bemine subagents show SMM
  uv run bemine subagents list --json
"""


BACKLOG_HELP = """\
Capture or list durable PA/SMM backlog items.

Backlog entries are Markdown in the Obsidian vault under Backlog/<owner>.md and
are audit logged. Owners are configured in [backlog].owners.

Examples:
  uv run bemine backlog add "post idea: Rai training checkpoint chart" --owner SMM
  uv run bemine backlog add "follow up on stuck Claude asks" --owner PA --project bm
  uv run bemine backlog list --owner SMM
"""


AUDIT_HELP = """\
Inspect Bemine's operational event log.

Use this when something acted weird and you need the ledger: worker transfers,
credential approvals, dispatch calls, job runs, and usage-related events.

Examples:
  uv run bemine audit list --limit 20
  uv run bemine audit list --category worker --event-type worker_transfer --json
  uv run bemine audit list --project-id bm
"""


PERMISSIONS_HELP = """\
Create, inspect, approve, or deny gated credential requests.

Use "ask" from workers because it notifies Discord by default. Use "request"
for quiet local/test flows. Approval may require TOTP depending on the
credential profile.

Examples:
  uv run bemine permissions ask aws:any --reason "deploy smoke test"
  uv run bemine permissions list --status pending
  uv run bemine permissions approve perm_xxxxx --totp 123456
"""


DISCORD_HELP = """\
Manage Discord project routing.

Project channels route messages into the corresponding project root. The sync
command creates/updates Discord channels from indexed projects. The local route
file is useful for gateway startup and tests.

Examples:
  uv run bemine discord write-project-channels --limit 200
  uv run bemine discord sync-project-channels --guild-id "$MAYOR_DISCORD_GUILD_ID"
  uv run mayor-discord-gateway --check
"""


JOBS_HELP = """\
Manage daemon-backed cron jobs and reminders.

Jobs are stored in SQLite and run by bemine-daemon. They can notify Discord or
run quietly for tests/manual checks.

Examples:
  uv run bemine jobs list
  uv run bemine jobs add-reminder morning_plan "30 8 * * *" --text "Review blockers"
  uv run bemine jobs run daily_digest --no-notify
"""


TMUX_HELP = """\
Interact with tmux-backed managed sessions.

This is the safe live-control transport Bemine supports today. Do not inject
text into arbitrary raw TTYs; use managed tmux sessions or a future app-server
adapter.

Examples:
  uv run bemine tmux start bemine-demo --command bash
  uv run bemine tmux send bemine-demo "echo hello"
  uv run bemine tmux capture bemine-demo
"""


WORKERS_HELP = """\
Start, adopt, inspect, and control Bemine-owned live workers.

Use this instead of writing into arbitrary terminal TTYs. Bemine can safely
control sessions it starts, and it can adopt existing tmux sessions. Raw
Codex/Claude/Kimi terminals that are not in tmux remain read-only status until
an app-server/PTY adapter exists.

Examples:
  uv run bemine workers start bm-codex --runtime codex --cwd /home/cdjk/bm
  uv run bemine workers adopt old-tmux --runtime claude --cwd /home/cdjk/github/project
  uv run bemine workers send bm-codex "Run bemine prime and report blockers"
  uv run bemine workers transfer bm-codex bm-claude --runtime claude
  uv run bemine workers duplicates
  uv run bemine workers capture bm-codex
  uv run bemine workers stop bm-codex
  uv run bemine workers forget bm-codex
"""


GOAL_HELP = """\
Start a managed goal session.

This fuses the worker and watcher paths: Bemine starts a tmux-backed worker in
the project, sends it the goal brief, and registers a watcher that keeps driving
that worker until the goal is done or blocked.

Examples:
  uv run bemine goal rai-speech-toolkit "train the first Rai TTS checkpoint"
  uv run bemine goal rai-speech-toolkit "eval against neptts-bench" --cwd ~/github/rai-speech-toolkit
  uv run bemine goal rai-speech-toolkit "resume training" --target-device mbp
  uv run bemine goal streaming-tts "find where this project exists across machines" --runtime claude --json
"""


def _settings_from_args(args: argparse.Namespace) -> BemineSettings:
    if getattr(args, "config", ""):
        os.environ["BEMINE_CONFIG"] = str(Path(args.config).expanduser().resolve())
    return BemineSettings.load(
        state_dir=Path(args.state_dir) if args.state_dir else None,
        vault_dir=Path(args.vault) if args.vault else None,
        codex_home=Path(args.codex_home) if args.codex_home else None,
        claude_home=Path(args.claude_home) if args.claude_home else None,
        kimi_home=Path(args.kimi_home) if args.kimi_home else None,
        hermes_home=Path(args.hermes_home) if args.hermes_home else None,
        device_id=args.device or None,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bemine",
        description="Bemine project mayor.",
        epilog=dedent(MAIN_HELP),
        formatter_class=HELP_FORMATTER,
    )
    parser.add_argument(
        "--state-dir",
        default="",
        help="State directory. Defaults to ./.bemine or BEMINE_HOME.",
    )
    parser.add_argument(
        "--config",
        default="",
        help="Bemine config file. Defaults to ./bemine.toml or BEMINE_CONFIG.",
    )
    parser.add_argument(
        "--vault",
        default="",
        help="Obsidian vault path. Defaults to ~/Documents/Mayor.",
    )
    parser.add_argument(
        "--codex-home", default="", help="Codex home. Defaults to ~/.codex."
    )
    parser.add_argument(
        "--claude-home", default="", help="Claude home. Defaults to ~/.claude."
    )
    parser.add_argument(
        "--kimi-home",
        default="",
        help="Kimi raw log home. Defaults to ~/devlog/raw/kimi.",
    )
    parser.add_argument(
        "--hermes-home", default="", help="Hermes home. Defaults to ~/.hermes."
    )
    parser.add_argument("--device", default="", help="Device ID for local scan.")
    parser.add_argument(
        "--credential-home",
        default="",
        help="Home directory to scan for credential metadata.",
    )

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "scan",
        help="Scan local runtime histories into the Bemine index.",
        description="Scan local Codex, Claude, and Kimi histories into SQLite and update vault project pages.",
        epilog="Example:\n  uv run bemine scan",
        formatter_class=HELP_FORMATTER,
    )

    prime = sub.add_parser(
        "prime",
        help="Print the startup packet for a Bemine worker/session.",
        description="Print the startup packet for a Bemine worker/session.",
        epilog=dedent(PRIME_HELP),
        formatter_class=HELP_FORMATTER,
    )
    prime.add_argument(
        "--limit",
        type=int,
        default=8,
        help="Maximum recent projects/sessions to include.",
    )
    prime.add_argument(
        "--cwd",
        default="",
        help="Project root to describe. Defaults to the current directory.",
    )
    prime.add_argument(
        "--fleet",
        action="store_true",
        help="Include SSH fleet status, not only local sessions.",
    )
    prime.add_argument(
        "--no-scan",
        action="store_true",
        help="Do not scan local histories before rendering.",
    )
    prime.add_argument(
        "--json", action="store_true", help="Emit machine-readable startup packet JSON."
    )

    handoff = sub.add_parser(
        "handoff",
        help="Record a shutdown handoff into the vault and audit log.",
        description="Record a shutdown handoff into the vault and audit log.",
        epilog=dedent(HANDOFF_HELP),
        formatter_class=HELP_FORMATTER,
    )
    handoff.add_argument(
        "summary", nargs="*", help="Summary text. Use --stdin for multiline input."
    )
    handoff.add_argument(
        "--stdin", action="store_true", help="Append stdin to the summary."
    )
    handoff.add_argument(
        "--cwd",
        default="",
        help="Project root for the handoff. Defaults to current directory.",
    )
    handoff.add_argument(
        "--project",
        default="",
        help="Project display name. Defaults to the cwd basename.",
    )
    handoff.add_argument(
        "--session-id", default="", help="Runtime session id, if known."
    )
    handoff.add_argument(
        "--runtime", default="", help="Runtime name such as codex, claude, or kimi."
    )
    handoff.add_argument(
        "--status",
        default="in_progress",
        help="Work state to record, e.g. done, blocked, in_progress.",
    )
    handoff.add_argument(
        "--next",
        dest="next_steps",
        action="append",
        default=[],
        help="Next step. Repeat for multiple items.",
    )
    handoff.add_argument(
        "--blocker",
        action="append",
        default=[],
        help="Blocker or approval needed. Repeat for multiple items.",
    )
    handoff.add_argument(
        "--test",
        dest="tests",
        action="append",
        default=[],
        help="Verification command/result. Repeat for multiple items.",
    )
    handoff.add_argument("--actor", default="cli", help="Actor recorded in audit log.")
    handoff.add_argument(
        "--json", action="store_true", help="Emit machine-readable handoff result JSON."
    )

    rundown = sub.add_parser(
        "rundown",
        help="Print project rundown.",
        description="Print indexed project summaries from SQLite and Obsidian vault state.",
        epilog="Examples:\n  uv run bemine rundown\n  uv run bemine rundown --limit 12",
        formatter_class=HELP_FORMATTER,
    )
    rundown.add_argument(
        "--limit", type=int, default=30, help="Maximum projects to show."
    )

    search = sub.add_parser(
        "search",
        help="Search indexed sessions with FTS5.",
        description="Search Codex/Claude/Kimi session summaries indexed in SQLite FTS5.",
        epilog='Examples:\n  uv run bemine search "AWS SSO"\n  uv run bemine search "blocked schema" --json',
        formatter_class=HELP_FORMATTER,
    )
    search.add_argument("query", help="FTS5 query text.")
    search.add_argument(
        "--limit", type=int, default=20, help="Maximum matching sessions to return."
    )
    search.add_argument(
        "--json", action="store_true", help="Emit matching session rows as JSON."
    )

    audit = sub.add_parser(
        "audit",
        help="Inspect Bemine's operational event log.",
        description="Inspect Bemine's operational event log.",
        epilog=dedent(AUDIT_HELP),
        formatter_class=HELP_FORMATTER,
    )
    audit_sub = audit.add_subparsers(dest="audit_command", required=True)
    audit_list = audit_sub.add_parser(
        "list",
        help="List recent audit events.",
        description="List recent audit events from SQLite.",
        formatter_class=HELP_FORMATTER,
    )
    audit_list.add_argument(
        "--category", default="", help="Filter by category, e.g. worker."
    )
    audit_list.add_argument(
        "--event-type",
        default="",
        help="Filter by event type, e.g. worker_transfer.",
    )
    audit_list.add_argument("--project-id", default="", help="Filter by project id.")
    audit_list.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum events to print, capped at 500.",
    )
    audit_list.add_argument(
        "--json", action="store_true", help="Emit parsed audit events as JSON."
    )

    status = sub.add_parser(
        "status",
        help="Show recent agent interactions across devices.",
        description="Show recent agent interactions across devices.",
        epilog=dedent(STATUS_HELP),
        formatter_class=HELP_FORMATTER,
    )
    status.add_argument(
        "--limit",
        type=int,
        default=12,
        help="Maximum sessions to print after sorting by last interaction.",
    )
    status.add_argument(
        "--json", action="store_true", help="Emit statuses and SSH errors as JSON."
    )
    status.add_argument(
        "--devices",
        default="",
        help="Comma-separated devices. Defaults to local,mbp,mbp2,k2,pi,windows.",
    )
    status.add_argument(
        "--local-only",
        action="store_true",
        help="Only inspect local histories; skip SSH devices.",
    )

    capabilities = sub.add_parser(
        "capabilities",
        help="Print the worker-facing capability manifest.",
        description="Print the worker-facing capability manifest.",
        epilog=dedent(CAPABILITIES_HELP),
        formatter_class=HELP_FORMATTER,
    )
    capabilities.add_argument(
        "--json",
        action="store_true",
        help="Emit credential capability manifest as JSON.",
    )

    subagents = sub.add_parser(
        "subagents",
        help="Inspect configured PA/SMM/profile Markdown.",
        description="Inspect configured PA/SMM/profile Markdown subagents.",
        epilog=dedent(SUBAGENTS_HELP),
        formatter_class=HELP_FORMATTER,
    )
    subagents_sub = subagents.add_subparsers(dest="subagents_command", required=True)
    subagents_list = subagents_sub.add_parser(
        "list", help="List configured subagent profile files."
    )
    subagents_list.add_argument(
        "--json", action="store_true", help="Emit profiles as JSON."
    )
    subagents_show = subagents_sub.add_parser(
        "show", help="Print one configured subagent profile."
    )
    subagents_show.add_argument("name", help="Profile name, e.g. PA or SMM.")
    subagents_show.add_argument(
        "--json", action="store_true", help="Emit profile as JSON."
    )

    backlog = sub.add_parser(
        "backlog",
        help="Capture/list PA or SMM backlog items.",
        description="Capture or list durable PA/SMM backlog items.",
        epilog=dedent(BACKLOG_HELP),
        formatter_class=HELP_FORMATTER,
    )
    backlog_sub = backlog.add_subparsers(dest="backlog_command", required=True)
    backlog_add = backlog_sub.add_parser("add", help="Append a backlog item.")
    backlog_add.add_argument("text", help="Idea/task text to capture.")
    backlog_add.add_argument(
        "--owner",
        default="",
        help="Backlog owner, e.g. PA or SMM. Defaults to [backlog].default_owner.",
    )
    backlog_add.add_argument("--project", default="", help="Related project id/name.")
    backlog_add.add_argument("--source", default="cli", help="Source label for audit.")
    backlog_add.add_argument("--json", action="store_true", help="Emit item as JSON.")
    backlog_list = backlog_sub.add_parser("list", help="List open backlog items.")
    backlog_list.add_argument(
        "--owner",
        default="",
        help="Backlog owner, e.g. PA or SMM. Defaults to [backlog].default_owner.",
    )
    backlog_list.add_argument("--limit", type=int, default=20, help="Maximum items.")
    backlog_list.add_argument("--json", action="store_true", help="Emit items as JSON.")

    context = sub.add_parser(
        "context",
        help="Show redacted operator context injected into workers.",
        description="Show redacted operator context injected into workers.",
        epilog="Examples:\n  uv run bemine context show\n  uv run bemine context show --json",
        formatter_class=HELP_FORMATTER,
    )
    context_sub = context.add_subparsers(dest="context_command", required=True)
    context_show = context_sub.add_parser(
        "show",
        help="Print redacted operator context files such as ~/deploy.md.",
        description="Print redacted operator context files such as ~/deploy.md.",
        formatter_class=HELP_FORMATTER,
    )
    context_show.add_argument(
        "--limit",
        type=int,
        default=12_000,
        help="Maximum characters of redacted context to print.",
    )
    context_show.add_argument(
        "--json", action="store_true", help="Emit context file list and text as JSON."
    )

    config_cmd = sub.add_parser(
        "config",
        help="Inspect Bemine's single human config file.",
        description="Inspect the active bemine.toml file.",
        epilog=(
            "Examples:\n"
            "  uv run bemine config path\n"
            "  uv run bemine config paths\n"
            "  uv run bemine config show\n"
            "  uv run bemine config show --format toml"
        ),
        formatter_class=HELP_FORMATTER,
    )
    config_sub = config_cmd.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("path", help="Print the active config file path.")
    config_sub.add_parser("paths", help="Print config path and key runtime paths.")
    config_show = config_sub.add_parser("show", help="Print parsed config.")
    config_show.add_argument(
        "--json", action="store_true", help="Emit parsed config as JSON."
    )
    config_show.add_argument(
        "--format",
        choices=["json", "toml"],
        default="json",
        help="Output format. Defaults to json.",
    )

    kimi = sub.add_parser(
        "kimi",
        help="Inspect or call the configured Kimi provider.",
        description="Inspect or call the configured Kimi provider through Bemine's handler.",
        epilog='Examples:\n  uv run bemine kimi status\n  uv run bemine kimi ask "summarize this" --max-tokens 512',
        formatter_class=HELP_FORMATTER,
    )
    kimi_sub = kimi.add_subparsers(dest="kimi_command", required=True)
    kimi_status = kimi_sub.add_parser(
        "status", help="Show provider/config status without printing the API key."
    )
    kimi_status.add_argument(
        "--json", action="store_true", help="Emit provider/config status as JSON."
    )
    kimi_ask = kimi_sub.add_parser(
        "ask", help="Send one prompt to Kimi and record usage."
    )
    kimi_ask.add_argument("prompt", help="Prompt text to send.")
    kimi_ask.add_argument("--system", default="", help="Optional system prompt.")
    kimi_ask.add_argument(
        "--max-tokens", type=int, default=512, help="Maximum output tokens."
    )
    kimi_ask.add_argument(
        "--timeout", type=float, default=60.0, help="Request timeout in seconds."
    )
    kimi_ask.add_argument(
        "--json", action="store_true", help="Emit response and usage as JSON."
    )

    usage_cmd = sub.add_parser(
        "usage",
        help="Show codex/claude/kimi token usage.",
        description="Show a rollup of recorded token usage by provider.",
        epilog="Examples:\n  uv run bemine usage\n  uv run bemine usage --provider kimi\n  uv run bemine usage --json",
        formatter_class=HELP_FORMATTER,
    )
    usage_cmd.add_argument(
        "--provider", default="", help="Filter by provider name, e.g. kimi, codex, claude."
    )
    usage_cmd.add_argument(
        "--json", action="store_true", help="Emit rollup as JSON."
    )

    dispatch = sub.add_parser(
        "dispatch",
        help="Inspect the configured Discord dispatch runtime/model.",
        description="Inspect which runtime and model Discord project work is routed to.",
        epilog="Examples:\n  uv run bemine dispatch status\n  uv run bemine dispatch status --json",
        formatter_class=HELP_FORMATTER,
    )
    dispatch_sub = dispatch.add_subparsers(dest="dispatch_command", required=True)
    dispatch_status = dispatch_sub.add_parser(
        "status",
        help="Show active dispatch runtime, provider, model, and tool-call capability.",
    )
    dispatch_status.add_argument(
        "--json", action="store_true", help="Emit dispatch status as JSON."
    )

    mail = sub.add_parser(
        "mail",
        help="Inspect mail bridge integrations.",
        description="Inspect local mail bridge integrations. Proton Mail ingestion is not wired yet.",
        epilog="Example:\n  uv run bemine mail bridge-status",
        formatter_class=HELP_FORMATTER,
    )
    mail_sub = mail.add_subparsers(dest="mail_command", required=True)
    bridge_status = mail_sub.add_parser(
        "bridge-status", help="Check Proton Bridge process and local IMAP/SMTP ports."
    )
    bridge_status.add_argument(
        "--imap-host", default="127.0.0.1", help="Bridge IMAP host."
    )
    bridge_status.add_argument(
        "--imap-port", type=int, default=1143, help="Bridge IMAP port."
    )
    bridge_status.add_argument(
        "--smtp-host", default="127.0.0.1", help="Bridge SMTP host."
    )
    bridge_status.add_argument(
        "--smtp-port", type=int, default=1025, help="Bridge SMTP port."
    )
    bridge_status.add_argument(
        "--json", action="store_true", help="Emit readiness checks as JSON."
    )

    tmux = sub.add_parser(
        "tmux",
        help="Interact with tmux-backed managed sessions.",
        description="Interact with tmux-backed managed sessions.",
        epilog=dedent(TMUX_HELP),
        formatter_class=HELP_FORMATTER,
    )
    tmux_sub = tmux.add_subparsers(dest="tmux_command", required=True)
    tmux_start = tmux_sub.add_parser(
        "start", help="Start a detached managed tmux session."
    )
    tmux_start.add_argument("name", help="tmux session name.")
    tmux_start.add_argument(
        "--command",
        dest="start_command",
        default="bash",
        help="Command to run in the session. Defaults to bash.",
    )
    tmux_send = tmux_sub.add_parser("send", help="Send text to a managed tmux session.")
    tmux_send.add_argument("name", help="tmux session name.")
    tmux_send.add_argument("text", help="Text to send.")
    tmux_send.add_argument(
        "--no-enter", action="store_true", help="Send text without pressing Enter."
    )
    tmux_capture = tmux_sub.add_parser(
        "capture", help="Capture the tmux pane contents."
    )
    tmux_capture.add_argument("name", help="tmux session name.")
    tmux_kill = tmux_sub.add_parser("kill", help="Kill a managed tmux session.")
    tmux_kill.add_argument("name", help="tmux session name.")

    workers = sub.add_parser(
        "workers",
        help="Start, adopt, and control Bemine-owned live workers.",
        description="Start, adopt, inspect, and control Bemine-owned live workers.",
        epilog=dedent(WORKERS_HELP),
        formatter_class=HELP_FORMATTER,
    )
    workers_sub = workers.add_subparsers(dest="workers_command", required=True)
    worker_start = workers_sub.add_parser(
        "start", help="Start and register a tmux-backed worker."
    )
    worker_start.add_argument("name", help="Stable worker/tmux session name.")
    worker_start.add_argument(
        "--runtime",
        default="codex",
        help="Runtime label: codex, claude, kimi, shell, or custom.",
    )
    worker_start.add_argument(
        "--cwd", default="", help="Project root. Defaults to current directory."
    )
    worker_start.add_argument(
        "--command",
        dest="worker_command",
        default="",
        help="Override command. Defaults from runtime.",
    )
    worker_start.add_argument(
        "--json", action="store_true", help="Emit worker record as JSON."
    )
    worker_adopt = workers_sub.add_parser(
        "adopt", help="Register an existing tmux session as a worker."
    )
    worker_adopt.add_argument("name", help="Existing tmux session name.")
    worker_adopt.add_argument(
        "--runtime", default="shell", help="Runtime label for the adopted session."
    )
    worker_adopt.add_argument(
        "--cwd",
        default="",
        help="Project root for this session. Defaults to current directory.",
    )
    worker_adopt.add_argument(
        "--command",
        dest="worker_command",
        default="adopted-existing-tmux-session",
        help="Command metadata to record.",
    )
    worker_adopt.add_argument(
        "--json", action="store_true", help="Emit worker record as JSON."
    )
    worker_list = workers_sub.add_parser(
        "list", help="List registered workers and whether tmux sessions are live."
    )
    worker_list.add_argument(
        "--json", action="store_true", help="Emit worker records as JSON."
    )
    worker_duplicates = workers_sub.add_parser(
        "duplicates",
        help="Show projects with more than one active worker head.",
        description="Show projects with more than one active managed worker head.",
        formatter_class=HELP_FORMATTER,
    )
    worker_duplicates.add_argument(
        "--json", action="store_true", help="Emit duplicate groups as JSON."
    )
    worker_send = workers_sub.add_parser(
        "send", help="Send text to a registered worker."
    )
    worker_send.add_argument("name", help="Worker name.")
    worker_send.add_argument("text", help="Text to send.")
    worker_send.add_argument(
        "--no-enter", action="store_true", help="Send text without pressing Enter."
    )
    worker_send.add_argument(
        "--json", action="store_true", help="Emit worker record as JSON."
    )
    worker_capture = workers_sub.add_parser(
        "capture", help="Capture a registered worker's tmux pane."
    )
    worker_capture.add_argument("name", help="Worker name.")
    worker_transfer = workers_sub.add_parser(
        "transfer",
        help="Move project context from one managed worker to another.",
        description=(
            "Capture the source worker pane, start or adopt the target tmux session, "
            "inject a structured handoff prompt, and audit duplicate project heads. "
            "When --target-device is not local, Bemine uses SSH to control tmux on "
            "that device."
        ),
        formatter_class=HELP_FORMATTER,
    )
    worker_transfer.add_argument("source", help="Registered source worker name.")
    worker_transfer.add_argument("target", help="Target worker/tmux session name.")
    worker_transfer.add_argument(
        "--runtime",
        required=True,
        help="Runtime label for the target: codex, claude, kimi, shell, or custom.",
    )
    worker_transfer.add_argument(
        "--cwd",
        default="",
        help="Target project root. Defaults to the source worker cwd.",
    )
    worker_transfer.add_argument(
        "--command",
        dest="worker_command",
        default="",
        help="Command to use if the target session must be started.",
    )
    worker_transfer.add_argument(
        "--target-device",
        default="",
        help=(
            "Receiving device. Defaults to local; non-local values use SSH-backed "
            "tmux control."
        ),
    )
    worker_transfer.add_argument(
        "--stop-source",
        action="store_true",
        help="Stop the source worker after the handoff is injected.",
    )
    worker_transfer.add_argument(
        "--json", action="store_true", help="Emit transfer result as JSON."
    )
    worker_stop = workers_sub.add_parser(
        "stop", help="Stop a registered worker's tmux session."
    )
    worker_stop.add_argument("name", help="Worker name.")
    worker_stop.add_argument(
        "--json", action="store_true", help="Emit worker record as JSON."
    )
    worker_forget = workers_sub.add_parser(
        "forget", help="Remove a worker from Bemine's registry without killing tmux."
    )
    worker_forget.add_argument("name", help="Worker name.")
    worker_forget.add_argument(
        "--json", action="store_true", help="Emit forget result as JSON."
    )

    goal = sub.add_parser(
        "goal",
        help="Start a managed worker and watcher from one goal brief.",
        description="Start a managed worker and watcher from one goal brief.",
        epilog=dedent(GOAL_HELP),
        formatter_class=HELP_FORMATTER,
    )
    goal.add_argument("project", help="Project name/id/path, e.g. rai-speech-toolkit.")
    goal.add_argument("brief", help="Goal brief to send to the worker and watcher.")
    goal.add_argument(
        "--cwd",
        default="",
        help="Project root. Required when the project is not indexed yet.",
    )
    goal.add_argument(
        "--runtime",
        default="",
        help="Worker runtime. Defaults to [goal].runtime or claude.",
    )
    goal.add_argument(
        "--worker",
        default="",
        help="Worker/tmux session name. Defaults to goal-<project>.",
    )
    goal.add_argument(
        "--interval",
        type=int,
        default=0,
        help="Watcher poll interval seconds. Defaults to [goal].interval_secs or 600.",
    )
    goal.add_argument(
        "--channel-id",
        default="",
        help="Discord channel id for watcher updates when started from Discord.",
    )
    goal.add_argument(
        "--target-device",
        default="",
        help="Device to start the worker on. Defaults to local or fleet-resolved device.",
    )
    goal.add_argument(
        "--command",
        dest="goal_command",
        default="",
        help="Override worker command. Defaults from runtime.",
    )
    goal.add_argument("--json", action="store_true", help="Emit goal start as JSON.")

    creds = sub.add_parser(
        "credentials",
        help="Inventory credential profiles without printing secrets.",
        description="Inventory credential profiles without printing secrets.",
        epilog="Examples:\n  uv run bemine credentials scan\n  uv run bemine credentials list --json",
        formatter_class=HELP_FORMATTER,
    )
    creds_sub = creds.add_subparsers(dest="credentials_command", required=True)
    creds_sub.add_parser(
        "scan",
        help="Scan standard locations and ~/.fmw metadata without printing secret values.",
    )
    creds_list = creds_sub.add_parser(
        "list", help="List discovered credential profiles and gate policy."
    )
    creds_list.add_argument(
        "--json", action="store_true", help="Emit profiles as JSON."
    )

    perms = sub.add_parser(
        "permissions",
        help="Create and resolve gated credential requests.",
        description="Create and resolve gated credential requests.",
        epilog=dedent(PERMISSIONS_HELP),
        formatter_class=HELP_FORMATTER,
    )
    perms_sub = perms.add_subparsers(dest="permissions_command", required=True)
    perm_req = perms_sub.add_parser(
        "request", help="Create a quiet local permission request."
    )
    _add_permission_request_args(perm_req, notify_default=False)
    perm_ask = perms_sub.add_parser(
        "ask", help="Create a permission request and notify Discord by default."
    )
    _add_permission_request_args(perm_ask, notify_default=True)
    perm_list = perms_sub.add_parser("list", help="List permission requests.")
    perm_list.add_argument(
        "--status", default="", help="Filter by status, e.g. pending, approved, denied."
    )
    perm_list.add_argument("--json", action="store_true", help="Emit requests as JSON.")
    perm_approve = perms_sub.add_parser(
        "approve", help="Approve a pending request, optionally with TOTP."
    )
    perm_approve.add_argument(
        "request_id", help="Permission request id, e.g. perm_xxxxx."
    )
    perm_approve.add_argument(
        "--actor", default="cli", help="Approver identity for audit log."
    )
    perm_approve.add_argument("--totp", default="", help="TOTP code when required.")
    perm_deny = perms_sub.add_parser("deny", help="Deny a pending request.")
    perm_deny.add_argument("request_id", help="Permission request id, e.g. perm_xxxxx.")
    perm_deny.add_argument(
        "--actor", default="cli", help="Denying actor for audit log."
    )

    proxy = sub.add_parser(
        "proxy",
        help="Use approved credential requests through a broker.",
        description="Use approved credential requests through a broker contract instead of raw secret reads.",
        epilog="Example:\n  uv run bemine proxy check perm_xxxxx",
        formatter_class=HELP_FORMATTER,
    )
    proxy_sub = proxy.add_subparsers(dest="proxy_command", required=True)
    proxy_check = proxy_sub.add_parser(
        "check", help="Verify that a request is approved for proxy use."
    )
    proxy_check.add_argument("request_id", help="Permission request id.")
    proxy_check.add_argument(
        "--actor", default="cli", help="Actor requesting proxy use."
    )

    jobs = sub.add_parser(
        "jobs",
        help="Manage scheduled Bemine jobs.",
        description="Manage scheduled Bemine jobs.",
        epilog=dedent(JOBS_HELP),
        formatter_class=HELP_FORMATTER,
    )
    jobs_sub = jobs.add_subparsers(dest="jobs_command", required=True)
    jobs_sub.add_parser(
        "seed", help="Seed built-in jobs such as daily_digest and morning_plan."
    )
    jobs_list = jobs_sub.add_parser(
        "list", help="List jobs, schedules, next run times, and failures."
    )
    jobs_list.add_argument("--json", action="store_true", help="Emit jobs as JSON.")
    jobs_run = jobs_sub.add_parser("run", help="Run one job immediately.")
    jobs_run.add_argument("job_id", help="Job id to run.")
    jobs_run.add_argument(
        "--no-notify",
        dest="notify",
        action="store_false",
        help="Do not send Discord notification.",
    )
    jobs_run.set_defaults(notify=True)
    jobs_reminder = jobs_sub.add_parser(
        "add-reminder", help="Create or update a recurring reminder job."
    )
    jobs_reminder.add_argument("job_id", help="Stable reminder job id.")
    jobs_reminder.add_argument(
        "schedule", help="Five-field cron schedule, e.g. '30 8 * * *'."
    )
    jobs_reminder.add_argument("--text", required=True, help="Reminder text to send.")
    jobs_reminder.add_argument(
        "--no-notify",
        dest="notify",
        action="store_false",
        help="Record job without Discord delivery.",
    )
    jobs_reminder.set_defaults(notify=True)
    jobs_enable = jobs_sub.add_parser("enable", help="Enable a job.")
    jobs_enable.add_argument("job_id", help="Job id.")
    jobs_disable = jobs_sub.add_parser("disable", help="Disable a job.")
    jobs_disable.add_argument("job_id", help="Job id.")

    deploy = sub.add_parser("deploy", help="Deploy Bemine's committed code (test-gated, auto-rollback).")
    deploy_sub = deploy.add_subparsers(dest="deploy_command", required=False)
    deploy_sub.add_parser("status", help="Show the current deploy state.")

    watch = sub.add_parser("watch", help="Manage per-project watchers.")
    watch_sub = watch.add_subparsers(dest="watch_command", required=True)
    w_start = watch_sub.add_parser("start", help="Start/replace a watcher for a project.")
    w_start.add_argument("--project", required=True, help="project id (e.g. channel:<name>)")
    w_start.add_argument("--brief", required=True, help="context brief / goal")
    w_start.add_argument("--interval", type=int, default=300, help="poll interval seconds")
    w_stop = watch_sub.add_parser("stop", help="Stop a watcher.")
    w_stop.add_argument("--project", required=True)
    w_status = watch_sub.add_parser("status", help="Show a watcher.")
    w_status.add_argument("--project", required=True)
    watch_sub.add_parser("list", help="List all watchers.")

    daemon = sub.add_parser(
        "daemon",
        help="Run the Bemine scheduler daemon.",
        description="Run the Bemine scheduler daemon that executes due SQLite-backed jobs.",
        epilog="Examples:\n  uv run bemine daemon --once\n  uv run bemine daemon --poll-seconds 30",
        formatter_class=HELP_FORMATTER,
    )
    daemon.add_argument(
        "--once", action="store_true", help="Run due jobs once, then exit."
    )
    daemon.add_argument(
        "--poll-seconds", type=int, default=30, help="Polling interval for daemon mode."
    )
    daemon.add_argument(
        "--json", action="store_true", help="Emit one-shot run results as JSON."
    )

    systemd = sub.add_parser(
        "systemd",
        help="Install persistent systemd user units.",
        description="Install or render user-level systemd units for bemine-daemon and mayor-discord-gateway.",
        epilog="Examples:\n  uv run bemine systemd install --now\n  uv run bemine systemd render",
        formatter_class=HELP_FORMATTER,
    )
    systemd_sub = systemd.add_subparsers(dest="systemd_command", required=True)
    systemd_install = systemd_sub.add_parser(
        "install", help="Install systemd user units."
    )
    systemd_install.add_argument(
        "--now", action="store_true", help="Enable and start units immediately."
    )
    systemd_install.add_argument(
        "--no-linger",
        dest="linger",
        action="store_false",
        help="Do not enable user lingering.",
    )
    systemd_install.set_defaults(linger=True)
    systemd_render = systemd_sub.add_parser(
        "render", help="Print unit files without installing."
    )
    systemd_render.add_argument(
        "--json", action="store_true", help="Emit rendered unit text as JSON."
    )

    discord = sub.add_parser(
        "discord",
        help="Manage Discord integration.",
        description="Manage Discord integration.",
        epilog=dedent(DISCORD_HELP),
        formatter_class=HELP_FORMATTER,
    )
    discord_sub = discord.add_subparsers(dest="discord_command", required=True)
    write_channels = discord_sub.add_parser(
        "write-project-channels",
        help="Write the local Discord project route file from indexed projects.",
    )
    write_channels.add_argument(
        "--limit", type=int, default=200, help="Maximum indexed projects to write."
    )
    sync_channels = discord_sub.add_parser(
        "sync-project-channels",
        help="Create/update Discord project channels from indexed projects.",
    )
    sync_channels.add_argument(
        "--guild-id",
        default=os.environ.get("MAYOR_DISCORD_GUILD_ID", ""),
        help="Discord guild/server id.",
    )
    sync_channels.add_argument(
        "--limit", type=int, default=200, help="Maximum indexed projects to sync."
    )
    sync_channels.add_argument(
        "--prune",
        action="store_true",
        help="Delete project-category channels that are no longer tracked.",
    )
    return parser


def _add_permission_request_args(
    parser: argparse.ArgumentParser, *, notify_default: bool
) -> None:
    parser.add_argument(
        "credential_id", help="Credential id from `bemine capabilities`, e.g. aws:any."
    )
    parser.add_argument(
        "--reason", required=True, help="Human-readable reason for gated access."
    )
    parser.add_argument(
        "--project-id", default="", help="Project id requesting access."
    )
    parser.add_argument(
        "--session-id", default="", help="Runtime session id requesting access."
    )
    parser.add_argument(
        "--runtime", default="", help="Runtime name requesting access, e.g. codex."
    )
    notify = parser.add_mutually_exclusive_group()
    if notify_default:
        parser.set_defaults(notify_discord=True)
        notify.add_argument("--no-notify", dest="notify_discord", action="store_false")
    else:
        parser.set_defaults(notify_discord=False)
        notify.add_argument(
            "--notify-discord", dest="notify_discord", action="store_true"
        )


def audit_events_to_dicts(rows: list[object]) -> list[dict]:
    events = []
    for row in rows:
        event = dict(row)
        payload_json = str(event.pop("payload_json", "") or "")
        try:
            event["payload"] = json.loads(payload_json) if payload_json else {}
        except json.JSONDecodeError:
            event["payload"] = {"raw": payload_json}
        events.append(event)
    return events


def render_audit_events(rows: list[object]) -> str:
    events = audit_events_to_dicts(rows)
    if not events:
        return "No audit events found.\n"
    lines = ["Audit events:"]
    for event in events:
        timestamp = datetime.fromtimestamp(float(event["ts"])).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        device = event.get("device_id") or "-"
        project = event.get("project_id") or "-"
        lines.append(
            f"- {timestamp} #{event['id']} {event['category']}/{event['event_type']} "
            f"actor={event['actor']} device={device} project={project}"
        )
        summary = _audit_payload_summary(event.get("payload"))
        if summary:
            lines.append(f"  {summary}")
    return "\n".join(lines) + "\n"


def _audit_payload_summary(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    source = payload.get("source")
    target = payload.get("target")
    if isinstance(source, dict) and isinstance(target, dict):
        duplicate_count = len(payload.get("duplicate_heads") or [])
        return (
            f"{source.get('name')}[{source.get('runtime')}] -> "
            f"{target.get('name')}[{target.get('runtime')}/{target.get('transport')}] "
            f"duplicates={duplicate_count}"
        )
    for key in ("credential_id", "request_id", "provider", "job_id", "text_len"):
        if key in payload:
            return f"{key}={payload[key]}"
    return ""


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "tmux":
        tmux = TmuxAdapter()
        if args.tmux_command == "start":
            tmux.start(args.name, args.start_command)
            print(json.dumps({"ok": True, "session": args.name}))
        elif args.tmux_command == "send":
            tmux.send(args.name, args.text, enter=not args.no_enter)
            print(json.dumps({"ok": True, "session": args.name}))
        elif args.tmux_command == "capture":
            print(tmux.capture(args.name), end="")
        elif args.tmux_command == "kill":
            tmux.kill(args.name)
            print(json.dumps({"ok": True, "session": args.name}))
        return

    settings = _settings_from_args(args)
    if args.command == "workers":
        controller = WorkerController(settings)
        try:
            if args.workers_command == "start":
                record = controller.start(
                    name=args.name,
                    runtime=args.runtime,
                    cwd=Path(args.cwd) if args.cwd else Path.cwd(),
                    command=args.worker_command,
                )
                if args.json:
                    print(json.dumps(record.to_dict(), indent=2, sort_keys=True))
                else:
                    print(render_workers([record]), end="")
            elif args.workers_command == "adopt":
                record = controller.adopt(
                    name=args.name,
                    runtime=args.runtime,
                    cwd=Path(args.cwd) if args.cwd else Path.cwd(),
                    command=args.worker_command,
                )
                if args.json:
                    print(json.dumps(record.to_dict(), indent=2, sort_keys=True))
                else:
                    print(render_workers([record]), end="")
            elif args.workers_command == "list":
                records = controller.list()
                if args.json:
                    print(
                        json.dumps(
                            [record.to_dict() for record in records],
                            indent=2,
                            sort_keys=True,
                        )
                    )
                else:
                    print(render_workers(records), end="")
            elif args.workers_command == "duplicates":
                groups = controller.duplicate_heads()
                if args.json:
                    print(
                        json.dumps(
                            [group.to_dict() for group in groups],
                            indent=2,
                            sort_keys=True,
                        )
                    )
                else:
                    print(render_duplicate_heads(groups), end="")
            elif args.workers_command == "send":
                record = controller.send(args.name, args.text, enter=not args.no_enter)
                if args.json:
                    print(json.dumps(record.to_dict(), indent=2, sort_keys=True))
                else:
                    print(render_workers([record]), end="")
            elif args.workers_command == "capture":
                print(controller.capture(args.name), end="")
            elif args.workers_command == "transfer":
                result = controller.transfer(
                    source_name=args.source,
                    target_name=args.target,
                    target_runtime=args.runtime,
                    cwd=Path(args.cwd) if args.cwd else None,
                    command=args.worker_command,
                    target_device=args.target_device,
                    stop_source=args.stop_source,
                )
                if args.json:
                    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
                else:
                    print(render_transfer(result), end="")
            elif args.workers_command == "stop":
                record = controller.stop(args.name)
                if args.json:
                    print(json.dumps(record.to_dict(), indent=2, sort_keys=True))
                else:
                    print(render_workers([record]), end="")
            elif args.workers_command == "forget":
                forgotten = controller.forget(args.name)
                payload = {"name": args.name, "forgotten": forgotten}
                if args.json:
                    print(json.dumps(payload, indent=2, sort_keys=True))
                else:
                    print(
                        f"Forgotten: {args.name}"
                        if forgotten
                        else f"Worker not found: {args.name}"
                    )
        except WorkerError as exc:
            raise SystemExit(str(exc)) from exc
        return

    kernel = BemineKernel(settings)
    if args.command == "scan":
        result = kernel.scan_local()
        print(json.dumps(result.__dict__, sort_keys=True))
    elif args.command == "prime":
        if not args.no_scan:
            kernel.scan_local()
        packet = build_prime_packet(
            settings,
            cwd=Path(args.cwd) if args.cwd else None,
            limit=args.limit,
            fleet=args.fleet,
        )
        if args.json:
            print(json.dumps(packet.to_dict(), indent=2, sort_keys=True))
        else:
            print(render_prime_packet(packet), end="")
    elif args.command == "handoff":
        result = record_handoff(
            settings,
            summary=summary_from_args(args.summary, read_stdin=args.stdin),
            cwd=Path(args.cwd) if args.cwd else None,
            project_name=args.project,
            session_id=args.session_id,
            runtime=args.runtime,
            status=args.status,
            next_steps=args.next_steps,
            blockers=args.blocker,
            tests=args.tests,
            actor=args.actor,
        )
        if args.json:
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        else:
            print(f"Handoff recorded: {result.path}")
    elif args.command == "rundown":
        print(kernel.project_rundown(limit=args.limit), end="")
    elif args.command == "search":
        rows = kernel.search(args.query, limit=args.limit)
        if args.json:
            print(json.dumps(rows, indent=2, sort_keys=True))
        else:
            for row in rows:
                print(
                    f"{row['runtime']} {row['cwd']} {row['updated_at']} {row['title']}"
                )
    elif args.command == "audit":
        if args.audit_command == "list":
            rows = kernel.db.list_audit_events(
                category=args.category,
                event_type=args.event_type,
                project_id=args.project_id,
                limit=args.limit,
            )
            if args.json:
                print(json.dumps(audit_events_to_dicts(rows), indent=2, sort_keys=True))
            else:
                print(render_audit_events(rows), end="")
    elif args.command == "status":
        if args.local_only:
            devices = ["local"]
        elif args.devices:
            devices = [part.strip() for part in args.devices.split(",") if part.strip()]
        else:
            devices = None
        statuses, errors = collect_fleet_status(settings, devices=devices)
        if args.json:
            print(
                json.dumps(
                    {
                        "statuses": [
                            status.to_dict() for status in statuses[: args.limit]
                        ],
                        "errors": [error.__dict__ for error in errors],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(render_status(statuses, errors, limit=args.limit), end="")
    elif args.command == "capabilities":
        inventory = CredentialInventory(
            kernel.db,
            home=Path(args.credential_home) if args.credential_home else None,
            device_id=settings.device_id,
        )
        inventory.scan()
        rows = [dict(row) for row in kernel.db.list_credential_profiles()]
        manifest = build_capability_manifest(rows, device_id=settings.device_id)
        if args.json:
            print(json.dumps(manifest, indent=2, sort_keys=True))
        else:
            print(render_capability_manifest(manifest), end="")
    elif args.command == "subagents":
        context = load_subagent_context()
        if args.subagents_command == "list":
            if args.json:
                print(json.dumps(context.to_dict(), indent=2, sort_keys=True))
            else:
                print(render_subagent_context(context), end="")
        elif args.subagents_command == "show":
            name = args.name.casefold()
            profile = next(
                (
                    item
                    for item in context.profiles
                    if item.name.casefold() == name
                    or item.path.name.casefold() == name
                ),
                None,
            )
            if profile is None:
                raise SystemExit(f"Unknown subagent profile: {args.name}")
            if args.json:
                print(json.dumps(profile.to_dict(), indent=2, sort_keys=True))
            else:
                print(profile.text.rstrip() + "\n", end="")
    elif args.command == "backlog":
        owner = args.owner or default_backlog_owner()
        try:
            if args.backlog_command == "add":
                entry = add_backlog_item(
                    settings,
                    owner=owner,
                    text=args.text,
                    source=args.source,
                    project=args.project,
                    db=kernel.db,
                )
                if args.json:
                    print(json.dumps(entry.to_dict(), indent=2, sort_keys=True))
                else:
                    print(f"Backlog item added: {entry.path}")
            elif args.backlog_command == "list":
                items = list_backlog_items(settings, owner=owner, limit=args.limit)
                if args.json:
                    print(
                        json.dumps(
                            {"owner": owner.upper(), "items": items},
                            indent=2,
                            sort_keys=True,
                        )
                    )
                else:
                    print(render_backlog_items(owner, items), end="")
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    elif args.command == "usage":
        rollup = kernel.db.usage_rollup(provider=getattr(args, "provider", None) or None)
        if getattr(args, "json", False):
            print(json.dumps(rollup, indent=2))
        else:
            from bemine.usage import render_usage
            print(render_usage(rollup))
    elif args.command == "context":
        if args.context_command == "show":
            context = load_operator_context(limit=args.limit)
            if args.json:
                print(json.dumps(context.to_dict(), indent=2, sort_keys=True))
            else:
                print(render_operator_context(context), end="")
    elif args.command == "config":
        if args.config_command == "path":
            print(bemine_config_path())
        elif args.config_command == "paths":
            payload = {
                "config": str(bemine_config_path()),
                "state_dir": str(settings.state_dir),
                "db_path": str(settings.db_path),
                "vault_dir": str(settings.vault_dir),
                "codex_home": str(settings.codex_home),
                "claude_home": str(settings.claude_home),
                "kimi_home": str(settings.kimi_home),
                "hermes_home": str(settings.hermes_home),
            }
            print(json.dumps(payload, indent=2, sort_keys=True))
        elif args.config_command == "show":
            payload = {
                "path": str(bemine_config_path()),
                "config": load_bemine_config(),
            }
            if args.format == "toml":
                print(render_toml_config(payload["config"]), end="")
            else:
                print(json.dumps(payload, indent=2, sort_keys=True))
    elif args.command == "kimi":
        handler = KimiHandler(settings.hermes_home)
        if args.kimi_command == "status":
            config = handler.status()
            if args.json:
                print(json.dumps(config.to_dict(), indent=2, sort_keys=True))
            else:
                print(render_kimi_status(config), end="")
        elif args.kimi_command == "ask":
            result = handler.ask(
                args.prompt,
                system=args.system,
                max_tokens=args.max_tokens,
                timeout=args.timeout,
            )
            usage = usage_payload(result)
            kernel.db.record_usage_sample(
                provider="kimi",
                account=result.provider,
                runtime="kimi",
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                total_tokens=result.total_tokens,
                raw_json=json.dumps(usage, sort_keys=True),
            )
            kernel.db.record_audit_event(
                category="runtime",
                event_type="kimi_ask",
                actor="cli",
                device_id=settings.device_id,
                payload_json=json.dumps(usage, sort_keys=True),
            )
            if args.json:
                print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
            else:
                print(result.text)
    elif args.command == "dispatch":
        if args.dispatch_command == "status":
            from bemine.discord.dispatch import (
                dispatch_status_payload,
                render_dispatch_status,
            )

            payload = dispatch_status_payload(settings)
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(render_dispatch_status(payload), end="")
    elif args.command == "mail":
        if args.mail_command == "bridge-status":
            status = check_proton_bridge(
                imap_host=args.imap_host,
                imap_port=args.imap_port,
                smtp_host=args.smtp_host,
                smtp_port=args.smtp_port,
            )
            if args.json:
                print(json.dumps(status.to_dict(), indent=2, sort_keys=True))
            else:
                print(render_proton_bridge_status(status), end="")
    elif args.command == "credentials":
        inventory = CredentialInventory(
            kernel.db,
            home=Path(args.credential_home) if args.credential_home else None,
            device_id=settings.device_id,
        )
        if args.credentials_command == "scan":
            profiles = inventory.scan()
            print(json.dumps({"credential_profiles": len(profiles)}, sort_keys=True))
        elif args.credentials_command == "list":
            rows = [dict(row) for row in kernel.db.list_credential_profiles()]
            if args.json:
                print(json.dumps(rows, indent=2, sort_keys=True))
            else:
                for row in rows:
                    totp = " totp" if row["totp_required"] else ""
                    print(
                        f"{row['credential_id']} [{row['kind']}] "
                        f"{row['approval_policy']}{totp} - {row['name']}"
                    )
    elif args.command == "permissions":
        gate = PermissionGate(kernel.db)
        if args.permissions_command in {"request", "ask"}:
            CredentialInventory(
                kernel.db,
                home=Path(args.credential_home) if args.credential_home else None,
                device_id=settings.device_id,
            ).scan()
            request_id = gate.request(
                credential_id=args.credential_id,
                reason=args.reason,
                project_id=args.project_id or None,
                session_id=args.session_id or None,
                device_id=settings.device_id,
                runtime=args.runtime or None,
            )
            result = {
                "request_id": request_id,
                "status": "pending",
                "notified": False,
                "discord_message_id": "",
            }
            if args.notify_discord:
                notification = notify_permission_request_via_discord(
                    kernel.db, request_id
                )
                result["notified"] = True
                result["discord_message_id"] = notification.message_id
            print(json.dumps(result, sort_keys=True))
        elif args.permissions_command == "list":
            rows = [
                dict(row)
                for row in kernel.db.list_permission_requests(
                    status=args.status or None
                )
            ]
            if args.json:
                print(json.dumps(rows, indent=2, sort_keys=True))
            else:
                for row in rows:
                    print(
                        f"{row['request_id']} {row['status']} "
                        f"{row['credential_id']} - {row['reason']}"
                    )
        elif args.permissions_command == "approve":
            gate.approve(args.request_id, actor=args.actor, totp_code=args.totp)
            print(
                json.dumps(
                    {"request_id": args.request_id, "status": "approved"},
                    sort_keys=True,
                )
            )
        elif args.permissions_command == "deny":
            gate.deny(args.request_id, actor=args.actor)
            print(
                json.dumps(
                    {"request_id": args.request_id, "status": "denied"}, sort_keys=True
                )
            )
    elif args.command == "proxy":
        if args.proxy_command == "check":
            result = CredentialProxy(kernel.db).check(args.request_id, actor=args.actor)
            print(json.dumps(result, sort_keys=True))
    elif args.command == "goal":
        try:
            result = start_goal(
                settings,
                project=args.project,
                brief=args.brief,
                cwd=Path(args.cwd) if args.cwd else None,
                runtime=args.runtime,
                worker_name=args.worker,
                interval_secs=args.interval,
                channel_id=args.channel_id,
                command=args.goal_command,
                target_device=args.target_device,
                db=kernel.db,
            )
        except (ValueError, WorkerError) as exc:
            raise SystemExit(str(exc)) from exc
        if args.json:
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        else:
            print(render_goal_start(result), end="")
    elif args.command == "jobs":
        if args.jobs_command == "seed":
            seed_default_jobs(kernel.db)
            print(
                json.dumps(
                    {"ok": True, "jobs": len(kernel.db.list_jobs())}, sort_keys=True
                )
            )
        elif args.jobs_command == "list":
            seed_default_jobs(kernel.db)
            rows = [dict(row) for row in kernel.db.list_jobs()]
            if args.json:
                print(json.dumps(rows, indent=2, sort_keys=True))
            else:
                print(render_jobs(rows), end="")
        elif args.jobs_command == "run":
            result = run_job_by_id(
                settings,
                args.job_id,
                sender=None
                if args.notify
                else (lambda text: {"id": "", "channel_id": ""}),
            )
            print(
                json.dumps(
                    {
                        "job_id": result.job_id,
                        "status": result.status,
                        "notified": result.notified,
                    },
                    sort_keys=True,
                )
            )
        elif args.jobs_command == "add-reminder":
            upsert_reminder_job(
                kernel.db,
                job_id=args.job_id,
                schedule=args.schedule,
                text=args.text,
                notify_discord=args.notify,
            )
            job = dict(kernel.db.get_job(args.job_id) or {})
            print(json.dumps(job, indent=2, sort_keys=True))
        elif args.jobs_command == "enable":
            kernel.db.set_job_enabled(args.job_id, enabled=True)
            print(json.dumps({"job_id": args.job_id, "enabled": True}, sort_keys=True))
        elif args.jobs_command == "disable":
            kernel.db.set_job_enabled(args.job_id, enabled=False)
            print(json.dumps({"job_id": args.job_id, "enabled": False}, sort_keys=True))
    elif args.command == "deploy":
        from bemine.deploy.state import DeployState, state_path
        settings = _settings_from_args(args)
        if getattr(args, "deploy_command", None) == "status":
            st = DeployState.load(state_path(settings))
            print(f"phase={st.phase} target={st.target_sha[:8]} rollback={st.rollback_sha[:8]}")
        else:
            st = run_deploy(settings, self_restart_daemon=False)
            print(f"deploy {st.phase} (target={st.target_sha[:8]})")
    elif args.command == "watch":
        from bemine.db import BemineDB
        settings = _settings_from_args(args)
        db = BemineDB(settings.db_path)
        if args.watch_command == "start":
            channel_id = db.get_channel_id_for_project(args.project) or ""
            db.upsert_project_watcher(
                project_id=args.project, channel_id=channel_id,
                brief=args.brief, interval_secs=args.interval,
            )
            print(f"watcher active for {args.project} (every {args.interval}s, "
                  f"channel={channel_id or 'unknown'})")
        elif args.watch_command == "stop":
            db.update_project_watcher(args.project, status="stopped")
            print(f"watcher stopped for {args.project}")
        elif args.watch_command == "status":
            row = db.get_project_watcher(args.project)
            print(json.dumps(row, indent=2) if row else "no watcher")
        elif args.watch_command == "list":
            for row in db.list_active_project_watchers():
                print(f"{row['project_id']}\t{row['status']}\t"
                      f"every {row['interval_secs']}s\tchannel={row['channel_id']}")
    elif args.command == "daemon":
        daemon = BemineDaemon(settings, poll_seconds=args.poll_seconds)
        if args.once:
            results = daemon.run_once()
            payload = [result.__dict__ for result in results]
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            elif not results:
                print("No due jobs.")
            else:
                for result in results:
                    print(
                        f"{result.job_id}: {result.status} notified={result.notified}"
                    )
        else:
            daemon.run_forever()
    elif args.command == "systemd":
        if args.systemd_command == "install":
            written = install_user_units(
                repo_dir=Path.cwd(),
                state_dir=settings.state_dir,
                now=args.now,
                enable_linger=args.linger,
            )
            print(
                json.dumps(
                    {"written": [str(path) for path in written]},
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.systemd_command == "render":
            units = render_user_units(repo_dir=Path.cwd(), state_dir=settings.state_dir)
            if args.json:
                print(
                    json.dumps(
                        {unit.name: unit.text for unit in units},
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                for unit in units:
                    print(f"# {unit.name}\n{unit.text}")
    elif args.command == "discord":
        if args.discord_command == "write-project-channels":
            from bemine.discord.dispatch import (
                project_channels_from_db,
                write_project_channel_config,
            )

            channels = project_channels_from_db(kernel.db, limit=args.limit)
            path = write_project_channel_config(
                settings.state_dir,
                channels,
                preserve_existing=False,
            )
            print(
                json.dumps(
                    {"path": str(path), "projects": len(channels)}, sort_keys=True
                )
            )
        elif args.discord_command == "sync-project-channels":
            from bemine.discord.config import Settings as DiscordSettings
            from bemine.discord.dispatch import sync_project_channels_to_discord

            discord_settings = DiscordSettings.load()
            guild_id = (
                args.guild_id.strip()
                or os.environ.get("DISCORD_GUILD_ID", "").strip()
                or config_string("discord", "guild_id", "")
            )
            if not guild_id:
                raise SystemExit("Set --guild-id or MAYOR_DISCORD_GUILD_ID.")
            result = asyncio.run(
                sync_project_channels_to_discord(
                    settings,
                    discord_settings.token,
                    guild_id,
                    limit=args.limit,
                    prune=args.prune,
                )
            )
            print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
