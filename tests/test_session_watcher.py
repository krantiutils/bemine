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


def test_check_sessions_detects_needs_input_then_finished(tmp_path, monkeypatch):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    state_path = tmp_path / "state.json"

    # Round 1: a session that is asking for input -> a "needs_input" event.
    monkeypatch.setattr(sw, "collect_fleet_status",
                        lambda *a, **k: ([_status("s1", asks_user=True, ask_summary="approve?")], []))
    events = sw.check_sessions(settings, state_path)
    assert any(e["type"] == "needs_input" and e["session_id"] == "s1" for e in events)

    # Round 2: same asking session present again -> no NEW needs_input event (already known).
    events2 = sw.check_sessions(settings, state_path)
    assert not any(e["type"] == "needs_input" for e in events2)

    # Round 3: session gone -> a "finished" event.
    monkeypatch.setattr(sw, "collect_fleet_status", lambda *a, **k: ([], []))
    events3 = sw.check_sessions(settings, state_path)
    assert any(e["type"] == "finished" and e["session_id"] == "s1" for e in events3)


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
