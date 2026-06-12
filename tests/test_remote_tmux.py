import shlex
import subprocess

from bemine.jobs.remote_tmux import RemoteTmux


class _FakeRunner:
    def __init__(self, stdout=""):
        self.calls = []
        self._stdout = stdout

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=self._stdout, stderr="")


def _remote_cmd(tmux_args):
    return "zsh -lic " + shlex.quote(shlex.join(["tmux", *tmux_args]))


def test_local_device_runs_tmux_directly():
    runner = _FakeRunner()
    rt = RemoteTmux(gateway_device="cdjk", runner=runner, pause=lambda s: None)
    rt.send_keys("cdjk", "%1", ["Down", "Enter"])
    assert runner.calls[-2][:1] == ["tmux"]
    assert runner.calls[-2] == ["tmux", "send-keys", "-t", "%1", "Down", "Enter"]
    assert runner.calls[-1] == ["tmux", "send-keys", "-t", "%1", "Enter"]


def test_remote_device_wraps_in_ssh():
    runner = _FakeRunner()
    rt = RemoteTmux(gateway_device="cdjk", runner=runner)
    rt.send_keys("mbp", "%2", ["Enter"])
    argv = runner.calls[-1]
    assert argv[0] == "ssh"
    assert "mbp" in argv
    assert argv[-1] == _remote_cmd(["send-keys", "-t", "%2", "Enter"])


def test_send_text_is_literal_then_enter_then_idempotent_enter():
    runner = _FakeRunner()
    pauses = []
    rt = RemoteTmux(gateway_device="cdjk", runner=runner, pause=pauses.append)
    rt.send_text("mbp", "%2", "hello world")
    # text, Enter, then a second Enter: the TUI can eat the first one
    # mid-redraw (an overnight answer sat unsubmitted in the input box).
    # A duplicate Enter on empty input is a no-op.
    assert runner.calls[-3][-1] == _remote_cmd(
        ["send-keys", "-t", "%2", "-l", "hello world"]
    )
    assert runner.calls[-2][-1] == _remote_cmd(["send-keys", "-t", "%2", "Enter"])
    assert runner.calls[-1][-1] == _remote_cmd(["send-keys", "-t", "%2", "Enter"])
    assert len(pauses) >= 2 and pauses[0] >= 0.5


def test_send_keys_ending_in_enter_gets_idempotent_second_enter():
    runner = _FakeRunner()
    rt = RemoteTmux(gateway_device="cdjk", runner=runner, pause=lambda s: None)
    rt.send_keys("mbp", "%2", ["Up", "Enter"])
    assert runner.calls[-2][-1] == _remote_cmd(["send-keys", "-t", "%2", "Up", "Enter"])
    assert runner.calls[-1][-1] == _remote_cmd(["send-keys", "-t", "%2", "Enter"])


def test_send_keys_without_enter_is_unchanged():
    runner = _FakeRunner()
    rt = RemoteTmux(gateway_device="cdjk", runner=runner, pause=lambda s: None)
    rt.send_keys("mbp", "%2", ["Down"])
    assert runner.calls[-1][-1] == _remote_cmd(["send-keys", "-t", "%2", "Down"])


def test_list_panes_parses_with_activity():
    out = (
        "%1\t/Users/cdjk/github/llm/rai-speech-toolkit\tclaude\t1780000000\t1\n"
        "%2\t/home/cdjk\tbash\t1780000050\t0\n"
    )
    runner = _FakeRunner(stdout=out)
    rt = RemoteTmux(gateway_device="cdjk", runner=runner)
    panes = rt.list_panes("mbp")
    assert panes[0] == {
        "pane_id": "%1",
        "path": "/Users/cdjk/github/llm/rai-speech-toolkit",
        "command": "claude",
        "activity": 1780000000,
        "attached": 1,
    }
    assert panes[1]["attached"] == 0
    argv = runner.calls[-1]
    assert argv[0] == "ssh" and "mbp" in argv
    assert "list-panes" in argv[-1]
    assert "window_activity" in argv[-1]
    assert "session_attached" in argv[-1]


def test_list_panes_tolerates_missing_activity():
    # Older tmux may not emit window_activity -> activity defaults to 0, line still parsed.
    out = "%1\t/home/cdjk\tbash\t\t\n"
    runner = _FakeRunner(stdout=out)
    rt = RemoteTmux(gateway_device="cdjk", runner=runner)
    panes = rt.list_panes("cdjk")
    assert panes[0]["activity"] == 0
    assert panes[0]["attached"] == 0


def test_local_device_still_runs_tmux_directly():
    runner = _FakeRunner()
    rt = RemoteTmux(gateway_device="cdjk", runner=runner, pause=lambda s: None)
    rt.send_keys("cdjk", "%1", ["Down", "Enter"])
    assert runner.calls[-2] == ["tmux", "send-keys", "-t", "%1", "Down", "Enter"]
    assert runner.calls[-1] == ["tmux", "send-keys", "-t", "%1", "Enter"]


def test_capture_returns_stdout():
    runner = _FakeRunner(stdout="pane contents\n")
    rt = RemoteTmux(gateway_device="cdjk", runner=runner)
    assert rt.capture("cdjk", "%1") == "pane contents\n"
    argv = runner.calls[-1]
    assert argv[:3] == ["tmux", "capture-pane", "-p"]
    assert "-S" in argv
    assert "%1" in argv


def test_capture_reads_scrollback():
    runner = _FakeRunner(stdout="pane text")
    rt = RemoteTmux(gateway_device="cdjk", runner=runner)
    rt.capture("cdjk", "%1")
    argv = runner.calls[-1]
    # local device -> direct tmux; must request scrollback with -S
    assert argv[:3] == ["tmux", "capture-pane", "-p"]
    assert "-S" in argv


def test_send_text_pauses_between_text_and_enter():
    pauses = []
    runner = _FakeRunner()
    rt = RemoteTmux(
        gateway_device="cdjk", runner=runner, pause=lambda s: pauses.append(s)
    )
    rt.send_text("cdjk", "%1", "hello")
    assert runner.calls[-3] == ["tmux", "send-keys", "-t", "%1", "-l", "hello"]
    assert runner.calls[-2] == ["tmux", "send-keys", "-t", "%1", "Enter"]
    assert runner.calls[-1] == ["tmux", "send-keys", "-t", "%1", "Enter"]
    assert pauses and pauses[0] > 0
