from __future__ import annotations

from typing import Protocol, Sequence

from bemine.providers.types import NormalizedResponse


class LLMClient(Protocol):
    """Bemine's seam onto the vendored provider transports. One call = one model turn."""

    def complete(
        self,
        *,
        messages: Sequence[dict],
        tools: Sequence[dict],
        model: str,
        system: str,
    ) -> NormalizedResponse: ...


from bemine.providers import get_transport  # noqa: E402

import logging  # noqa: E402

LOG = logging.getLogger("bemine.agent.clients")


class FallbackClient:
    """Wraps a primary LLM client with a fallback (e.g. claude-via-meridian -> kimi).

    The meridian proxy intermittently 500s (its passthrough caps Claude Code at 3 internal
    turns); without this, one bad completion kills the whole dispatch/worker. Policy:
    try primary, retry primary once, then run THIS completion on the fallback client with
    the fallback model. The next completion tries the primary again (transient by design).
    """

    def __init__(self, primary, fallback, *, fallback_model: str) -> None:
        self._primary = primary
        self._fallback = fallback
        self._fallback_model = fallback_model

    def complete(self, *, messages, tools, model, system):
        last_exc: Exception | None = None
        for attempt in (1, 2):
            try:
                return self._primary.complete(
                    messages=messages, tools=tools, model=model, system=system
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                LOG.warning("primary client failed (attempt %d): %s", attempt, exc)
        try:
            LOG.warning("falling back to %s for this completion", self._fallback_model)
            return self._fallback.complete(
                messages=messages, tools=tools, model=self._fallback_model, system=system
            )
        except Exception:
            raise last_exc  # surface the PRIMARY failure, it's the real problem


class AnthropicClient:
    """Claude AND Anthropic-compatible Kimi. The vendored anthropic transport already
    detects api.kimi.com/coding and adjusts betas/auth."""

    def __init__(self, *, sdk, base_url: str | None = None):
        self._sdk = sdk
        self._base_url = base_url
        self._transport = get_transport("anthropic_messages")

    def complete(self, *, messages, tools, model, system):
        # Prepend system as a system-role message; build_anthropic_kwargs
        # (via convert_messages_to_anthropic) extracts it from messages internally.
        full_messages = [{"role": "system", "content": system}, *messages]
        kwargs = self._transport.build_kwargs(
            model=model,
            messages=full_messages,
            tools=list(tools),
            max_tokens=_max_output_tokens(),
            base_url=self._base_url,
        )
        raw = self._sdk.messages.create(**kwargs)
        return self._transport.normalize_response(raw)


class CodexClient:
    """Codex via the OpenAI Responses API, through the vendored codex transport."""

    def __init__(self, *, sdk):
        self._sdk = sdk
        self._transport = get_transport("codex_responses")

    def complete(self, *, messages, tools, model, system):
        # Prepend system as a system-role message; build_kwargs extracts it from
        # messages[0] when role == "system" and instructions param is absent.
        full_messages = [{"role": "system", "content": system}, *messages]
        kwargs = self._transport.build_kwargs(
            model=model,
            messages=full_messages,
            tools=list(tools),
            max_tokens=_max_output_tokens(),
        )
        raw = self._sdk.responses.create(**kwargs)
        return self._transport.normalize_response(raw)


import os  # noqa: E402

from bemine.config import BemineSettings, config_int, config_string  # noqa: E402


def _max_output_tokens() -> int:
    # Sane human default: long enough for a substantial chat reply, tunable via
    # [agent].max_output_tokens in bemine.toml.
    return config_int("agent", "max_output_tokens", 4096)


def build_client_for_runtime(settings: BemineSettings, runtime: str):
    """Deterministic runtime -> LLMClient. Construction never makes network calls and
    never raises in a keyless env: a placeholder api_key is used when none is configured."""
    runtime = (runtime or "kimi").lower()
    if runtime == "codex":
        import openai

        return CodexClient(
            sdk=openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY", "sk-none"))
        )

    import anthropic

    if runtime == "claude":
        base_url = config_string("claude", "base_url", "") or None
        api_key = (
            os.environ.get(
                config_string("claude", "api_key_env", "MERIDIAN_API_KEY"), "sk-none"
            )
            or "sk-none"
        )
        sdk = (
            anthropic.Anthropic(api_key=api_key, base_url=base_url)
            if base_url
            else anthropic.Anthropic(api_key=api_key)
        )
        primary = AnthropicClient(sdk=sdk, base_url=base_url)
        # Meridian 500s intermittently; fall back to kimi for the affected completion so
        # a proxy hiccup never kills a dispatch/worker. Disable with fallback_runtime="".
        fb_runtime = config_string("claude", "fallback_runtime", "kimi")
        if not fb_runtime.strip() or fb_runtime.strip().lower() == "claude":
            return primary
        fallback = build_client_for_runtime(settings, fb_runtime)
        fb_model = config_string(fb_runtime, "model", "") or "kimi-k2.6"
        return FallbackClient(primary, fallback, fallback_model=fb_model)

    # kimi (Anthropic-compatible)
    base_url = config_string("kimi", "base_url", "https://api.kimi.com/coding")
    api_key = ""
    try:
        from bemine.kimi_handler import load_kimi_config, resolve_kimi_api_key

        config = load_kimi_config(settings.hermes_home)
        api_key = resolve_kimi_api_key(settings.hermes_home, config)
    except Exception:
        api_key = ""
    if not api_key:
        api_key = (
            os.environ.get(config_string("kimi", "api_key_env", "KIMI_API_KEY"), "")
            or "sk-none"
        )
    sdk = anthropic.Anthropic(api_key=api_key, base_url=base_url)
    return AnthropicClient(sdk=sdk, base_url=base_url)
