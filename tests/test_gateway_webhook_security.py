from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import bemine.discord.gateway as gateway


@pytest.mark.parametrize("content", ["blog-comment my-post\nchange this", "run a command"])
def test_webhooks_cannot_invoke_commands_even_when_all_users_allowed(monkeypatch, content):
    bot = SimpleNamespace(tree=object(), user=SimpleNamespace(id=1))
    events = {}
    def register_event(handler):
        events[handler.__name__] = handler
        return handler
    bot.event = register_event
    instance = gateway.MayorDiscordGateway.__new__(gateway.MayorDiscordGateway)
    instance.bot = bot
    instance.settings = SimpleNamespace(allow_all_users=True, allowed_users=set())
    instance._handle_allowed_message = AsyncMock()
    command = Mock()
    monkeypatch.setattr(gateway, "run_command", command)
    monkeypatch.setattr(gateway, "register_slash_commands", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway, "_acknowledge_message", AsyncMock())
    instance._register_events()
    message = SimpleNamespace(author=SimpleNamespace(id=2), type=gateway.discord.MessageType.default, webhook_id=123, content=content)
    asyncio.run(events["on_message"](message))
    command.assert_not_called()
    instance._handle_allowed_message.assert_not_awaited()
    gateway._acknowledge_message.assert_not_awaited()
