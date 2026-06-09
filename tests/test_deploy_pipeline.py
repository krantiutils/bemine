from bemine.config import BemineSettings
from bemine.deploy.state import DeployState, state_path
from bemine.deploy import pipeline as P


def _settings(tmp_path):
    return BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")


class _Clock:
    def __init__(self):
        self.t = 1000.0
    def now(self):
        return self.t
    def sleep(self, s):
        self.t += s


def _deps(*, clock, tests_ok=True, head="newsha", clean=True, hb_script=None):
    """hb_script: dict service -> list of heartbeat dicts returned successively."""
    calls = {"restart": [], "reset": [], "echo": [], "tests": 0}
    hb_script = hb_script or {}
    hb_idx = {k: 0 for k in hb_script}

    def run_tests():
        calls["tests"] += 1
        ok = tests_ok if isinstance(tests_ok, bool) else tests_ok[min(calls["tests"] - 1, len(tests_ok) - 1)]
        return ok, "test output tail"

    def read_hb(service):
        seq = hb_script.get(service)
        if not seq:
            return None
        i = min(hb_idx[service], len(seq) - 1)
        hb_idx[service] += 1
        return seq[i]

    deps = P.DeployDeps(
        run_tests=run_tests,
        restart=lambda svc: calls["restart"].append(svc),
        git_reset_hard=lambda sha: calls["reset"].append(sha),
        read_hb=read_hb,
        now=clock.now,
        sleep=clock.sleep,
        echo=lambda c, t: calls["echo"].append(t),
        head_sha=lambda: head,
        is_clean=lambda: clean,
    )
    return deps, calls


def _fresh(sha, ts):
    return {"service": "x", "sha": sha, "pid": 1, "ts": ts}


def test_request_deploy_sets_requested(tmp_path):
    s = _settings(tmp_path)
    ok, _ = P.request_deploy(s, channel_id="c1", requested_by="u")
    assert ok is True
    assert DeployState.load(state_path(s)).phase == "requested"
    ok2, msg = P.request_deploy(s, channel_id="c1")
    assert ok2 is False


def test_run_deploy_aborts_on_dirty_tree(tmp_path):
    s = _settings(tmp_path)
    deps, calls = _deps(clock=_Clock(), clean=False)
    st = P.run_deploy(s, channel_id="c1", deps=deps)
    assert st.phase == "failed"
    assert calls["restart"] == []
    assert any("dirty" in t.lower() for t in calls["echo"])


def test_run_deploy_aborts_on_red_tests_after_retry(tmp_path):
    s = _settings(tmp_path)
    deps, calls = _deps(clock=_Clock(), tests_ok=False)
    st = P.run_deploy(s, channel_id="c1", deps=deps)
    assert st.phase == "failed"
    assert calls["tests"] == 2
    assert calls["restart"] == []


def test_run_deploy_cli_happy_path_confirms(tmp_path):
    s = _settings(tmp_path)
    clock = _Clock()
    deps, calls = _deps(clock=clock, head="newsha", hb_script={
        "gateway": [_fresh("oldsha", clock.t), _fresh("newsha", clock.t)],
        "daemon": [_fresh("newsha", clock.t)],
    })
    st = P.run_deploy(s, channel_id="c1", deps=deps, self_restart_daemon=False)
    assert st.phase == "confirmed"
    assert calls["restart"] == ["gateway", "daemon"]
    assert calls["reset"] == []


def test_run_deploy_rolls_back_when_gateway_unhealthy(tmp_path):
    s = _settings(tmp_path)
    clock = _Clock()
    deps, calls = _deps(clock=clock, head="newsha", hb_script={
        "gateway": [_fresh("oldsha", clock.t)],
    })
    st = P.run_deploy(s, channel_id="c1", deps=deps, self_restart_daemon=False)
    assert st.phase == "rolled_back"
    assert calls["reset"] == ["oldsha"]
    assert calls["restart"].count("gateway") == 2
    assert "daemon" not in calls["restart"]


def test_run_deploy_daemon_executor_ends_at_daemon_refresh(tmp_path):
    s = _settings(tmp_path)
    clock = _Clock()
    deps, calls = _deps(clock=clock, head="newsha", hb_script={
        "gateway": [_fresh("oldsha", clock.t), _fresh("newsha", clock.t)],
    })
    st = P.run_deploy(s, channel_id="c1", deps=deps, self_restart_daemon=True)
    assert st.phase == "daemon_refresh"
    assert calls["restart"] == ["gateway", "daemon"]
    assert DeployState.load(state_path(s)).phase == "daemon_refresh"
