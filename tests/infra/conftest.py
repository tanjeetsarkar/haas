"""Make repo-root tooling packages (``loadtest``, ``scripts``) importable in tests."""

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
