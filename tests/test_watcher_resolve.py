from bemine.index.status import SessionStatus
from bemine.jobs.project_watcher import resolve_project_session, resolve_pane


def _status(cwd, age, device="mbp"):
    return SessionStatus(
        device_id=device, runtime="claude", session_id="s", cwd=cwd,
        source_path="", last_interaction_at=1000 - age, age_seconds=age,
        last_role="assistant", asks_user=True, ask_summary="", latest_summary="",
    )


def test_resolve_project_session_matches_by_cwd_basename_most_recent():
    statuses = [
        _status("/Users/cdjk/github/llm/rai-speech-toolkit", age=500),
        _status("/Users/cdjk/github/llm/rai-speech-toolkit", age=30),
        _status("/Users/cdjk/other", age=10),
    ]
    found = resolve_project_session(statuses, "rai-speech-toolkit")
    assert found is not None
    assert found.age_seconds == 30  # most recent of the matching ones


def test_resolve_project_session_none_when_no_match():
    assert resolve_project_session([_status("/x/y/z", age=1)], "nope") is None


class _FakeTmux:
    def __init__(self, panes):
        self._panes = panes
    def list_panes(self, device):
        return self._panes


def test_resolve_pane_matches_path_and_agent_command():
    tmux = _FakeTmux([
        {"pane_id": "%1", "path": "/home/cdjk", "command": "bash"},
        {"pane_id": "%2", "path": "/Users/cdjk/github/llm/rai-speech-toolkit", "command": "claude"},
    ])
    assert resolve_pane(tmux, "mbp", "/Users/cdjk/github/llm/rai-speech-toolkit") == "%2"


def test_resolve_pane_none_when_no_path_match():
    tmux = _FakeTmux([{"pane_id": "%1", "path": "/home/cdjk", "command": "bash"}])
    assert resolve_pane(tmux, "mbp", "/Users/cdjk/github/llm/rai-speech-toolkit") is None
