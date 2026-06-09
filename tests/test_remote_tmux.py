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
    rt = RemoteTmux(gateway_device="cdjk", runner=runner)
    rt.send_keys("cdjk", "%1", ["Down", "Enter"])
    assert runner.calls[-1][:1] == ["tmux"]
    assert runner.calls[-1] == ["tmux", "send-keys", "-t", "%1", "Down", "Enter"]


def test_remote_device_wraps_in_ssh():
    runner = _FakeRunner()
    rt = RemoteTmux(gateway_device="cdjk", runner=runner)
    rt.send_keys("mbp", "%2", ["Enter"])
    argv = runner.calls[-1]
    assert argv[0] == "ssh"
    assert "mbp" in argv
    assert argv[-1] == _remote_cmd(["send-keys", "-t", "%2", "Enter"])


def test_send_text_is_literal_then_enter():
    runner = _FakeRunner()
    rt = RemoteTmux(gateway_device="cdjk", runner=runner)
    rt.send_text("mbp", "%2", "hello world")
    assert runner.calls[-2][-1] == _remote_cmd(["send-keys", "-t", "%2", "-l", "hello world"])
    assert runner.calls[-1][-1] == _remote_cmd(["send-keys", "-t", "%2", "Enter"])


def test_list_panes_parses_tab_separated_output():
    out = "%1\t/Users/cdjk/github/llm/rai-speech-toolkit\tclaude\n%2\t/home/cdjk\tbash\n"
    runner = _FakeRunner(stdout=out)
    rt = RemoteTmux(gateway_device="cdjk", runner=runner)
    panes = rt.list_panes("mbp")
    assert panes[0] == {"pane_id": "%1",
                        "path": "/Users/cdjk/github/llm/rai-speech-toolkit",
                        "command": "claude"}
    argv = runner.calls[-1]
    assert argv[0] == "ssh" and "mbp" in argv
    assert "list-panes" in argv[-1]  # tmux command is inside the quoted remote string


def test_local_device_still_runs_tmux_directly():
    runner = _FakeRunner()
    rt = RemoteTmux(gateway_device="cdjk", runner=runner)
    rt.send_keys("cdjk", "%1", ["Down", "Enter"])
    assert runner.calls[-1] == ["tmux", "send-keys", "-t", "%1", "Down", "Enter"]


def test_capture_returns_stdout():
    runner = _FakeRunner(stdout="pane contents\n")
    rt = RemoteTmux(gateway_device="cdjk", runner=runner)
    assert rt.capture("cdjk", "%1") == "pane contents\n"
    assert runner.calls[-1] == ["tmux", "capture-pane", "-p", "-t", "%1"]


def test_send_text_pauses_between_text_and_enter():
    pauses = []
    runner = _FakeRunner()
    rt = RemoteTmux(gateway_device="cdjk", runner=runner, pause=lambda s: pauses.append(s))
    rt.send_text("cdjk", "%1", "hello")
    assert runner.calls[-2] == ["tmux", "send-keys", "-t", "%1", "-l", "hello"]
    assert runner.calls[-1] == ["tmux", "send-keys", "-t", "%1", "Enter"]
    assert pauses and pauses[0] > 0
