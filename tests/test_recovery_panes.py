from bemine.jobs.auto_recovery import _is_agent_pane


def test_claude_version_string_command_is_agent():
    # Claude Code reports its version as the pane command — must be treated as an agent.
    assert _is_agent_pane("2.1.168") is True
    assert _is_agent_pane("2.1.161") is True


def test_named_agent_commands_are_agent():
    for cmd in ("claude", "codex-aarch64-a", "kimi", "node", "python3.13"):
        assert _is_agent_pane(cmd) is True, cmd


def test_shells_and_tools_are_not_agent():
    for cmd in ("bash", "-bash", "zsh", "-zsh", "sh", "fish", "tmux", "vim", "nvim",
                "less", "man", "ssh", "tail", "git", "fzf", "htop"):
        assert _is_agent_pane(cmd) is False, cmd


def test_empty_command_is_not_agent():
    assert _is_agent_pane("") is False
    assert _is_agent_pane(None) is False
