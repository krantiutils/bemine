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
            max_tokens=1600,
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
            max_tokens=1600,
        )
        raw = self._sdk.responses.create(**kwargs)
        return self._transport.normalize_response(raw)


import os  # noqa: E402

from bemine.config import BemineSettings, config_string  # noqa: E402


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
        return AnthropicClient(sdk=sdk, base_url=base_url)

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
