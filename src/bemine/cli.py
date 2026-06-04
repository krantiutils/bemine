from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import BemineSettings
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


if __name__ == "__main__":
    main()

