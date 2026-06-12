import os
import sys
from pathlib import Path

# Allow test modules to import each other as `tests.<module>`.
sys.path.insert(0, str(Path(__file__).parent))

# The repo bemine.toml ignores /tmp (scratch dirs must not become indexed
# projects/Discord channels), but pytest tmp_path lives under /tmp — neutralize
# the ignore list for the suite. Tests that exercise the filter itself manage
# this env var explicitly.
os.environ.setdefault("BEMINE_IGNORED_PROJECT_ROOTS", str(Path.home() / "gt"))

# Bemine-launched sessions export BEMINE_HOME/BEMINE_CONFIG pointing at the
# live state dir; tests must resolve paths from their own tmp fixtures.
os.environ.pop("BEMINE_HOME", None)
os.environ.pop("BEMINE_CONFIG", None)

# The repo bemine.toml routes workers into the user's home session ("0");
# tests must not touch it. Empty pins the legacy one-session-per-worker shape;
# home-session tests pass an explicit home_session to the adapter.
os.environ.setdefault("BEMINE_TMUX_HOME_SESSION", "")
