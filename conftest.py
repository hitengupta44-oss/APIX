"""Make the project root importable however pytest is invoked.

`python -m pytest` puts the current directory on sys.path; a bare `pytest`
does not. So the suite passed locally and failed in CI with
`ModuleNotFoundError: No module named 'apix'` — the classic version of this
bug, where the thing that differs is the command, not the code.

A conftest.py at the root fixes it for both forms, and for `pytest tests/`,
`pytest tests/test_index.py::test_x`, and running from an IDE. Preferred over
adding `python -m` to the workflow, because that only fixes the one caller who
happened to complain.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
