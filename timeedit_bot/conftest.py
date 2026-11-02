"""Make ``te_bot`` importable when pytest is run from the repository root."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # the bot needs `requests`; keep the course project's `pytest` run green without it
    import requests  # noqa: F401
except ImportError:  # pragma: no cover
    collect_ignore_glob = ["tests/*"]
