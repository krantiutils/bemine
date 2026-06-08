import subprocess
from bemine.jobs.remote_tmux import RemoteTmux


class _FakeRunner:
    def __init__(self, stdout=""):
        self.calls = []
        self._stdout = stdout

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=self._stdout, stderr="")


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
    assert runner.calls[-1] == ["ssh", "mbp", "tmux", "send-keys", "-t", "%2", "Enter"]


def test_send_text_is_literal_then_enter():
    runner = _FakeRunner()
    rt = RemoteTmux(gateway_device="cdjk", runner=runner)
    rt.send_text("mbp", "%2", "hello world")
    assert runner.calls[-2] == ["ssh", "mbp", "tmux", "send-keys", "-t", "%2", "-l", "hello world"]
    assert runner.calls[-1] == ["ssh", "mbp", "tmux", "send-keys", "-t", "%2", "Enter"]


def test_list_panes_parses_tab_separated_output():
    out = "%1\t/Users/cdjk/github/llm/rai-speech-toolkit\tclaude\n%2\t/home/cdjk\tbash\n"
    runner = _FakeRunner(stdout=out)
    rt = RemoteTmux(gateway_device="cdjk", runner=runner)
    panes = rt.list_panes("mbp")
    assert panes[0] == {"pane_id": "%1",
                        "path": "/Users/cdjk/github/llm/rai-speech-toolkit",
                        "command": "claude"}
    assert runner.calls[-1][:3] == ["ssh", "mbp", "tmux"]
    assert "list-panes" in runner.calls[-1]


def test_capture_returns_stdout():
    runner = _FakeRunner(stdout="pane contents\n")
    rt = RemoteTmux(gateway_device="cdjk", runner=runner)
    assert rt.capture("cdjk", "%1") == "pane contents\n"
    assert runner.calls[-1] == ["tmux", "capture-pane", "-p", "-t", "%1"]
