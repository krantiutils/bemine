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


# Faithful copy of agent.model_metadata.grok_supports_reasoning_effort — a small
# pure function used by codex_transport on the xAI/Grok path only.
_GROK_EFFORT_CAPABLE_PREFIXES = (
    "grok-3-mini",
    "grok-4.20-multi-agent",
    "grok-4.3",
)


def grok_supports_reasoning_effort(model: str) -> bool:
    """Return True when an xAI Grok model accepts ``reasoning.effort``.

    Allowlist by substring (matches both bare ``grok-3-mini`` and
    aggregator-prefixed ``x-ai/grok-3-mini``). Conservative by design:
    if a future Grok model isn't listed, we send no effort dial rather
    than 400.
    """
    name = (model or "").strip().lower()
    if not name:
        return False
    for sep in ("/",):
        if sep in name:
            name = name.rsplit(sep, 1)[-1]
    return any(name.startswith(prefix) for prefix in _GROK_EFFORT_CAPABLE_PREFIXES)
