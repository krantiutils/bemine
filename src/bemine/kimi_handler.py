from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp

KIMI_CODE_BASE_URL = "https://api.kimi.com/coding"
KIMI_DEFAULT_MODEL = "kimi-k2.6"
ANTHROPIC_VERSION = "2023-06-01"
KIMI_CODING_USER_AGENT = "claude-code/0.1.0"

_COMMON_ANTHROPIC_BETAS = (
    "interleaved-thinking-2025-05-14",
    "fine-grained-tool-streaming-2025-05-14",
)
_SECRETISH_RE = re.compile(
    r"(?i)\b(?:sk|ak|pk|ghp|github_pat|hf|xox[baprs])[-_a-z0-9]{12,}\b"
)

JsonTransport = Callable[
    [str, Mapping[str, str], Mapping[str, Any], float],
    Awaitable[Mapping[str, Any]] | Mapping[str, Any],
]


@dataclass(frozen=True)
class KimiConfig:
    provider: str
    model: str
    base_url: str
    api_mode: str
    api_key_env: str
    api_key_present: bool
    key_source: str
    config_path: str
    env_path: str
    auth_path: str
    health_url: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "api_mode": self.api_mode,
            "api_key_env": self.api_key_env,
            "api_key_present": self.api_key_present,
            "key_source": self.key_source,
            "config_path": self.config_path,
            "env_path": self.env_path,
            "auth_path": self.auth_path,
            "health_url": self.health_url,
        }


@dataclass(frozen=True)
class KimiMessageResult:
    text: str
    model: str
    provider: str
    api_mode: str
    finish_reason: str
    input_tokens: int
    output_tokens: int
    total_tokens: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "model": self.model,
            "provider": self.provider,
            "api_mode": self.api_mode,
            "finish_reason": self.finish_reason,
            "usage": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "total_tokens": self.total_tokens,
            },
        }


class KimiConfigError(RuntimeError):
    pass


class KimiAPIError(RuntimeError):
    pass


class KimiHandler:
    def __init__(
        self,
        hermes_home: Path,
        *,
        transport: JsonTransport | None = None,
    ) -> None:
        self.hermes_home = hermes_home.expanduser().resolve()
        self.config = load_kimi_config(self.hermes_home)
        self._transport = transport or _post_json

    def status(self) -> KimiConfig:
        return self.config

    def ask(
        self,
        prompt: str,
        *,
        system: str = "",
        max_tokens: int = 512,
        timeout: float = 60.0,
    ) -> KimiMessageResult:
        return asyncio.run(
            self.ask_async(
                prompt,
                system=system,
                max_tokens=max_tokens,
                timeout=timeout,
            )
        )

    async def ask_async(
        self,
        prompt: str,
        *,
        system: str = "",
        max_tokens: int = 512,
        timeout: float = 60.0,
    ) -> KimiMessageResult:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("prompt is required")
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")

        api_key = resolve_kimi_api_key(self.hermes_home, self.config)
        if not api_key:
            raise KimiConfigError(
                f"{self.config.api_key_env} is missing from the environment and Hermes .env"
            )

        if self.config.api_mode == "anthropic_messages":
            url, headers, payload = _anthropic_request(self.config, api_key, prompt, system, max_tokens)
            data = await _maybe_await(self._transport(url, headers, payload, timeout))
            return _parse_anthropic_response(data, self.config)

        if self.config.api_mode == "chat_completions":
            url, headers, payload = _chat_request(self.config, api_key, prompt, system, max_tokens)
            data = await _maybe_await(self._transport(url, headers, payload, timeout))
            return _parse_chat_response(data, self.config)

        raise KimiConfigError(f"unsupported Kimi api_mode: {self.config.api_mode}")


def load_kimi_config(hermes_home: Path) -> KimiConfig:
    hermes_home = hermes_home.expanduser().resolve()
    config_path = hermes_home / "config.yaml"
    env_path = hermes_home / ".env"
    auth_path = hermes_home / "auth.json"

    model_cfg = _read_model_config(config_path)
    provider = str(model_cfg.get("provider") or "kimi-coding").strip()
    model = str(model_cfg.get("default") or model_cfg.get("model") or KIMI_DEFAULT_MODEL).strip()

    env_values = _read_env_file(env_path)
    auth_entry = _read_credential_pool_entry(auth_path, provider)
    api_key_env = _resolve_api_key_env(provider, auth_entry, env_values)
    api_key = _first_env_value(api_key_env, env_values)

    configured_base = str(model_cfg.get("base_url") or "").strip()
    auth_base = str(auth_entry.get("base_url") or "").strip() if auth_entry else ""
    env_base = _first_env_value(_base_url_env(provider), env_values)
    base_url = configured_base or auth_base or _default_base_url(provider, api_key, env_base)

    configured_api_mode = str(model_cfg.get("api_mode") or "").strip()
    api_mode = configured_api_mode or detect_api_mode(provider, base_url)
    key_source = _key_source(api_key_env, env_values)

    return KimiConfig(
        provider=provider,
        model=model,
        base_url=base_url.rstrip("/"),
        api_mode=api_mode,
        api_key_env=api_key_env,
        api_key_present=bool(api_key),
        key_source=key_source,
        config_path=str(config_path),
        env_path=str(env_path),
        auth_path=str(auth_path),
        health_url=_health_url(base_url),
    )


def resolve_kimi_api_key(hermes_home: Path, config: KimiConfig) -> str:
    env_value = os.environ.get(config.api_key_env, "").strip()
    if env_value:
        return env_value
    return _read_env_file(hermes_home / ".env").get(config.api_key_env, "").strip()


def render_kimi_status(config: KimiConfig) -> str:
    present = "present" if config.api_key_present else "missing"
    return "\n".join(
        [
            "Kimi handler:",
            f"- provider: {config.provider}",
            f"- model: {config.model}",
            f"- base_url: {config.base_url}",
            f"- api_mode: {config.api_mode}",
            f"- api_key: {present} via {config.api_key_env}",
            f"- health_url: {config.health_url}",
            "",
        ]
    )


def usage_payload(result: KimiMessageResult) -> dict[str, Any]:
    return {
        "provider": result.provider,
        "model": result.model,
        "api_mode": result.api_mode,
        "finish_reason": result.finish_reason,
        "usage": {
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "total_tokens": result.total_tokens,
        },
    }


def detect_api_mode(provider: str, base_url: str) -> str:
    normalized = (base_url or "").strip().lower().rstrip("/")
    hostname = (urlparse(normalized).hostname or "").lower()
    if normalized.endswith("/anthropic") or hostname == "api.anthropic.com":
        return "anthropic_messages"
    if hostname == "api.kimi.com" and "/coding" in normalized:
        return "anthropic_messages"
    if hostname == "api.openai.com":
        return "codex_responses"
    if provider in {"kimi-coding", "kimi-coding-cn"} and normalized.endswith("/coding"):
        return "anthropic_messages"
    return "chat_completions"


def _anthropic_request(
    config: KimiConfig,
    api_key: str,
    prompt: str,
    system: str,
    max_tokens: int,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    payload: dict[str, Any] = {
        "model": config.model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system.strip():
        payload["system"] = system.strip()

    headers = {
        "content-type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
    }
    if _is_kimi_coding_endpoint(config.base_url):
        headers["User-Agent"] = KIMI_CODING_USER_AGENT
        headers["anthropic-beta"] = ",".join(_COMMON_ANTHROPIC_BETAS)

    return _anthropic_messages_url(config.base_url), headers, payload


def _chat_request(
    config: KimiConfig,
    api_key: str,
    prompt: str,
    system: str,
    max_tokens: int,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    messages: list[dict[str, str]] = []
    if system.strip():
        messages.append({"role": "system", "content": system.strip()})
    messages.append({"role": "user", "content": prompt})

    return (
        _chat_completions_url(config.base_url),
        {
            "content-type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        {
            "model": config.model,
            "max_tokens": max_tokens,
            "messages": messages,
        },
    )


def _parse_anthropic_response(data: Mapping[str, Any], config: KimiConfig) -> KimiMessageResult:
    content = data.get("content") or []
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, Mapping):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)

    usage = data.get("usage") if isinstance(data.get("usage"), Mapping) else {}
    input_tokens = _int(usage.get("input_tokens"))
    output_tokens = _int(usage.get("output_tokens"))
    return KimiMessageResult(
        text="".join(parts).strip(),
        model=str(data.get("model") or config.model),
        provider=config.provider,
        api_mode=config.api_mode,
        finish_reason=str(data.get("stop_reason") or ""),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )


def _parse_chat_response(data: Mapping[str, Any], config: KimiConfig) -> KimiMessageResult:
    choices = data.get("choices") if isinstance(data.get("choices"), list) else []
    first = choices[0] if choices and isinstance(choices[0], Mapping) else {}
    message = first.get("message") if isinstance(first.get("message"), Mapping) else {}
    content = message.get("content") if isinstance(message.get("content"), str) else ""
    usage = data.get("usage") if isinstance(data.get("usage"), Mapping) else {}
    input_tokens = _int(usage.get("prompt_tokens"))
    output_tokens = _int(usage.get("completion_tokens"))
    total_tokens = _int(usage.get("total_tokens")) or input_tokens + output_tokens
    return KimiMessageResult(
        text=content.strip(),
        model=str(data.get("model") or config.model),
        provider=config.provider,
        api_mode=config.api_mode,
        finish_reason=str(first.get("finish_reason") or ""),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


async def _post_json(
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
    timeout: float,
) -> Mapping[str, Any]:
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(timeout=client_timeout) as session:
        async with session.post(url, headers=dict(headers), json=dict(payload)) as response:
            body = await response.text()
            if response.status >= 400:
                raise KimiAPIError(f"Kimi request failed {response.status}: {_redact(body)}")
            try:
                data = json.loads(body)
            except json.JSONDecodeError as exc:
                raise KimiAPIError(f"Kimi response was not JSON: {_redact(body[:300])}") from exc
            if not isinstance(data, Mapping):
                raise KimiAPIError("Kimi response JSON was not an object")
            return data


async def _maybe_await(value: Awaitable[Mapping[str, Any]] | Mapping[str, Any]) -> Mapping[str, Any]:
    if hasattr(value, "__await__"):
        return await value  # type: ignore[misc]
    return value


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        values[key] = _strip_quotes(value.strip())
    return values


def _read_model_config(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    lines = path.read_text(encoding="utf-8").splitlines()
    for idx, raw_line in enumerate(lines):
        if not raw_line.startswith("model:"):
            continue
        inline = raw_line.split(":", 1)[1].strip()
        if inline:
            return {"default": _strip_quotes(inline)}

        block: dict[str, str] = {}
        for child in lines[idx + 1 :]:
            if not child.strip() or child.lstrip().startswith("#"):
                continue
            if not child.startswith((" ", "\t")):
                break
            stripped = child.strip()
            if ":" not in stripped:
                continue
            key, value = stripped.split(":", 1)
            block[key.strip()] = _strip_quotes(value.strip())
        return block
    return {}


def _read_credential_pool_entry(path: Path, provider: str) -> Mapping[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    pool = data.get("credential_pool") if isinstance(data, Mapping) else None
    entries = pool.get(provider) if isinstance(pool, Mapping) else None
    if not isinstance(entries, list):
        return {}
    for entry in entries:
        if isinstance(entry, Mapping) and entry.get("auth_type") == "api_key":
            return entry
    return {}


def _resolve_api_key_env(
    provider: str,
    auth_entry: Mapping[str, Any],
    env_values: Mapping[str, str],
) -> str:
    source = str(auth_entry.get("source") or "")
    if source.startswith("env:") and source[4:].strip():
        return source[4:].strip()
    for candidate in _api_key_env_candidates(provider):
        if os.environ.get(candidate, "").strip() or env_values.get(candidate, "").strip():
            return candidate
    return _api_key_env_candidates(provider)[0]


def _api_key_env_candidates(provider: str) -> tuple[str, ...]:
    if provider == "kimi-coding-cn":
        return ("KIMI_CN_API_KEY",)
    return ("KIMI_API_KEY", "KIMI_CODING_API_KEY")


def _base_url_env(provider: str) -> str:
    if provider == "kimi-coding":
        return "KIMI_BASE_URL"
    return ""


def _default_base_url(provider: str, api_key: str, env_base: str) -> str:
    if env_base:
        return env_base
    if provider == "kimi-coding-cn":
        return "https://api.moonshot.cn/v1"
    if api_key.startswith("sk-kimi-"):
        return KIMI_CODE_BASE_URL
    return "https://api.moonshot.ai/v1"


def _first_env_value(name: str, env_values: Mapping[str, str]) -> str:
    if not name:
        return ""
    return os.environ.get(name, "").strip() or env_values.get(name, "").strip()


def _key_source(api_key_env: str, env_values: Mapping[str, str]) -> str:
    if os.environ.get(api_key_env, "").strip():
        return f"process_env:{api_key_env}"
    if env_values.get(api_key_env, "").strip():
        return f"hermes_env:{api_key_env}"
    return "missing"


def _anthropic_messages_url(base_url: str) -> str:
    base = re.sub(r"/v1/?$", "", base_url.rstrip("/"))
    return base + "/v1/messages"


def _chat_completions_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base + "/chat/completions"


def _health_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if _is_kimi_coding_endpoint(base):
        return base + "/v1/models"
    if not base.endswith("/v1"):
        base += "/v1"
    return base + "/models"


def _is_kimi_coding_endpoint(base_url: str) -> bool:
    normalized = base_url.rstrip("/").lower()
    hostname = (urlparse(normalized).hostname or "").lower()
    return hostname == "api.kimi.com" and "/coding" in normalized


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _redact(text: str) -> str:
    return _SECRETISH_RE.sub("<secret-redacted>", text)
