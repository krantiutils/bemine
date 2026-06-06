"""Replacements for Hermes-only deps used by the vendored adapter files."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

# Replaces agent.prompt_builder.DEFAULT_AGENT_IDENTITY
DEFAULT_AGENT_IDENTITY = "You are Bemine, the operator's personal multi-device agent."


def get_hermes_home() -> Path:
    """Replaces hermes_constants.get_hermes_home(). The adapters only use this for
    optional on-disk credential lookups we do not exercise; a stable home is fine."""
    return Path.home() / ".bemine" / "provider-home"


def base_url_host_matches(base_url: str | None, host: str) -> bool:
    if not base_url:
        return False
    return (urlparse(base_url).hostname or "").lower() == host.lower()


def normalize_proxy_env_vars() -> None:
    """No-op: Bemine does not rewrite proxy env."""
    return None
