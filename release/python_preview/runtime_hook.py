"""Small, side-effect-limited runtime marker for frozen Zvec processes."""

from __future__ import annotations

import os
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    # BackendHost can consume this marker while the PowerShell-free runtime is
    # rolled out.  It contains no user data and does not override explicit input.
    os.environ.setdefault("ZVEC_FROZEN_APP_DIR", str(Path(sys.executable).parent))
