"""Runtime CLI preflight: is each agent runtime installed AND usable?

Services can be green while a capability is dead — kimi sat unauthenticated
("LLM not set") for days with [goal].runtime = "kimi", so every goal worker
booted braindead and nothing alerted. These are cheap disk checks (no tokens,
no subprocesses) so health can run them on every call.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from bemine.config import BemineSettings, config_string
from bemine.workers.workers import _resolve_runtime_bin

PREFLIGHT_RUNTIMES = ("claude", "codex", "kimi")


def _kimi_cli_home() -> Path:
    # NOT settings.kimi_home — that is the (currently empty) devlog mirror used
    # by discovery; the CLI itself reads ~/.kimi.
    return Path(config_string("kimi", "cli_home", "~/.kimi")).expanduser()


def _auth_check(runtime: str, settings: BemineSettings) -> tuple[bool, str]:
    if runtime == "claude":
        creds = settings.claude_home / ".credentials.json"
        if creds.is_file():
            return True, "credentials present"
        return False, f"no credentials at {creds}"
    if runtime == "codex":
        auth = settings.codex_home / "auth.json"
        if auth.is_file() and auth.stat().st_size > 2:
            return True, "auth.json present"
        return False, f"not logged in (missing {auth})"
    if runtime == "kimi":
        config = _kimi_cli_home() / "config.toml"
        if not config.is_file():
            return False, f"no CLI config at {config}"
        try:
            data = tomllib.loads(config.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            return False, f"unreadable CLI config: {exc}"
        if str(data.get("default_model") or "").strip() or data.get("providers"):
            return True, "LLM configured"
        return False, "LLM not set — run kimi and /login"
    return False, "unknown runtime"


def runtime_preflight(settings: BemineSettings) -> list[dict[str, Any]]:
    goal_runtime = config_string("goal", "runtime", "claude").strip().lower()
    rows: list[dict[str, Any]] = []
    for runtime in PREFLIGHT_RUNTIMES:
        bin_path = _resolve_runtime_bin(runtime)
        installed = "/" in bin_path and Path(bin_path).is_file()
        if installed:
            authenticated, detail = _auth_check(runtime, settings)
        else:
            authenticated, detail = False, "binary not found"
        rows.append(
            {
                "runtime": runtime,
                "bin": bin_path if installed else "",
                "installed": installed,
                "authenticated": authenticated,
                "ok": installed and authenticated,
                "goal_default": runtime == goal_runtime,
                "detail": detail,
            }
        )
    return rows


def preflight_issues(rows: list[dict[str, Any]]) -> list[str]:
    """Health-issue lines: a broken goal-default runtime always matters; other
    runtimes only when installed but unusable (not-installed extras are fine)."""
    issues = []
    for row in rows:
        if row["ok"]:
            continue
        if row["goal_default"]:
            issues.append(
                f"goal default runtime {row['runtime']} is unusable: {row['detail']}"
            )
        elif row["installed"]:
            issues.append(f"runtime {row['runtime']} is unusable: {row['detail']}")
    return issues


def render_runtime_preflight(rows: list[dict[str, Any]]) -> str:
    lines = ["Runtimes:"]
    for row in rows:
        marker = "ok" if row["ok"] else "UNUSABLE"
        default = " (goal default)" if row["goal_default"] else ""
        lines.append(f"- {row['runtime']}{default}: {marker} — {row['detail']}")
    return "\n".join(lines) + "\n"
