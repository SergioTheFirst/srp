"""Root conftest: put the project root on sys.path for `pytest`.

With pytest's default ``prepend`` import mode the test files' own directory
(``tests/``) lands on ``sys.path`` -- not the project root -- so ``import
server`` / ``import client`` / ``import shared`` would fail. Inserting the root
here (the directory holding this file) fixes that for every test module.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
