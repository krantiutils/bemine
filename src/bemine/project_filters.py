from __future__ import annotations

from pathlib import Path


def ignored_project_reason(cwd: str | Path, *, name: str = "") -> str | None:
    path = Path(str(cwd)).expanduser()
    project_name = (name or path.name).strip().lower().replace("_", "-")
    path_name = path.name.strip().lower().replace("_", "-")
    if project_name == "witness" and path_name == "witness":
        return "gastown witness workspace"
    return None
