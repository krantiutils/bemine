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
    # I1: a blank [<runtime>].model must resolve to a real model id, never the
    # runtime name or device id, so the SDK is never sent a bogus model -> 404.
    assert binding.model  # never empty
    assert binding.model != binding.runtime
    assert binding.model != settings.device_id
    assert binding.project_id  # falls back to a default project id
    assert isinstance(binding.cwd, Path)
    assert isinstance(binding.system, str)


def test_named_guild_channel_binds_to_its_project_even_without_local_checkout(tmp_path):
    # A project that lives on another device has a Discord channel named after it but
    # no local checkout / project mapping. The binding must still carry that project
    # identity so the agent knows which project/session this channel controls.
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    msg = _FakeMessage(_FakeChannel(name="rai-speech-toolkit"))
    binding = resolve_channel_binding(settings, msg)
    # The channel is bound to its own project, NOT the generic coordination surface.
    assert binding.project_id != "bemine"
    assert "rai-speech-toolkit" in binding.project_id
    # The system prompt tells the agent this channel IS that project and to never ask
    # the user which project/session they mean.
    assert "rai-speech-toolkit" in binding.system
    assert "never ask" in binding.system.lower()


def test_local_project_channel_uses_real_cwd_and_local_relay(tmp_path):
    # A channel named after a project that HAS a local checkout binds to that project's
    # real cwd and is told to relay via local tmux — not the cross-device path.
    import json

    state_dir = tmp_path / ".bemine"
    state_dir.mkdir(parents=True)
    proj_dir = tmp_path / "checkout"
    proj_dir.mkdir()
    (state_dir / "discord-projects.json").write_text(
        json.dumps({"my-local-proj": {"cwd": str(proj_dir), "project_id": "p_local"}}),
        encoding="utf-8",
    )
    settings = BemineSettings.load(state_dir=state_dir, vault_dir=tmp_path / "v")
    msg = _FakeMessage(_FakeChannel(name="my-local-proj"))
    binding = resolve_channel_binding(settings, msg)
    assert binding.cwd == proj_dir
    assert "my-local-proj" in binding.system
    # Local checkout -> local tmux relay guidance, not the "no checkout on this device" path.
    assert "no checkout on this device" not in binding.system


def test_project_channel_prompt_scopes_status_to_project_session(tmp_path):
    # "status" / "it" / "its" in a project channel must mean the project's session,
    # not Bemine's own runtime config (the bemine_config tool).
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    msg = _FakeMessage(_FakeChannel(name="magar-toolkit"))
    binding = resolve_channel_binding(settings, msg)
    system = binding.system.lower()
    # The prompt must bind this channel to the project and steer a bare "status"
    # toward the project's session rather than Bemine's own runtime config.
    assert "magar-toolkit" in system
    assert "dedicated to the project" in system
    assert "not your own runtime" in system


def test_reserved_channels_are_not_treated_as_projects(tmp_path):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    for reserved in ("session-updates", "general"):
        msg = _FakeMessage(_FakeChannel(name=reserved))
        binding = resolve_channel_binding(settings, msg)
        assert binding.project_id == "bemine", reserved
        assert "dedicated to the project" not in binding.system, reserved


def test_dm_is_not_treated_as_a_project(tmp_path):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    msg = _FakeMessage(_FakeChannel(name="whatever"), guild=None)
    binding = resolve_channel_binding(settings, msg)
    assert binding.project_id == "bemine"
    assert "dedicated to the project" not in binding.system


def test_system_prompt_forbids_handing_user_commands(tmp_path):
    # The worker is an operator: it must run commands itself, never tell the user to run them.
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    msg = _FakeMessage(_FakeChannel(name="bemine"))
    binding = resolve_channel_binding(settings, msg)
    low = binding.system.lower()
    assert "never reply with commands for the user to run" in low
    assert "agents.md" in low
