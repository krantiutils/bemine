from __future__ import annotations

from bemine.index.status import SessionStatus

# tmux pane commands that indicate an interactive agent session.
_AGENT_COMMANDS = {"claude", "codex", "kimi", "node", "python", "python3"}


def _basename(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1]


def resolve_project_session(statuses: list[SessionStatus],
                            project_name: str) -> SessionStatus | None:
    """The most-recently-active fleet session whose cwd basename matches the project."""
    matches = [s for s in statuses if _basename(s.cwd) == project_name]
    if not matches:
        return None
    return min(matches, key=lambda s: s.age_seconds)


def resolve_pane(tmux, device: str, cwd: str) -> str | None:
    """Find the tmux pane on `device` whose current path matches the session cwd.

    Prefers a pane also running an agent command, but falls back to any path match.
    """
    panes = tmux.list_panes(device)
    target = cwd.rstrip("/")
    path_matches = [p for p in panes if p.get("path", "").rstrip("/") == target]
    if not path_matches:
        return None
    for pane in path_matches:
        if pane.get("command", "") in _AGENT_COMMANDS:
            return pane["pane_id"]
    return path_matches[0]["pane_id"]
