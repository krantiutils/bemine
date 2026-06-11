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
