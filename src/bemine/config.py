from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


CONFIG_FILE_NAME = "bemine.toml"


def _config_path() -> Path:
    raw = os.environ.get("BEMINE_CONFIG", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.cwd() / CONFIG_FILE_NAME).resolve()


def load_bemine_config(path: Path | None = None) -> dict[str, Any]:
    config_path = path or _config_path()
    if not config_path.exists():
        return {}
    with config_path.open("rb") as handle:
        data = tomllib.load(handle)
    return data if isinstance(data, dict) else {}


def config_path() -> Path:
    return _config_path()


def render_toml_config(data: Mapping[str, Any]) -> str:
    lines: list[str] = []
    scalars: dict[str, Any] = {}
    sections: dict[str, Mapping[str, Any]] = {}
    for key, value in data.items():
        if isinstance(value, Mapping):
            sections[str(key)] = value
        else:
            scalars[str(key)] = value
    for key, value in scalars.items():
        lines.append(f"{key} = {_toml_literal(value)}")
    for section, values in sections.items():
        if lines and lines[-1] != "":
            lines.append("")
        lines.append(f"[{section}]")
        for key, value in values.items():
            lines.append(f"{key} = {_toml_literal(value)}")
    return "\n".join(lines).rstrip() + "\n"


def update_bemine_config(
    updates: Mapping[tuple[str, str], Any],
    *,
    path: Path | None = None,
) -> Path:
    target = path or _config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        data: dict[str, dict[str, Any]] = {}
        for (section, key), value in updates.items():
            data.setdefault(section, {})[key] = value
        target.write_text(render_toml_config(data), encoding="utf-8")
        return target

    lines = target.read_text(encoding="utf-8").splitlines()
    pending = dict(updates)
    current_section = ""
    for index, line in enumerate(lines):
        parsed_section = _line_section(line)
        if parsed_section is not None:
            current_section = parsed_section
        key = _line_key(line)
        if key is None:
            continue
        update_key = (current_section, key)
        if update_key in pending:
            lines[index] = f"{key} = {_toml_literal(pending.pop(update_key))}"

    for section in sorted({section for section, _key in pending}):
        section_updates = {
            key: pending.pop((section, key))
            for pending_section, key in list(pending)
            if pending_section == section
        }
        section_line = _find_section_line(lines, section)
        if section_line is None:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(f"[{section}]")
            for key, value in section_updates.items():
                lines.append(f"{key} = {_toml_literal(value)}")
            continue
        insert_at = section_line + 1
        while insert_at < len(lines) and _line_section(lines[insert_at]) is None:
            insert_at += 1
        new_lines = [f"{key} = {_toml_literal(value)}" for key, value in section_updates.items()]
        lines[insert_at:insert_at] = new_lines

    target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return target


def _toml_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_toml_literal(item) for item in value) + "]"
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _line_section(line: str) -> str | None:
    stripped = line.strip()
    if stripped.startswith("[") and stripped.endswith("]") and not stripped.startswith("[["):
        return stripped[1:-1].strip()
    return None


def _line_key(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    return stripped.split("=", 1)[0].strip()


def _find_section_line(lines: list[str], section: str) -> int | None:
    for index, line in enumerate(lines):
        if _line_section(line) == section:
            return index
    return None


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)
    return value if isinstance(value, dict) else {}


def _config_str(data: dict[str, Any], section: str, key: str, default: str = "") -> str:
    value = _section(data, section).get(key, default)
    return str(value).strip() if value is not None else default


def config_string(section: str, key: str, default: str = "") -> str:
    return _config_str(load_bemine_config(), section, key, default)


def config_bool(section: str, key: str, default: bool = False) -> bool:
    value = _section(load_bemine_config(), section).get(key, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def config_int(section: str, key: str, default: int) -> int:
    value = _section(load_bemine_config(), section).get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def config_list(section: str, key: str, default: list[str] | None = None) -> list[str]:
    default = default or []
    value = _section(load_bemine_config(), section).get(key, default)
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [part.strip() for part in value.split(os.pathsep) if part.strip()]
    return list(default)


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return default.expanduser().resolve()


def _config_path_value(data: dict[str, Any], key: str, default: Path) -> Path:
    raw = _config_str(data, "paths", key, "")
    if raw:
        return Path(raw).expanduser().resolve()
    return default.expanduser().resolve()


@dataclass(frozen=True)
class BemineSettings:
    state_dir: Path
    db_path: Path
    vault_dir: Path
    codex_home: Path
    claude_home: Path
    kimi_home: Path
    hermes_home: Path
    device_id: str
    status_devices: list[str]

    @classmethod
    def load(
        cls,
        *,
        state_dir: Path | None = None,
        vault_dir: Path | None = None,
        codex_home: Path | None = None,
        claude_home: Path | None = None,
        kimi_home: Path | None = None,
        hermes_home: Path | None = None,
        device_id: str | None = None,
    ) -> "BemineSettings":
        cwd = Path.cwd()
        config = load_bemine_config()
        resolved_state = (
            state_dir.expanduser().resolve()
            if state_dir
            else _env_path(
                "BEMINE_HOME", _config_path_value(config, "state_dir", cwd / ".bemine")
            )
        )
        resolved_vault = (
            vault_dir.expanduser().resolve()
            if vault_dir
            else _env_path(
                "BEMINE_VAULT",
                _config_path_value(
                    config, "vault_dir", Path.home() / "Documents" / "Mayor"
                ),
            )
        )
        resolved_codex = (
            codex_home.expanduser().resolve()
            if codex_home
            else _env_path(
                "BEMINE_CODEX_HOME",
                _config_path_value(config, "codex_home", Path.home() / ".codex"),
            )
        )
        resolved_claude = (
            claude_home.expanduser().resolve()
            if claude_home
            else _env_path(
                "BEMINE_CLAUDE_HOME",
                _config_path_value(config, "claude_home", Path.home() / ".claude"),
            )
        )
        resolved_kimi = (
            kimi_home.expanduser().resolve()
            if kimi_home
            else _env_path(
                "BEMINE_KIMI_HOME",
                _config_path_value(
                    config, "kimi_home", Path.home() / "devlog" / "raw" / "kimi"
                ),
            )
        )
        resolved_hermes = (
            hermes_home.expanduser().resolve()
            if hermes_home
            else _env_path(
                "BEMINE_HERMES_HOME",
                _config_path_value(config, "hermes_home", Path.home() / ".hermes"),
            )
        )
        resolved_device = (
            device_id
            or os.environ.get("BEMINE_DEVICE_ID", "").strip()
            or _config_str(config, "identity", "device_id", "")
            or os.uname().nodename
        )
        status_devices = config_list(
            "status",
            "devices",
            ["local", "mbp", "mbp2", "k2"],
        )
        return cls(
            state_dir=resolved_state,
            db_path=resolved_state / "bemine.db",
            vault_dir=resolved_vault,
            codex_home=resolved_codex,
            claude_home=resolved_claude,
            kimi_home=resolved_kimi,
            hermes_home=resolved_hermes,
            device_id=resolved_device,
            status_devices=status_devices,
        )

    def ensure_dirs(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.vault_dir.mkdir(parents=True, exist_ok=True)
