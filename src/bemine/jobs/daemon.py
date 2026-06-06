from __future__ import annotations

import argparse
import json

from ..config import BemineSettings
from .core import BemineDaemon


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bemine-daemon", description="Bemine background daemon.")
    parser.add_argument("--once", action="store_true", help="Run one scheduler pass and exit.")
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    settings = BemineSettings.load()
    daemon = BemineDaemon(settings, poll_seconds=args.poll_seconds)
    if args.once:
        results = daemon.run_once()
        payload = [result.__dict__ for result in results]
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            if not results:
                print("No due jobs.")
            for result in results:
                print(f"{result.job_id}: {result.status} notified={result.notified}")
        return
    daemon.run_forever()


if __name__ == "__main__":
    main()
