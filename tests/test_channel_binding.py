from pathlib import Path

from bemine.config import BemineSettings
from bemine.agent.clients import build_client_for_runtime, AnthropicClient, CodexClient
from bemine.discord.dispatch import resolve_channel_binding, ChannelBinding


def test_runtime_factory_maps_runtime_to_client(tmp_path):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    assert isinstance(build_client_for_runtime(settings, "kimi"), AnthropicClient)
    assert isinstance(build_client_for_runtime(settings, "claude"), AnthropicClient)
    assert isinstance(build_client_for_runtime(settings, "codex"), CodexClient)


class _FakeChannel:
    def __init__(self, name="", category=None):
        self.name = name
        self.id = 123
        self.category = category


class _FakeMessage:
    def __init__(self, channel, guild=object()):
        self.channel = channel
        self.guild = guild


def test_resolve_channel_binding_is_deterministic_with_fallback(tmp_path):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    msg = _FakeMessage(_FakeChannel(name="unmapped-channel"))
    binding = resolve_channel_binding(settings, msg)
    assert isinstance(binding, ChannelBinding)
    assert binding.runtime  # a concrete runtime, never empty
    assert binding.project_id  # falls back to a default project id
    assert isinstance(binding.cwd, Path)
    assert isinstance(binding.system, str)
