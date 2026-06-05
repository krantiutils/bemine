from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from bemine.kimi_handler import KimiHandler, load_kimi_config


SECRET = "sk-kimi-redacted-test-value"


def _write_hermes_home(
    path: Path,
    *,
    base_url: str = "https://api.kimi.com/coding",
    api_mode: str = "",
) -> None:
    path.mkdir(parents=True, exist_ok=True)
    api_mode_line = f"  api_mode: {api_mode}\n" if api_mode else ""
    (path / "config.yaml").write_text(
        "model:\n"
        "  default: kimi-k2.6\n"
        "  provider: kimi-coding\n"
        f"  base_url: {base_url}\n"
        f"{api_mode_line}",
        encoding="utf-8",
    )
    (path / ".env").write_text(f"KIMI_API_KEY={SECRET}\n", encoding="utf-8")
    (path / "auth.json").write_text(
        json.dumps(
            {
                "credential_pool": {
                    "kimi-coding": [
                        {
                            "auth_type": "api_key",
                            "source": "env:KIMI_API_KEY",
                            "base_url": base_url,
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )


def test_kimi_config_reads_hermes_without_exposing_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.delenv("KIMI_CODING_API_KEY", raising=False)
    monkeypatch.delenv("KIMI_BASE_URL", raising=False)
    hermes = tmp_path / "hermes"
    _write_hermes_home(hermes)

    config = load_kimi_config(hermes)

    assert config.provider == "kimi-coding"
    assert config.model == "kimi-k2.6"
    assert config.base_url == "https://api.kimi.com/coding"
    assert config.api_mode == "anthropic_messages"
    assert config.api_key_env == "KIMI_API_KEY"
    assert config.api_key_present is True
    assert config.key_source == "hermes_env:KIMI_API_KEY"
    assert SECRET not in json.dumps(config.to_dict())


def test_kimi_handler_uses_anthropic_messages_wire_for_coding_endpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    hermes = tmp_path / "hermes"
    _write_hermes_home(hermes)
    calls: list[dict[str, Any]] = []

    async def transport(
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        calls.append({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
        return {
            "model": "kimi-k2.6",
            "content": [{"type": "text", "text": "bemine-kimi-ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 7},
        }

    result = asyncio.run(
        KimiHandler(hermes, transport=transport).ask_async(
            "reply ok",
            system="test system",
            max_tokens=32,
            timeout=3,
        )
    )

    assert result.text == "bemine-kimi-ok"
    assert result.input_tokens == 5
    assert result.output_tokens == 7
    assert calls[0]["url"] == "https://api.kimi.com/coding/v1/messages"
    assert calls[0]["headers"]["x-api-key"] == SECRET
    assert calls[0]["headers"]["anthropic-version"] == "2023-06-01"
    assert calls[0]["headers"]["User-Agent"] == "claude-code/0.1.0"
    assert calls[0]["payload"] == {
        "model": "kimi-k2.6",
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "reply ok"}],
        "system": "test system",
    }
    assert "temperature" not in calls[0]["payload"]
    assert SECRET not in json.dumps(result.to_dict())


class _KimiTestServer(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        self.__class__.requests.append(
            {
                "path": self.path,
                "key": self.headers.get("x-api-key"),
                "anthropic_version": self.headers.get("anthropic-version"),
                "body": json.loads(body),
            }
        )
        response = {
            "model": "kimi-k2.6",
            "content": [{"type": "text", "text": "bemine-kimi-ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 11, "output_tokens": 13},
        }
        encoded = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:
        return


def test_cli_kimi_ask_hits_http_endpoint_and_records_usage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.setenv("BEMINE_CONFIG", str(tmp_path / "missing-bemine.toml"))
    _KimiTestServer.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _KimiTestServer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        hermes = tmp_path / "hermes"
        _write_hermes_home(
            hermes,
            base_url=f"http://127.0.0.1:{server.server_port}/coding",
            api_mode="anthropic_messages",
        )
        state = tmp_path / "state"
        vault = tmp_path / "vault"

        status = subprocess.run(
            [
                sys.executable,
                "-m",
                "bemine.cli",
                "--state-dir",
                str(state),
                "--vault",
                str(vault),
                "--hermes-home",
                str(hermes),
                "kimi",
                "status",
                "--json",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        assert SECRET not in status.stdout
        assert json.loads(status.stdout)["api_key_present"] is True

        ask = subprocess.run(
            [
                sys.executable,
                "-m",
                "bemine.cli",
                "--state-dir",
                str(state),
                "--vault",
                str(vault),
                "--hermes-home",
                str(hermes),
                "--device",
                "testbox",
                "kimi",
                "ask",
                "reply with ok",
                "--max-tokens",
                "64",
                "--json",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert SECRET not in ask.stdout
    payload = json.loads(ask.stdout)
    assert payload["text"] == "bemine-kimi-ok"
    assert payload["usage"] == {
        "input_tokens": 11,
        "output_tokens": 13,
        "total_tokens": 24,
    }

    assert len(_KimiTestServer.requests) == 1
    request = _KimiTestServer.requests[0]
    assert request["path"] == "/coding/v1/messages"
    assert request["key"] == SECRET
    assert request["anthropic_version"] == "2023-06-01"
    assert request["body"]["messages"] == [{"role": "user", "content": "reply with ok"}]
    assert "temperature" not in request["body"]

    conn = sqlite3.connect(state / "bemine.db")
    try:
        usage = conn.execute(
            "SELECT provider, runtime, input_tokens, output_tokens, total_tokens, raw_json FROM usage_samples"
        ).fetchone()
        assert usage[:5] == ("kimi", "kimi", 11, 13, 24)
        assert SECRET not in usage[5]
        audit = conn.execute(
            "SELECT category, event_type, actor, device_id, payload_json FROM audit_events"
        ).fetchone()
        assert audit[:4] == ("runtime", "kimi_ask", "cli", "testbox")
        assert SECRET not in audit[4]
    finally:
        conn.close()
