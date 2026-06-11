from bemine.config import BemineSettings
from bemine.jobs.auto_recovery import (
    tick_recovery,
    RecoveryDeps,
    load_recovery_state,
    recovery_state_path,
)


def _settings(tmp_path):
    return BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")


def _pane(
    *,
    activity=0,
    command="claude",
    pane_id="%2",
    path="/Users/cdjk/github/llm/rai-speech-toolkit",
    device="mbp",
):
    # activity=0 -> staleness check skipped (fail-open), good for deterministic tests
    return {
        "device": device,
        "pane_id": pane_id,
        "path": path,
        "command": command,
        "activity": activity,
    }


class _Tmux:
    """Returns a scripted sequence of captures; records sends."""

    def __init__(self, captures):
        self._caps = list(captures)
        self.i = 0
        self.sent_keys = []
        self.sent_text = []

    def capture(self, device, pane, lines=200):
        v = self._caps[min(self.i, len(self._caps) - 1)]
        self.i += 1
        return v

    def send_keys(self, device, pane, keys):
        self.sent_keys.append(keys)

    def send_text(self, device, pane, text):
        self.sent_text.append(text)


class _Decision:
    def __init__(self, kind, text="", keys=None, question=""):
        self.kind = kind
        self.text = text
        self.keys = keys or []
        self.question = question


def _deps(panes, tmux, *, clock, escalations, audits, decide=None):
    return RecoveryDeps(
        get_panes=lambda s: panes,
        tmux=tmux,
        decide=decide,
        escalate=lambda e: escalations.append(e),
        now=clock["now"],
        sleep=lambda s: None,
        audit=lambda et, kind, pane: audits.append((et, kind)),
    )


def _clock(t=2000.0):
    box = {"t": t}
    return {"now": lambda: box["t"], "box": box}


def test_rate_limit_resubmits_with_up_enter(tmp_path):
    s = _settings(tmp_path)
    tmux = _Tmux(["API Error: Rate limited"])
    clock, esc, aud = _clock(), [], []
    deps = _deps([_pane()], tmux, clock=clock, escalations=esc, audits=aud)
    tick_recovery(s, deps, now=clock["box"]["t"])
    assert tmux.sent_keys == [["Up", "Enter"]]
    assert ("resubmit", "rate_limit") in aud


def test_rate_limit_recovered_on_later_clean_tick(tmp_path):
    s = _settings(tmp_path)
    # tick1: rate-limited -> resubmit (attempts=1). tick2 (past backoff): clean -> recovered.
    tmux = _Tmux(["Rate limited", "all good now"])
    clock, esc, aud = _clock(), [], []
    deps = _deps([_pane()], tmux, clock=clock, escalations=esc, audits=aud)
    tick_recovery(s, deps, now=2000.0)
    tick_recovery(s, deps, now=2000.0 + 100000)  # past backoff
    assert ("recovered", "rate_limit") in aud
    assert load_recovery_state(recovery_state_path(s)) == {}


def test_rate_limit_escalates_once_when_persistent(tmp_path):
    s = _settings(tmp_path)
    tmux = _Tmux(["Rate limited"])  # always rate-limited
    clock, esc, aud = _clock(), [], []
    deps = _deps([_pane()], tmux, clock=clock, escalations=esc, audits=aud)
    for i in range(8):
        tick_recovery(s, deps, now=2000.0 + i * 100000)
    assert len(esc) == 1  # escalate-once even though it stays rate-limited


def test_trivial_confirm_sends_yes(tmp_path):
    s = _settings(tmp_path)
    tmux = _Tmux(["Do you want to proceed? (y/n)"])
    clock, esc, aud = _clock(), [], []
    deps = _deps([_pane()], tmux, clock=clock, escalations=esc, audits=aud)
    tick_recovery(s, deps, now=2000.0)
    assert tmux.sent_text == ["yes"]


def test_llm_answers_stuck_question_when_idle(tmp_path):
    s = _settings(tmp_path)
    # Same screen both ticks (idle). Not rate-limit, not trivial -> LLM tier on the 2nd tick.
    q = "I found two configs. Which environment should I target?"
    tmux = _Tmux([q, q, q])
    clock, esc, aud = _clock(), [], []
    calls = {"n": 0}

    def decide(pane, before):
        calls["n"] += 1
        return _Decision("send_text", text="use staging")

    deps = _deps(
        [_pane()], tmux, clock=clock, escalations=esc, audits=aud, decide=decide
    )
    tick_recovery(s, deps, now=2000.0)  # 1st sighting: record hash, no LLM yet
    assert tmux.sent_text == []
    tick_recovery(s, deps, now=2001.0)  # same screen -> idle -> LLM answers
    assert tmux.sent_text == ["use staging"]
    assert calls["n"] == 1
    tick_recovery(s, deps, now=2002.0)  # same screen, already judged -> no repeat LLM
    assert calls["n"] == 1


def test_llm_not_called_when_screen_changing(tmp_path):
    s = _settings(tmp_path)
    tmux = _Tmux(["working line 1", "working line 2", "working line 3"])
    clock, esc, aud = _clock(), [], []
    calls = {"n": 0}

    def decide(pane, before):
        calls["n"] += 1
        return _Decision("wait")

    deps = _deps(
        [_pane()], tmux, clock=clock, escalations=esc, audits=aud, decide=decide
    )
    for i in range(3):
        tick_recovery(s, deps, now=2000.0 + i)
    assert calls["n"] == 0  # screen keeps changing -> treated as working, never LLM'd
    assert tmux.sent_text == []


def test_skips_stale_pane(tmp_path):
    s = _settings(tmp_path)
    tmux = _Tmux(["Rate limited"])
    clock, esc, aud = _clock(), [], []
    # activity far in past; now huge -> exceeds 4h (14400) window from repo bemine.toml
    deps = _deps([_pane(activity=1)], tmux, clock=clock, escalations=esc, audits=aud)
    tick_recovery(s, deps, now=10_000_000.0)
    assert tmux.sent_keys == [] and tmux.sent_text == []


def test_tick_isolates_failing_pane(tmp_path):
    s = _settings(tmp_path)

    class _Boom:
        def capture(self, d, p, lines=200):
            raise RuntimeError("ssh fail")

        def send_keys(self, d, p, k):
            pass

        def send_text(self, d, p, t):
            pass

    clock, esc, aud = _clock(), [], []
    deps = _deps([_pane()], _Boom(), clock=clock, escalations=esc, audits=aud)
    tick_recovery(s, deps, now=2000.0)  # must not raise
    assert load_recovery_state(recovery_state_path(s)) == {}


def test_no_resend_after_escalation(tmp_path):
    s = _settings(tmp_path)
    tmux = _Tmux(["Rate limited"])  # always rate-limited
    clock, esc, aud = _clock(), [], []
    deps = _deps([_pane()], tmux, clock=clock, escalations=esc, audits=aud)
    for i in range(12):
        tick_recovery(
            s, deps, now=2000.0 + i * 100000
        )  # each tick past the hold window
    assert len(esc) == 1  # escalated exactly once
    assert (
        len(tmux.sent_keys) <= 5
    )  # stops resending at max_attempts, doesn't grow to 12


def test_usage_limit_waits_until_reset_then_nudges_continue(tmp_path):
    import time as _t

    s = _settings(tmp_path)
    limit_pane = "You've hit your usage limit. Try again at 3:47am."
    tmux = _Tmux([limit_pane])
    clock, esc, aud = _clock(), [], []
    deps = _deps(
        [_pane(command="codex-aarch64-a")],
        tmux,
        clock=clock,
        escalations=esc,
        audits=aud,
    )
    # Tick at 01:00 local: must NOT send anything; must schedule the wait until ~03:47.
    now1 = _t.mktime((2026, 6, 11, 1, 0, 0, 0, 0, -1))
    tick_recovery(s, deps, now=now1)
    assert tmux.sent_text == [] and tmux.sent_keys == []
    assert ("limit_wait", "usage_limit") in aud
    st = load_recovery_state(recovery_state_path(s))
    rec = list(st.values())[0]
    reset = _t.localtime(float(rec["limit_wait_until"]))
    assert (reset.tm_hour, reset.tm_min) == (3, 47)
    # Tick after reset: nudge "continue" once.
    now2 = _t.mktime((2026, 6, 11, 3, 50, 0, 0, 0, -1))
    tick_recovery(s, deps, now=now2)
    assert tmux.sent_text == ["continue"]
    assert ("limit_continue", "usage_limit") in aud


def test_usage_limit_within_backoff_does_not_renudge(tmp_path):
    import time as _t

    s = _settings(tmp_path)
    tmux = _Tmux(["usage limit reached. try again at 3:47am"])
    clock, esc, aud = _clock(), [], []
    deps = _deps(
        [_pane(command="codex-aarch64-a")],
        tmux,
        clock=clock,
        escalations=esc,
        audits=aud,
    )
    t0 = _t.mktime((2026, 6, 11, 3, 50, 0, 0, 0, -1))
    tick_recovery(s, deps, now=t0)  # waits (parse -> tomorrow? no: stored wait)
    tick_recovery(s, deps, now=t0 + 60)  # still inside scheduled window -> no spam
    assert tmux.sent_text.count("continue") <= 1


def test_usage_limit_retries_continue_with_backoff_capped_10min(tmp_path):
    import time as _t

    s = _settings(tmp_path)
    # Pane shows the limit forever (nudge not taking) — must retry with doubling
    # backoff capped at 600s, not stall until tomorrow.
    tmux = _Tmux(["You've hit your usage limit. Try again at 3:47am."])
    clock, esc, aud = _clock(), [], []
    deps = _deps(
        [_pane(command="codex-aarch64-a")],
        tmux,
        clock=clock,
        escalations=esc,
        audits=aud,
    )
    t0 = _t.mktime((2026, 6, 11, 1, 0, 0, 0, 0, -1))
    tick_recovery(s, deps, now=t0)  # schedules wait until 03:47
    assert tmux.sent_text == []

    from bemine.jobs.auto_recovery import load_recovery_state, recovery_state_path

    reset = _t.mktime((2026, 6, 11, 3, 47, 0, 0, 0, -1))
    now = reset + 1
    gaps = []
    for _ in range(7):
        tick_recovery(s, deps, now=now)
        rec = list(load_recovery_state(recovery_state_path(s)).values())[0]
        gaps.append(rec["next_retry_at"] - now)
        now = float(rec["next_retry_at"]) + 1
    assert len(tmux.sent_text) == 7  # kept retrying
    assert all(g <= 600 for g in gaps)  # never beyond 10 min
    assert max(gaps) == 600  # reaches and caps at 10 min
    assert gaps[0] < gaps[-1]  # actually backs off


def test_failed_nudge_reparses_pane_for_later_reset(tmp_path):
    import time as _t

    s = _settings(tmp_path)
    # Session got limited at 03:36; pane states its own reset (8:36am). First sighting
    # before 03:47 fallback... here: nudge fails, pane STILL shows 'try again at 8:36am'
    # -> instead of blind 10-min retries for 5 hours, re-wait until 08:36.
    pane = "You've hit your usage limit. Try again at 8:36am."
    tmux = _Tmux([pane])
    clock, esc, aud = _clock(), [], []
    deps = _deps(
        [_pane(command="codex-aarch64-a")],
        tmux,
        clock=clock,
        escalations=esc,
        audits=aud,
    )
    t0 = _t.mktime((2026, 6, 11, 3, 0, 0, 0, 0, -1))
    tick_recovery(s, deps, now=t0)  # waits until 08:36 (parsed) — good
    from bemine.jobs.auto_recovery import load_recovery_state, recovery_state_path

    rec = list(load_recovery_state(recovery_state_path(s)).values())[0]
    reset = _t.localtime(float(rec["limit_wait_until"]))
    assert (reset.tm_hour, reset.tm_min) == (8, 36)

    # Simulate a MIS-SCHEDULED wait (e.g. fallback 03:47): force wait_until to the past,
    # nudge fires, pane unchanged and still states 8:36 -> must RE-WAIT to 8:36, not
    # hammer every <=10min.
    st = load_recovery_state(recovery_state_path(s))
    key = list(st.keys())[0]
    st[key]["limit_wait_until"] = t0 + 60  # pretend reset was 03:01
    st[key]["next_retry_at"] = 0
    from bemine.jobs.auto_recovery import save_recovery_state

    save_recovery_state(recovery_state_path(s), st)

    t1 = _t.mktime((2026, 6, 11, 3, 50, 0, 0, 0, -1))
    tick_recovery(s, deps, now=t1)  # nudges once (wait passed)
    assert tmux.sent_text == ["continue"]
    t2 = _t.mktime((2026, 6, 11, 3, 56, 0, 0, 0, -1))
    tick_recovery(s, deps, now=t2)  # pane still limited, states 8:36
    rec2 = list(load_recovery_state(recovery_state_path(s)).values())[0]
    reset2 = _t.localtime(float(rec2["limit_wait_until"]))
    assert (reset2.tm_hour, reset2.tm_min) == (8, 36)  # re-waited to the stated reset
    assert tmux.sent_text == ["continue"]  # no second blind nudge


def test_usage_limit_pane_idle_past_max_age_still_gets_continue(tmp_path):
    import time as _t

    s = _settings(tmp_path)
    tmux = _Tmux(["You've hit your usage limit. Try again at 3:47am."])
    clock, esc, aud = _clock(), [], []
    # limit hit at 01:00 with fresh activity -> wait scheduled for 03:47
    now1 = _t.mktime((2026, 6, 11, 1, 0, 0, 0, 0, -1))
    deps = _deps(
        [_pane(command="codex-aarch64-a", activity=int(now1))],
        tmux,
        clock=clock,
        escalations=esc,
        audits=aud,
    )
    tick_recovery(s, deps, now=now1)
    assert tmux.sent_text == []
    # 03:50: past the reset, but the pane has been idle > 4h (activity unchanged
    # since the limit hit at 01:00 the PREVIOUS day in this scenario). The age
    # gate must not strand a scheduled post-reset continue.
    now2 = now1 + 14400 + 3600  # 6h later: 07:00 next window, > max_age idle
    tick_recovery(s, deps, now=now2)
    assert tmux.sent_text == ["continue"]


def test_classify_approaching_limit_warning_is_not_usage_limit(tmp_path):
    from bemine.jobs.auto_recovery import classify, RATE_LIMIT_DEFAULTS

    for pane in [
        "Approaching 5-hour limit · resets 3pm",
        "Approaching usage limit · resets 11pm",
        "approaching weekly limit - resets Jun 17",
    ]:
        assert (
            classify(pane, asks_user=False, signatures=RATE_LIMIT_DEFAULTS) == "healthy"
        ), pane
    assert (
        classify(
            "5-hour limit reached ∙ resets 3am",
            asks_user=False,
            signatures=RATE_LIMIT_DEFAULTS,
        )
        == "usage_limit"
    )


def test_parse_reset_epoch_handles_current_cli_phrasings(tmp_path):
    import time as _t
    from bemine.jobs.auto_recovery import _parse_reset_epoch

    now = _t.mktime((2026, 6, 11, 9, 0, 0, 0, 0, -1))

    # claude 5h window: clock without minutes
    reset = _parse_reset_epoch("5-hour limit reached ∙ resets 3pm", now)
    assert reset is not None
    assert (_t.localtime(reset).tm_hour, _t.localtime(reset).tm_min) == (15, 0)

    # codex relative phrasing
    reset = _parse_reset_epoch(
        "You've hit your usage limit. Try again in 4 hours 58 minutes.", now
    )
    assert reset is not None
    assert abs(reset - (now + 4 * 3600 + 58 * 60)) < 120

    reset = _parse_reset_epoch("usage limit hit, try again in 30 minutes", now)
    assert reset is not None
    assert abs(reset - (now + 1800)) < 120

    # weekly limit: date + time
    reset = _parse_reset_epoch("Weekly limit reached · resets Jun 17, 10:59am", now)
    assert reset is not None
    lt = _t.localtime(reset)
    assert (lt.tm_mon, lt.tm_mday, lt.tm_hour, lt.tm_min) == (6, 17, 10, 59)

    # existing HH:MM clock keeps working
    reset = _parse_reset_epoch("try again at 3:47am", now)
    assert reset is not None
    assert (_t.localtime(reset).tm_hour, _t.localtime(reset).tm_min) == (3, 47)


def _attended_pane(*, activity, command="claude"):
    pane = _pane(activity=activity, command=command)
    pane["attached"] = 1
    return pane


def test_attended_active_pane_defers_trivial_confirm(tmp_path):
    s = _settings(tmp_path)
    tmux = _Tmux(["Do you want to proceed? (y/n)"])
    clock, esc, aud = _clock(), [], []
    now = 2000.0
    deps = _deps(
        [_attended_pane(activity=int(now) - 30)],
        tmux,
        clock=clock,
        escalations=esc,
        audits=aud,
    )
    tick_recovery(s, deps, now=now)
    # someone is at the keyboard: do not type into their session
    assert tmux.sent_text == [] and tmux.sent_keys == []


def test_attended_idle_pane_still_gets_trivial_confirm(tmp_path):
    s = _settings(tmp_path)
    tmux = _Tmux(["Do you want to proceed? (y/n)"])
    clock, esc, aud = _clock(), [], []
    now = 2000.0
    deps = _deps(
        [_attended_pane(activity=int(now) - 900)],
        tmux,
        clock=clock,
        escalations=esc,
        audits=aud,
    )
    tick_recovery(s, deps, now=now)
    assert tmux.sent_text == ["yes"]


def test_attended_active_pane_defers_rate_limit_resend(tmp_path):
    s = _settings(tmp_path)
    tmux = _Tmux(["Rate limited"])
    clock, esc, aud = _clock(), [], []
    now = 2000.0
    deps = _deps(
        [_attended_pane(activity=int(now) - 30)],
        tmux,
        clock=clock,
        escalations=esc,
        audits=aud,
    )
    tick_recovery(s, deps, now=now)
    assert tmux.sent_keys == []
    # but once the pane goes idle (user stepped away), the resend happens
    deps2 = _deps(
        [_attended_pane(activity=int(now) - 900)],
        tmux,
        clock=clock,
        escalations=esc,
        audits=aud,
    )
    tick_recovery(s, deps2, now=now + 400)
    assert tmux.sent_keys == [["Up", "Enter"]]


def test_attended_active_pane_defers_limit_continue_but_still_schedules_wait(tmp_path):
    import time as _t

    s = _settings(tmp_path)
    tmux = _Tmux(["You've hit your usage limit. Try again at 3:47am."])
    clock, esc, aud = _clock(), [], []
    now1 = _t.mktime((2026, 6, 12, 1, 0, 0, 0, 0, -1))
    deps = _deps(
        [_attended_pane(activity=int(now1) - 30, command="codex-aarch64-a")],
        tmux,
        clock=clock,
        escalations=esc,
        audits=aud,
    )
    tick_recovery(s, deps, now=now1)
    # scheduling the wait is keystroke-free and must still happen
    assert ("limit_wait", "usage_limit") in aud
    assert tmux.sent_text == []
    # past reset but user is typing -> defer the continue
    now2 = _t.mktime((2026, 6, 12, 3, 50, 0, 0, 0, -1))
    deps2 = _deps(
        [_attended_pane(activity=int(now2) - 30, command="codex-aarch64-a")],
        tmux,
        clock=clock,
        escalations=esc,
        audits=aud,
    )
    tick_recovery(s, deps2, now=now2)
    assert tmux.sent_text == []
    # user gone (idle past grace) -> continue goes out
    now3 = now2 + 600
    deps3 = _deps(
        [_attended_pane(activity=int(now2) - 30, command="codex-aarch64-a")],
        tmux,
        clock=clock,
        escalations=esc,
        audits=aud,
    )
    tick_recovery(s, deps3, now=now3)
    assert tmux.sent_text == ["continue"]
