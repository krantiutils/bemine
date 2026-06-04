from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from .capabilities import build_capability_manifest, render_capability_manifest
from .config import BemineSettings
from .credentials import CredentialInventory, PermissionGate
from .jobs import BemineDaemon, render_jobs, run_job_by_id, seed_default_jobs, upsert_reminder_job
from .mail_bridge import check_proton_bridge, render_proton_bridge_status
from .kimi_handler import KimiHandler, render_kimi_status, usage_payload
from .kernel import BemineKernel
from .notifications import notify_permission_request_via_discord
from .operator_context import load_operator_context, render_operator_context
from .proxy import CredentialProxy
from .status import collect_fleet_status, render_status
from .systemd import install_user_units, render_user_units
from .tmux_adapter import TmuxAdapter


def _settings_from_args(args: argparse.Namespace) -> BemineSettings:
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
    parser = argparse.ArgumentParser(prog="bemine", description="Bemine project mayor.")
    parser.add_argument("--state-dir", default="", help="State directory. Defaults to ./.bemine or BEMINE_HOME.")
    parser.add_argument("--vault", default="", help="Obsidian vault path. Defaults to ~/Documents/Mayor.")
    parser.add_argument("--codex-home", default="", help="Codex home. Defaults to ~/.codex.")
    parser.add_argument("--claude-home", default="", help="Claude home. Defaults to ~/.claude.")
    parser.add_argument("--kimi-home", default="", help="Kimi raw log home. Defaults to ~/devlog/raw/kimi.")
    parser.add_argument("--hermes-home", default="", help="Hermes home. Defaults to ~/.hermes.")
    parser.add_argument("--device", default="", help="Device ID for local scan.")
    parser.add_argument("--credential-home", default="", help="Home directory to scan for credential metadata.")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scan", help="Scan local runtime histories into the Bemine index.")

    rundown = sub.add_parser("rundown", help="Print project rundown.")
    rundown.add_argument("--limit", type=int, default=30)

    search = sub.add_parser("search", help="Search indexed sessions with FTS5.")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--json", action="store_true")

    status = sub.add_parser("status", help="Show recent agent interactions across devices.")
    status.add_argument("--limit", type=int, default=12)
    status.add_argument("--json", action="store_true")
    status.add_argument(
        "--devices",
        default="",
        help="Comma-separated devices. Defaults to local,mbp,mbp2,k2,pi,windows.",
    )
    status.add_argument("--local-only", action="store_true")

    capabilities = sub.add_parser(
        "capabilities",
        help="Print the worker-facing capability manifest.",
    )
    capabilities.add_argument("--json", action="store_true")

    context = sub.add_parser(
        "context",
        help="Show redacted operator context injected into workers.",
    )
    context_sub = context.add_subparsers(dest="context_command", required=True)
    context_show = context_sub.add_parser("show")
    context_show.add_argument("--limit", type=int, default=12_000)
    context_show.add_argument("--json", action="store_true")

    kimi = sub.add_parser("kimi", help="Inspect or call the configured Kimi provider.")
    kimi_sub = kimi.add_subparsers(dest="kimi_command", required=True)
    kimi_status = kimi_sub.add_parser("status")
    kimi_status.add_argument("--json", action="store_true")
    kimi_ask = kimi_sub.add_parser("ask")
    kimi_ask.add_argument("prompt")
    kimi_ask.add_argument("--system", default="")
    kimi_ask.add_argument("--max-tokens", type=int, default=512)
    kimi_ask.add_argument("--timeout", type=float, default=60.0)
    kimi_ask.add_argument("--json", action="store_true")

    mail = sub.add_parser("mail", help="Inspect mail bridge integrations.")
    mail_sub = mail.add_subparsers(dest="mail_command", required=True)
    bridge_status = mail_sub.add_parser("bridge-status")
    bridge_status.add_argument("--imap-host", default="127.0.0.1")
    bridge_status.add_argument("--imap-port", type=int, default=1143)
    bridge_status.add_argument("--smtp-host", default="127.0.0.1")
    bridge_status.add_argument("--smtp-port", type=int, default=1025)
    bridge_status.add_argument("--json", action="store_true")

    tmux = sub.add_parser("tmux", help="Interact with tmux-backed managed sessions.")
    tmux_sub = tmux.add_subparsers(dest="tmux_command", required=True)
    tmux_start = tmux_sub.add_parser("start")
    tmux_start.add_argument("name")
    tmux_start.add_argument("--command", default="bash")
    tmux_send = tmux_sub.add_parser("send")
    tmux_send.add_argument("name")
    tmux_send.add_argument("text")
    tmux_send.add_argument("--no-enter", action="store_true")
    tmux_capture = tmux_sub.add_parser("capture")
    tmux_capture.add_argument("name")
    tmux_kill = tmux_sub.add_parser("kill")
    tmux_kill.add_argument("name")

    creds = sub.add_parser("credentials", help="Inventory credential profiles without printing secrets.")
    creds_sub = creds.add_subparsers(dest="credentials_command", required=True)
    creds_sub.add_parser("scan")
    creds_list = creds_sub.add_parser("list")
    creds_list.add_argument("--json", action="store_true")

    perms = sub.add_parser("permissions", help="Create and resolve gated credential requests.")
    perms_sub = perms.add_subparsers(dest="permissions_command", required=True)
    perm_req = perms_sub.add_parser("request")
    _add_permission_request_args(perm_req, notify_default=False)
    perm_ask = perms_sub.add_parser("ask")
    _add_permission_request_args(perm_ask, notify_default=True)
    perm_list = perms_sub.add_parser("list")
    perm_list.add_argument("--status", default="")
    perm_list.add_argument("--json", action="store_true")
    perm_approve = perms_sub.add_parser("approve")
    perm_approve.add_argument("request_id")
    perm_approve.add_argument("--actor", default="cli")
    perm_approve.add_argument("--totp", default="")
    perm_deny = perms_sub.add_parser("deny")
    perm_deny.add_argument("request_id")
    perm_deny.add_argument("--actor", default="cli")

    proxy = sub.add_parser("proxy", help="Use approved credential requests through a broker.")
    proxy_sub = proxy.add_subparsers(dest="proxy_command", required=True)
    proxy_check = proxy_sub.add_parser("check")
    proxy_check.add_argument("request_id")
    proxy_check.add_argument("--actor", default="cli")

    jobs = sub.add_parser("jobs", help="Manage scheduled Bemine jobs.")
    jobs_sub = jobs.add_subparsers(dest="jobs_command", required=True)
    jobs_sub.add_parser("seed")
    jobs_list = jobs_sub.add_parser("list")
    jobs_list.add_argument("--json", action="store_true")
    jobs_run = jobs_sub.add_parser("run")
    jobs_run.add_argument("job_id")
    jobs_run.add_argument("--no-notify", dest="notify", action="store_false")
    jobs_run.set_defaults(notify=True)
    jobs_reminder = jobs_sub.add_parser("add-reminder")
    jobs_reminder.add_argument("job_id")
    jobs_reminder.add_argument("schedule", help="Five-field cron schedule, e.g. '30 8 * * *'.")
    jobs_reminder.add_argument("--text", required=True)
    jobs_reminder.add_argument("--no-notify", dest="notify", action="store_false")
    jobs_reminder.set_defaults(notify=True)
    jobs_enable = jobs_sub.add_parser("enable")
    jobs_enable.add_argument("job_id")
    jobs_disable = jobs_sub.add_parser("disable")
    jobs_disable.add_argument("job_id")

    daemon = sub.add_parser("daemon", help="Run the Bemine scheduler daemon.")
    daemon.add_argument("--once", action="store_true")
    daemon.add_argument("--poll-seconds", type=int, default=30)
    daemon.add_argument("--json", action="store_true")

    systemd = sub.add_parser("systemd", help="Install persistent systemd user units.")
    systemd_sub = systemd.add_subparsers(dest="systemd_command", required=True)
    systemd_install = systemd_sub.add_parser("install")
    systemd_install.add_argument("--now", action="store_true")
    systemd_install.add_argument("--no-linger", dest="linger", action="store_false")
    systemd_install.set_defaults(linger=True)
    systemd_render = systemd_sub.add_parser("render")
    systemd_render.add_argument("--json", action="store_true")

    discord = sub.add_parser("discord", help="Manage Discord integration.")
    discord_sub = discord.add_subparsers(dest="discord_command", required=True)
    write_channels = discord_sub.add_parser(
        "write-project-channels",
        help="Write the local Discord project route file from indexed projects.",
    )
    write_channels.add_argument("--limit", type=int, default=200)
    sync_channels = discord_sub.add_parser("sync-project-channels")
    sync_channels.add_argument("--guild-id", default=os.environ.get("MAYOR_DISCORD_GUILD_ID", ""))
    sync_channels.add_argument("--limit", type=int, default=200)
    sync_channels.add_argument(
        "--prune",
        action="store_true",
        help="Delete project-category channels that are no longer tracked.",
    )
    return parser


def _add_permission_request_args(parser: argparse.ArgumentParser, *, notify_default: bool) -> None:
    parser.add_argument("credential_id")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--project-id", default="")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--runtime", default="")
    notify = parser.add_mutually_exclusive_group()
    if notify_default:
        parser.set_defaults(notify_discord=True)
        notify.add_argument("--no-notify", dest="notify_discord", action="store_false")
    else:
        parser.set_defaults(notify_discord=False)
        notify.add_argument("--notify-discord", dest="notify_discord", action="store_true")


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "tmux":
        tmux = TmuxAdapter()
        if args.tmux_command == "start":
            tmux.start(args.name, args.command)
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
    kernel = BemineKernel(settings)
    if args.command == "scan":
        result = kernel.scan_local()
        print(json.dumps(result.__dict__, sort_keys=True))
    elif args.command == "rundown":
        print(kernel.project_rundown(limit=args.limit), end="")
    elif args.command == "search":
        rows = kernel.search(args.query, limit=args.limit)
        if args.json:
            print(json.dumps(rows, indent=2, sort_keys=True))
        else:
            for row in rows:
                print(f"{row['runtime']} {row['cwd']} {row['updated_at']} {row['title']}")
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
                        "statuses": [status.to_dict() for status in statuses[: args.limit]],
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
    elif args.command == "context":
        if args.context_command == "show":
            context = load_operator_context(limit=args.limit)
            if args.json:
                print(json.dumps(context.to_dict(), indent=2, sort_keys=True))
            else:
                print(render_operator_context(context), end="")
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
                notification = notify_permission_request_via_discord(kernel.db, request_id)
                result["notified"] = True
                result["discord_message_id"] = notification.message_id
            print(json.dumps(result, sort_keys=True))
        elif args.permissions_command == "list":
            rows = [
                dict(row)
                for row in kernel.db.list_permission_requests(status=args.status or None)
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
            print(json.dumps({"request_id": args.request_id, "status": "approved"}, sort_keys=True))
        elif args.permissions_command == "deny":
            gate.deny(args.request_id, actor=args.actor)
            print(json.dumps({"request_id": args.request_id, "status": "denied"}, sort_keys=True))
    elif args.command == "proxy":
        if args.proxy_command == "check":
            result = CredentialProxy(kernel.db).check(args.request_id, actor=args.actor)
            print(json.dumps(result, sort_keys=True))
    elif args.command == "jobs":
        if args.jobs_command == "seed":
            seed_default_jobs(kernel.db)
            print(json.dumps({"ok": True, "jobs": len(kernel.db.list_jobs())}, sort_keys=True))
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
                sender=None if args.notify else (lambda text: {"id": "", "channel_id": ""}),
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
                    print(f"{result.job_id}: {result.status} notified={result.notified}")
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
            print(json.dumps({"written": [str(path) for path in written]}, indent=2, sort_keys=True))
        elif args.systemd_command == "render":
            units = render_user_units(repo_dir=Path.cwd(), state_dir=settings.state_dir)
            if args.json:
                print(json.dumps({unit.name: unit.text for unit in units}, indent=2, sort_keys=True))
            else:
                for unit in units:
                    print(f"# {unit.name}\n{unit.text}")
    elif args.command == "discord":
        if args.discord_command == "write-project-channels":
            from mayor_discord.dispatch import (
                project_channels_from_db,
                write_project_channel_config,
            )

            channels = project_channels_from_db(kernel.db, limit=args.limit)
            path = write_project_channel_config(
                settings.state_dir,
                channels,
                preserve_existing=False,
            )
            print(json.dumps({"path": str(path), "projects": len(channels)}, sort_keys=True))
        elif args.discord_command == "sync-project-channels":
            from mayor_discord.config import Settings as DiscordSettings
            from mayor_discord.dispatch import sync_project_channels_to_discord

            discord_settings = DiscordSettings.load()
            guild_id = args.guild_id.strip() or os.environ.get("DISCORD_GUILD_ID", "").strip()
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
