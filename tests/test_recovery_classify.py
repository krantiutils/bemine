from bemine.config import BemineSettings
from bemine.jobs.auto_recovery import (
    classify, RATE_LIMIT_DEFAULTS, load_recovery_state, save_recovery_state,
    recovery_state_path, recovery_key,
)


def test_classify_rate_limit():
    pane = "API Error: Server is temporarily limiting requests (not your usage limit) · Rate limited"
    assert classify(pane, asks_user=False, signatures=RATE_LIMIT_DEFAULTS) == "rate_limit"


def test_classify_rate_limit_wins_even_if_asks_user():
    pane = "overloaded_error: please try again\n> "
    assert classify(pane, asks_user=True, signatures=RATE_LIMIT_DEFAULTS) == "rate_limit"


def test_classify_trivial_confirm():
    assert classify("Do you want to proceed? (y/n)", asks_user=True,
                    signatures=RATE_LIMIT_DEFAULTS) == "trivial_confirm"


def test_classify_ambiguous_when_asks_user_but_not_trivial():
    assert classify("Which AWS account should I use?", asks_user=True,
                    signatures=RATE_LIMIT_DEFAULTS) == "ambiguous"


def test_classify_healthy():
    assert classify("...thinking...\nWriting file foo.py", asks_user=False,
                    signatures=RATE_LIMIT_DEFAULTS) == "healthy"


def test_classify_destructive_confirm_is_ambiguous_not_trivial():
    for pane in [
        "Delete all branches? (y/n)",
        "Force push to main? (y/n)",
        "Run rm -rf /tmp/x and continue? (y/n)",
        "Reset --hard to origin? (y/n)",
        "Drop table users? (y/n)",
    ]:
        assert classify(pane, asks_user=True, signatures=RATE_LIMIT_DEFAULTS) == "ambiguous", pane


def test_classify_benign_confirm_still_trivial():
    for pane in ["Proceed? (y/n)", "Continue? (y/n)", "Do you want to continue? (y/n)"]:
        assert classify(pane, asks_user=True, signatures=RATE_LIMIT_DEFAULTS) == "trivial_confirm", pane


def test_classify_trivial_confirm_from_pane_without_asks_user():
    # pane-mode: no asks_user signal, but the pane clearly shows a y/n confirm
    assert classify("Proceed? (y/n)", signatures=RATE_LIMIT_DEFAULTS) == "trivial_confirm"


def test_classify_destructive_confirm_from_pane_is_not_trivial():
    # destructive confirm without asks_user must NOT auto-yes -> healthy (no trivial, no asks_user)
    assert classify("Delete all branches? (y/n)", signatures=RATE_LIMIT_DEFAULTS) == "healthy"


def test_classify_healthy_pane_without_asks_user():
    assert classify("writing code...", signatures=RATE_LIMIT_DEFAULTS) == "healthy"


def test_classify_ignores_stale_rate_limit_in_scrollback():
    # An OLD rate-limit high in the scrollback, but the session has since moved on and the
    # bottom is healthy -> must NOT classify as rate_limit.
    old_error = "API Error: Rate limited\n"
    healthy_tail = "\n".join(["working on file %d" % i for i in range(40)])
    pane = old_error + healthy_tail
    assert classify(pane, signatures=RATE_LIMIT_DEFAULTS) == "healthy"


def test_classify_rate_limit_when_error_is_at_bottom():
    pane = "\n".join(["did stuff"] * 40) + "\nAPI Error: Server is temporarily limiting requests · Rate limited"
    assert classify(pane, signatures=RATE_LIMIT_DEFAULTS) == "rate_limit"


def test_state_roundtrip_and_key(tmp_path):
    s = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    p = recovery_state_path(s)
    save_recovery_state(p, {"k": {"attempts": 2}})
    assert load_recovery_state(p) == {"k": {"attempts": 2}}
    assert load_recovery_state(tmp_path / "missing.json") == {}

    assert recovery_key({"device": "mbp", "pane_id": "%2"}) == "mbp:%2"


def test_classify_usage_limit_beats_rate_limit():
    for pane in [
        "You've hit your usage limit. Try again at 3:47am.",
        "You have reached your usage limit. Your limit will reset at 03:47.",
        "5-hour limit reached — upgrade or try again later.",
    ]:
        assert classify(pane, signatures=RATE_LIMIT_DEFAULTS) == "usage_limit", pane


def test_parse_reset_epoch():
    import time as _t
    from bemine.jobs.auto_recovery import _parse_reset_epoch
    # 2026-06-11 01:00 local
    now = _t.mktime((2026, 6, 11, 1, 0, 0, 0, 0, -1))
    e = _parse_reset_epoch("hit your usage limit. Try again at 3:47am", now)
    assert e is not None
    lt = _t.localtime(e)
    assert (lt.tm_hour, lt.tm_min, lt.tm_mday) == (3, 47, 11)  # later today
    # time already past -> tomorrow
    now2 = _t.mktime((2026, 6, 11, 5, 0, 0, 0, 0, -1))
    e2 = _parse_reset_epoch("limit will reset at 03:47", now2)
    assert _t.localtime(e2).tm_mday == 12
    assert _parse_reset_epoch("no time here", now) is None
