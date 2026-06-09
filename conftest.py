import sys
from pathlib import Path

# Allow test modules to import each other as `tests.<module>`.
sys.path.insert(0, str(Path(__file__).parent))
