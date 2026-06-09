from __future__ import annotations

import json
import re
from pathlib import Path

RATE_LIMIT_DEFAULTS = [
    "server is temporarily limiting requests",
    "rate limited",
    "rate_limit",
    "api error",
    "overloaded",
    "529",
    "too many requests",
    "please try again",
]

# Obvious "just say yes / press enter" prompts that are safe to auto-confirm.
_TRIVIAL_CONFIRM = re.compile(
    r"\(y/n\)|\[y/n\]|yes/no|y/n\)|proceed\?|continue\?|press enter|"
    r"do you want to (proceed|continue)|confirm\?|\bok\?\b",
    re.IGNORECASE,
)


def classify(pane_text: str, asks_user: bool, *, signatures: list[str]) -> str:
    low = (pane_text or "").lower()
    if any(sig in low for sig in signatures):
        return "rate_limit"
    if asks_user:
        if _TRIVIAL_CONFIRM.search(pane_text or ""):
            return "trivial_confirm"
        return "ambiguous"
    return "healthy"


def recovery_state_path(settings) -> Path:
    return settings.state_dir / "recovery_state.json"


def load_recovery_state(path: Path) -> dict:
    if not Path(path).exists():
        return {}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def save_recovery_state(path: Path, state: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def recovery_key(status) -> str:
    return f"{status.device_id}:{status.runtime}:{status.session_id}"
