from pathlib import Path

from bemine.config import BemineSettings
from bemine.index.status import SessionStatus
import bemine.jobs.session_watcher as sw


def _status(session_id, *, asks_user=False, ask_summary=""):
    return SessionStatus(
        device_id="local", runtime="codex", session_id=session_id, cwd="/home/cdjk/bm",
        source_path="", last_interaction_at=0.0, age_seconds=5, last_role="assistant",
        asks_user=asks_user, ask_summary=ask_summary, latest_summary="", model="", branch="",
    )


def test_check_sessions_needs_input_dedup_and_no_disappear_event(tmp_path, monkeypatch):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    state_path = tmp_path / "state.json"

    # Round 1: a session that is asking for input -> a "needs_input" event.
    monkeypatch.setattr(sw, "collect_fleet_status",
                        lambda *a, **k: ([_status("s1", asks_user=True, ask_summary="approve?")], []))
    events = sw.check_sessions(settings, state_path)
    assert any(e["type"] == "needs_input" and e["session_id"] == "s1" for e in events)

    # Round 2: same asking session present again -> no NEW needs_input event (notify once).
    events2 = sw.check_sessions(settings, state_path)
    assert not any(e["type"] == "needs_input" for e in events2)

    # Round 3: session gone -> NO event (a session aging out is not "finished processing").
    monkeypatch.setattr(sw, "collect_fleet_status", lambda *a, **k: ([], []))
    assert sw.check_sessions(settings, state_path) == []


def test_stale_sessions_are_ignored(tmp_path, monkeypatch):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    state_path = tmp_path / "state.json"
    # A session whose last activity is way past the recent window (e.g. days ago) is ignored
    # entirely — it never enters state, so it can never produce a notification.
    monkeypatch.setattr(sw, "collect_fleet_status", lambda *a, **k: ([_status_age("ancient", 400000)], []))
    assert sw.check_sessions(settings, state_path) == []
    import json as _json
    assert _json.loads(state_path.read_text())["known_sessions"] == {}


def test_resolve_channel_id_by_basename():
    from bemine.jobs.session_watcher import _resolve_channel_id
    name_to_id = {"vertex-core": "111", "neptts-bench": "222"}
    # remote-style cwd with no local project mapping -> matched by basename
    assert _resolve_channel_id("/Users/cdjk/github/alpen/vertex-core", {}, name_to_id) == "111"
    # underscore normalized to hyphen
    assert _resolve_channel_id("/x/neptts_bench", {}, name_to_id) == "222"
    # no matching channel -> None (DM fallback)
    assert _resolve_channel_id("/x/unknown-proj", {}, name_to_id) is None


def test_render_events_formats_and_empty():
    assert sw.render_events([]) == ""
    text = sw.render_events([
        {"type": "needs_input", "device_id": "local", "session_id": "s1",
         "cwd": "/x", "ask_summary": "approve?", "age_seconds": 1},
        {"type": "finished", "device_id": "local", "session_id": "s2", "cwd": "/y", "age_seconds": 9},
    ])
    assert "Needs input" in text and "Finished" in text and "s1" in text and "s2" in text


def test_session_watcher_job_empty_output_does_not_crash_notify(tmp_path, monkeypatch):
    """Regression: a notify_discord job with empty output (no events) must not crash on
    an unbound `sent` (UnboundLocalError) — it should succeed and not notify."""
    from bemine.db import BemineDB
    from bemine.kernel import BemineKernel
    from bemine.jobs.core import run_job_by_id, seed_default_jobs

    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    monkeypatch.setattr(sw, "collect_fleet_status", lambda *a, **k: ([], []))  # no events
    kernel = BemineKernel(settings)
    seed_default_jobs(kernel.db, settings=settings)

    sent_calls = []
    result = run_job_by_id(settings, "session_watcher",
                           sender=lambda text: sent_calls.append(text) or {"id": "x"})
    assert result.status == "success"
    assert result.notified is False
    assert sent_calls == []  # nothing posted for empty output


def test_match_channel_name_routes_by_cwd():
    from bemine.jobs.session_watcher import _match_channel_name
    cwd_map = {
        "/home/cdjk/github/neptts-bench": "neptts-bench",
        "/home/cdjk/github": "github",
        "/home/cdjk/bm": "bemine",
    }
    assert _match_channel_name("/home/cdjk/github/neptts-bench", cwd_map) == "neptts-bench"
    # subdir picks the longest-prefix project
    assert _match_channel_name("/home/cdjk/github/neptts-bench/src", cwd_map) == "neptts-bench"
    assert _match_channel_name("/home/cdjk/github/other-thing", cwd_map) == "github"
    assert _match_channel_name("/tmp/elsewhere", cwd_map) is None
    assert _match_channel_name("", cwd_map) is None


def test_deliver_events_empty_is_noop():
    from bemine.jobs.session_watcher import deliver_events
    from bemine.config import BemineSettings
    assert deliver_events(BemineSettings.load(), []) == ""


def _status_age(session_id, age, *, asks_user=False):
    return SessionStatus(
        device_id="local", runtime="codex", session_id=session_id, cwd="/home/cdjk/bm",
        source_path="", last_interaction_at=0.0, age_seconds=age, last_role="assistant",
        asks_user=asks_user, ask_summary="", latest_summary="did the thing", model="", branch="")


def test_done_fires_when_active_session_goes_idle(tmp_path, monkeypatch):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    state_path = tmp_path / "state.json"
    # Round 1: ACTIVE (age below the 300s idle threshold) -> observed active, no done yet
    monkeypatch.setattr(sw, "collect_fleet_status", lambda *a, **k: ([_status_age("sx", 5)], []))
    assert not any(e["type"] == "done" for e in sw.check_sessions(settings, state_path))
    # Round 2: now IDLE (age past threshold) -> fires exactly one "done"
    monkeypatch.setattr(sw, "collect_fleet_status", lambda *a, **k: ([_status_age("sx", 600)], []))
    events = sw.check_sessions(settings, state_path)
    assert sum(1 for e in events if e["type"] == "done" and e["session_id"] == "sx") == 1
    # Round 3: still idle -> no duplicate
    assert not any(e["type"] == "done" for e in sw.check_sessions(settings, state_path))


def test_recent_idle_session_notifies_on_first_sight(tmp_path, monkeypatch):
    # THE bug fix: a recently-active session found already idle (e.g. the watcher only
    # started seeing it after it stopped) MUST still notify — no "was it active first" gate.
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(sw, "collect_fleet_status", lambda *a, **k: ([_status_age("idle1", 600)], []))
    events = sw.check_sessions(settings, state_path)
    assert sum(1 for e in events if e["type"] == "done" and e["session_id"] == "idle1") == 1
    # And it does NOT re-notify on the next poll while it stays idle.
    assert not any(e["type"] == "done" for e in sw.check_sessions(settings, state_path))


def test_stale_idle_session_does_not_notify(tmp_path, monkeypatch):
    # Past the recency window (e.g. days idle) -> ignored entirely, no notification.
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(sw, "collect_fleet_status", lambda *a, **k: ([_status_age("old", 400000)], []))
    assert not any(e["type"] == "done" for e in sw.check_sessions(settings, state_path))


def test_channel_name_and_project_cwd_guards():
    from bemine.jobs.session_watcher import _channel_name_for, _is_project_cwd
    assert _channel_name_for("/home/cdjk/github/alpen/vertex-core") == "vertex-core"
    assert _channel_name_for("/x/Neptts_Bench") == "neptts-bench"
    # junk / non-project dirs are not given channels
    assert _is_project_cwd("/home/cdjk/github/neptts-bench") is True
    assert _is_project_cwd("/tmp/whatever") is False
    assert _is_project_cwd("/") is False
    assert _is_project_cwd("/x/src") is False


def test_undelivered_event_is_rearmed_for_retry(tmp_path):
    import json
    from bemine.jobs.session_watcher import _rearm_undelivered, WatcherState
    state_path = tmp_path / "state.json"
    key = json.dumps({"d": "mbp", "r": "claude", "s": "sX"}, sort_keys=True)
    WatcherState(known_sessions={key: {"notified_done": True, "asks_user": True}},
                 last_check=0).save(state_path)
    # A "done" event that failed delivery must reset notified_done so the next poll retries.
    _rearm_undelivered(state_path, [{"type": "done", "device_id": "mbp",
                                     "runtime": "claude", "session_id": "sX"}])
    saved = json.loads(state_path.read_text())["known_sessions"][key]
    assert saved["notified_done"] is False
