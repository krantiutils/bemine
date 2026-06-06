from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from bemine.config import config_bool, config_list, config_string


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def load_env_files() -> dict[str, str]:
    """Load local env first, then Hermes env as a compatibility fallback."""
    merged: dict[str, str] = {}
    for path in (Path.cwd() / ".env", Path.home() / ".hermes" / ".env"):
        for key, value in _parse_env_file(path).items():
            merged.setdefault(key, value)
    return merged


def _env(name: str, loaded: dict[str, str], default: str = "") -> str:
    return os.environ.get(name) or loaded.get(name) or default


def _env_from_config(name: str, loaded: dict[str, str], default: str = "") -> str:
    configured_env = config_string("discord", name, "")
    if configured_env:
        return _env(configured_env, loaded, default)
    return default


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _split_ids(value: str) -> set[str]:
    return {part.strip() for part in value.split(",") if part.strip()}


def _approved_hermes_discord_users() -> set[str]:
    path = Path.home() / ".hermes" / "pairing" / "discord-approved.json"
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    if not isinstance(data, dict):
        return set()
    return {str(key) for key in data.keys() if str(key).strip()}


@dataclass(frozen=True)
class Settings:
    token: str
    home_channel_id: str
    allowed_users: set[str]
    allow_all_users: bool
    mayor_home: Path
    db_path: Path
    inbox_dir: Path

    @classmethod
    def load(cls) -> "Settings":
        loaded = load_env_files()

        token = (
            _env("MAYOR_DISCORD_BOT_TOKEN", loaded)
            or _env("DISCORD_BOT_TOKEN", loaded)
            or _env_from_config("token_env", loaded)
        ).strip()
        home_channel_id = (
            _env("MAYOR_DISCORD_HOME_CHANNEL", loaded)
            or _env("DISCORD_HOME_CHANNEL", loaded)
            or config_string("discord", "home_channel_id", "")
        ).strip()
        allowed_raw = _env("MAYOR_DISCORD_ALLOWED_USERS", loaded) or _env(
            "DISCORD_ALLOWED_USERS", loaded
        )
        allowed_users = _split_ids(allowed_raw)
        if not allowed_users:
            allowed_users = set(config_list("discord", "allowed_users"))
        if not allowed_users:
            allowed_users = _approved_hermes_discord_users()

        allow_all_users = _truthy(
            _env("MAYOR_DISCORD_ALLOW_ALL_USERS", loaded)
            or _env("DISCORD_ALLOW_ALL_USERS", loaded)
        ) or config_bool("discord", "allow_all_users", False)

        home_raw = _env("MAYOR_HOME", loaded)
        home_raw = _env("BEMINE_HOME", loaded) or home_raw
        home_raw = home_raw or config_string("paths", "state_dir", "")
        mayor_home = Path(home_raw).expanduser() if home_raw else Path.cwd() / ".bemine"
        mayor_home = mayor_home.resolve()

        return cls(
            token=token,
            home_channel_id=home_channel_id,
            allowed_users=allowed_users,
            allow_all_users=allow_all_users,
            mayor_home=mayor_home,
            db_path=mayor_home / "bemine.db",
            inbox_dir=mayor_home / "inbox",
        )

    def require_token(self) -> None:
        if not self.token:
            raise SystemExit(
                "Discord token not configured. Set MAYOR_DISCORD_BOT_TOKEN "
                "or DISCORD_BOT_TOKEN, or set discord.token_env in bemine.toml."
            )

    def require_home_channel(self) -> None:
        if not self.home_channel_id:
            raise SystemExit(
                "Discord home channel not configured. Set MAYOR_DISCORD_HOME_CHANNEL "
                "or DISCORD_HOME_CHANNEL, or set discord.home_channel_id in bemine.toml."
            )
