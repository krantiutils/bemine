from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import BemineSettings
from .credentials import CredentialInventory, PermissionGate
from .kernel import BemineKernel
from .tmux_adapter import TmuxAdapter


def _settings_from_args(args: argparse.Namespace) -> BemineSettings:
    return BemineSettings.load(
        state_dir=Path(args.state_dir) if args.state_dir else None,
        vault_dir=Path(args.vault) if args.vault else None,
        codex_home=Path(args.codex_home) if args.codex_home else None,
        claude_home=Path(args.claude_home) if args.claude_home else None,
        kimi_home=Path(args.kimi_home) if args.kimi_home else None,
        device_id=args.device or None,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bemine", description="Bemine project mayor.")
    parser.add_argument("--state-dir", default="", help="State directory. Defaults to ./.bemine or BEMINE_HOME.")
    parser.add_argument("--vault", default="", help="Obsidian vault path. Defaults to ~/Documents/Mayor.")
    parser.add_argument("--codex-home", default="", help="Codex home. Defaults to ~/.codex.")
    parser.add_argument("--claude-home", default="", help="Claude home. Defaults to ~/.claude.")
    parser.add_argument("--kimi-home", default="", help="Kimi raw log home. Defaults to ~/devlog/raw/kimi.")
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
    perm_req.add_argument("credential_id")
    perm_req.add_argument("--reason", required=True)
    perm_req.add_argument("--project-id", default="")
    perm_req.add_argument("--session-id", default="")
    perm_req.add_argument("--runtime", default="")
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
    return parser


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
        if args.permissions_command == "request":
            request_id = gate.request(
                credential_id=args.credential_id,
                reason=args.reason,
                project_id=args.project_id or None,
                session_id=args.session_id or None,
                device_id=settings.device_id,
                runtime=args.runtime or None,
            )
            print(json.dumps({"request_id": request_id, "status": "pending"}, sort_keys=True))
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


if __name__ == "__main__":
    main()
