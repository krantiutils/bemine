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


def test_state_roundtrip_and_key(tmp_path):
    s = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    p = recovery_state_path(s)
    save_recovery_state(p, {"k": {"attempts": 2}})
    assert load_recovery_state(p) == {"k": {"attempts": 2}}
    assert load_recovery_state(tmp_path / "missing.json") == {}

    class _St:
        device_id = "mbp"
        runtime = "claude"
        session_id = "s1"
    assert recovery_key(_St()) == "mbp:claude:s1"
