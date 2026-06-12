import subprocess
from bemine.config import BemineSettings
from bemine.deploy.heartbeat import (
    write_heartbeat, read_heartbeat, heartbeat_path, current_head_sha,
    boot_sha_resolver,
)


def _settings(tmp_path):
    return BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")


def test_write_then_read_roundtrip(tmp_path):
    s = _settings(tmp_path)
    write_heartbeat(s, "gateway", "abc123")
    hb = read_heartbeat(s, "gateway")
    assert hb["service"] == "gateway"
    assert hb["sha"] == "abc123"
    assert isinstance(hb["pid"], int)
    assert isinstance(hb["ts"], (int, float))


def test_read_missing_returns_none(tmp_path):
    assert read_heartbeat(_settings(tmp_path), "daemon") is None


def test_heartbeat_path_under_state_dir(tmp_path):
    s = _settings(tmp_path)
    p = heartbeat_path(s, "gateway")
    assert p.name == "heartbeat-gateway.json"
    assert p.parent == s.state_dir / "deploy"


def test_current_head_sha_uses_runner():
    def fake_runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="deadbeef\n", stderr="")
    assert current_head_sha(runner=fake_runner) == "deadbeef"


def test_boot_sha_resolver_sticks_to_first_success_even_if_head_moves():
    shas = iter(["aaa\n", "bbb\n"])

    def fake_runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=next(shas), stderr="")

    resolve = boot_sha_resolver(runner=fake_runner)
    assert resolve() == "aaa"
    # HEAD moved (a deploy landed); the running process must keep reporting
    # the sha it booted on, not start advertising code it never imported.
    assert resolve() == "aaa"


def test_boot_sha_resolver_retries_until_git_answers():
    answers = iter(["", "", "ccc\n"])

    def fake_runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout=next(answers), stderr="x")

    resolve = boot_sha_resolver(runner=fake_runner)
    assert resolve() == ""
    assert resolve() == ""
    assert resolve() == "ccc"
    assert resolve() == "ccc"  # iterator exhausted: re-running git would raise
