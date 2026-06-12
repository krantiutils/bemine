from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path


def repo_dir() -> Path:
    """The git repo root containing this package (services run from it)."""
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        if (parent / ".git").exists():
            return parent
    return Path.cwd()


def current_head_sha(repo: Path | None = None, *, runner=subprocess.run) -> str:
    proc = runner(
        ["git", "-C", str(repo or repo_dir()), "rev-parse", "HEAD"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    return (proc.stdout or "").strip()


def boot_sha_resolver(repo: Path | None = None, *, runner=subprocess.run):
    """HEAD captured once, near boot. Retries while git hasn't answered, but the
    first success sticks — a long-lived process must report the sha it booted
    on, never the repo's later HEAD (which is code it never imported)."""
    cache = {"sha": ""}

    def resolve() -> str:
        if not cache["sha"]:
            try:
                cache["sha"] = current_head_sha(repo, runner=runner)
            except Exception:
                return ""
        return cache["sha"]

    return resolve


def heartbeat_path(settings, service: str) -> Path:
    return settings.state_dir / "deploy" / f"heartbeat-{service}.json"


def write_heartbeat(settings, service: str, sha: str) -> None:
    path = heartbeat_path(settings, service)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"service": service, "sha": sha, "pid": os.getpid(), "ts": time.time()},
        sort_keys=True,
    ), encoding="utf-8")


def read_heartbeat(settings, service: str) -> dict | None:
    path = heartbeat_path(settings, service)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
